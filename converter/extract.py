"""Read the vector geometry of one PDF page and sort it by line type.

Engineering PDFs exported from CAD (NX, SolidWorks, Creo ...) keep the drawing as stroked paths:
  thick strokes  -> visible edges        thin strokes -> hidden / centre / dimension lines
  filled shapes  -> text glyphs and arrow heads (ignored here)
Everything is returned in paper inches with y pointing up."""
import math
from dataclasses import dataclass, field
import numpy as np
import pymupdf


@dataclass
class Prim:
    kind: str                 # "line" | "arc" | "circle"
    pts: list                 # line: [p0, p1]; arc/circle: sampled points
    width: float
    cls: str = ""             # visible | thin | hatch | heavy
    cx: float = 0.0
    cy: float = 0.0
    r: float = 0.0
    a0: float = 0.0           # arc start / end angle (deg, ccw)
    a1: float = 0.0
    path: int = -1
    dashed: bool = False      # drawn with a dash pattern: a hidden or centre line

    def bbox(self):
        a = np.array(self.pts)
        return a[:, 0].min(), a[:, 1].min(), a[:, 0].max(), a[:, 1].max()


def _bez(p0, p1, p2, p3, n=8):
    out = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        out.append((mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1]))
    return out


def fit_circle(pts):
    """algebraic least-squares circle; returns cx, cy, r, rms"""
    a = np.asarray(pts, float)
    A = np.c_[2 * a[:, 0], 2 * a[:, 1], np.ones(len(a))]
    b = (a ** 2).sum(1)
    (cx, cy, c), *_ = np.linalg.lstsq(A, b, rcond=None)
    r = math.sqrt(max(c + cx * cx + cy * cy, 0))
    rms = float(np.sqrt(((np.hypot(a[:, 0] - cx, a[:, 1] - cy) - r) ** 2).mean()))
    return cx, cy, r, rms


def classify_widths(widths):
    """the most used thick width is 'visible'; thinner ones are 'thin' (dims, hidden, centre) or 'hatch'"""
    ws = sorted(set(widths))
    if not ws:
        return {}
    count = {w: widths.count(w) for w in ws}
    # visible = the thickest width that is used a lot (border lines are thick but few)
    vis = max((w for w in ws if count[w] >= 0.05 * len(widths)), default=max(ws))
    out = {}
    thin = [w for w in ws if w < vis]
    for w in ws:
        if w == vis:
            out[w] = "visible"
        elif w > vis:
            out[w] = "heavy"
        elif w == max(thin):
            out[w] = "thin"
        else:
            out[w] = "hatch"
    return out


def page_prims(pdf, pageno):
    doc = pymupdf.open(pdf)
    page = doc[pageno]
    H = page.rect.height
    P = lambda q: (q.x / 72.0, (H - q.y) / 72.0)
    prims, widths = [], []
    dashed_paths = set()
    for k, d in enumerate(page.get_drawings()):
        if d["type"] != "s":
            continue
        w = round(d.get("width") or 0, 2)
        items = d["items"]
        dash = str(d.get("dashes") or "")
        if dash.strip() and not dash.replace(" ", "").startswith("[]"):
            dashed_paths.add(k)
        # a closed run of beziers = one circle / arc
        curves = [it for it in items if it[0] == "c"]
        if curves and len(curves) == len(items):
            pts = []
            for it in curves:
                seg = _bez(P(it[1]), P(it[2]), P(it[3]), P(it[4]))
                pts.extend(seg if not pts else seg[1:])
            cx, cy, r, rms = fit_circle(pts)
            closed = math.dist(pts[0], pts[-1]) < 1e-3
            if rms < 0.02 * r + 1e-4:
                pr = Prim("circle" if closed else "arc", pts, w, cx=cx, cy=cy, r=r, path=k)
                if not closed:
                    angs = [math.degrees(math.atan2(y - cy, x - cx)) for x, y in pts]
                    pr.a0, pr.a1 = angs[0], angs[-1]
                prims.append(pr); widths.append(w)
                continue
        # a long chain of short straight pieces: some exporters draw arcs, circles (and even straight
        # chamfers) as polylines. Join it back into one line, or one arc / circle.
        if len(items) >= 6 and all(it[0] == "l" for it in items) and \
                all(math.dist(a[2], b[1]) < 1e-3 * 72 for a, b in zip(items, items[1:])):
            pts = [P(it[1]) for it in items] + [P(items[-1][2])]
            a, b = pts[0], pts[-1]
            L = math.dist(a, b)
            if L > 1e-4:
                dev = max(abs((q[0] - a[0]) * (b[1] - a[1]) - (q[1] - a[1]) * (b[0] - a[0])) / L for q in pts)
                if dev < 0.002:
                    # widths are counted per piece drawn, as before joining, so the line-type split is unchanged
                    prims.append(Prim("line", [a, b], w, path=k)); widths.extend([w] * len(items))
                    continue
            cx, cy, r, rms = fit_circle(pts)
            closed = L < 1e-3
            if rms < 0.002 * r + 1e-4 and r < 20:
                pr = Prim("circle" if closed else "arc", pts, w, cx=cx, cy=cy, r=r, path=k)
                if not closed:
                    angs = [math.degrees(math.atan2(y - cy, x - cx)) for x, y in pts]
                    pr.a0, pr.a1 = angs[0], angs[-1]
                prims.append(pr); widths.extend([w] * len(items))
                continue
        for it in items:
            if it[0] == "l":
                a, b = P(it[1]), P(it[2])
                if math.dist(a, b) > 1e-6:
                    prims.append(Prim("line", [a, b], w, path=k)); widths.append(w)
            elif it[0] == "c":
                pts = _bez(P(it[1]), P(it[2]), P(it[3]), P(it[4]))
                for a, b in zip(pts, pts[1:]):
                    prims.append(Prim("line", [a, b], w, path=k)); widths.append(w)
            elif it[0] == "re":
                r = it[1]
                c = [P(pymupdf.Point(r.x0, r.y0)), P(pymupdf.Point(r.x1, r.y0)), P(pymupdf.Point(r.x1, r.y1)), P(pymupdf.Point(r.x0, r.y1))]
                for a, b in zip(c, c[1:] + c[:1]):
                    prims.append(Prim("line", [a, b], w, path=k)); widths.append(w)
    cls = classify_widths(widths)
    for p in prims:
        p.cls = cls.get(p.width, "thin")
        p.dashed = p.path in dashed_paths
        # a dashed stroke is a hidden or centre line whatever its width (some exporters vary the widths)
        if p.dashed and p.cls == "hatch":
            p.cls = "thin"
    page_size = (page.rect.width / 72.0, H / 72.0)
    return prims, page_size
