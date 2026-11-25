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


def _join_pieces(prims, tol=1e-4, piece=0.05):
    """join tiny straight pieces that continue each other (end to end, same direction, nothing else meeting
    there) back into one line. Some exporters chop lines - dashes especially - into pieces a few thousandths
    long. Only pieces shorter than `piece` are joined, so an edge never merges with an extension line that
    carries straight on from its end."""
    def key(q):
        return (round(q[0] / tol), round(q[1] / tol))
    at = {}
    for i, p in enumerate(prims):
        for q in (p.pts[0], p.pts[-1]):
            at.setdefault(key(q), []).append(i if p.kind == "line" else -1)    # an arc's end blocks joining

    def unit(p):
        (ax, ay), (bx, by) = p.pts[0], p.pts[-1]
        L = math.hypot(bx - ax, by - ay)
        return ((bx - ax) / L, (by - ay) / L) if L > 1e-9 else None
    small = lambda p: p.kind == "line" and math.dist(p.pts[0], p.pts[-1]) < piece
    parent = list(range(len(prims)))
    def f(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for ids in at.values():
        if len(ids) != 2 or -1 in ids or ids[0] == ids[1]:
            continue
        a, b = prims[ids[0]], prims[ids[1]]
        if not (small(a) and small(b)):
            continue
        ua, ub = unit(a), unit(b)
        if ua and ub and a.width == b.width and abs(ua[0] * ub[1] - ua[1] * ub[0]) < 1e-3:
            parent[f(ids[0])] = f(ids[1])
    groups = {}
    for i, p in enumerate(prims):
        groups.setdefault(f(i) if p.kind == "line" else ("x", i), []).append(p)
    out = []
    for g in groups.values():
        if len(g) == 1:
            out.append(g[0]); continue
        d = next(u for u in map(unit, g) if u)
        pts = [q for p in g for q in (p.pts[0], p.pts[-1])]
        s = [q[0] * d[0] + q[1] * d[1] for q in pts]
        a, b = pts[s.index(min(s))], pts[s.index(max(s))]
        out.append(Prim("line", [a, b], g[0].width, cls=g[0].cls, path=g[0].path, dashed=g[0].dashed))
    return out


def _touches(prims, tol=0.002, cell=0.1):         # tol: well under the gaps of fine dashes (0.0056 in seen)
    """for each prim, [prims touching its start, prims touching its end] (None for a circle: no ends)"""
    segs = []                                          # (prim index, a, b) for each straight piece
    for i, p in enumerate(prims):
        pts = p.pts if p.kind != "line" else [p.pts[0], p.pts[-1]]
        segs.extend((i, a, b) for a, b in zip(pts, pts[1:]))
    grid = {}
    for k, (i, a, b) in enumerate(segs):
        for gx in range(int(math.floor((min(a[0], b[0]) - tol) / cell)), int(math.floor((max(a[0], b[0]) + tol) / cell)) + 1):
            for gy in range(int(math.floor((min(a[1], b[1]) - tol) / cell)), int(math.floor((max(a[1], b[1]) + tol) / cell)) + 1):
                grid.setdefault((gx, gy), []).append(k)

    def near(q, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / L2))
        return math.hypot(q[0] - a[0] - t * dx, q[1] - a[1] - t * dy) < tol

    touch = []
    for i, p in enumerate(prims):
        if p.kind == "circle":
            touch.append(None); continue
        ends = []
        for q in (p.pts[0], p.pts[-1]):
            ks = grid.get((int(math.floor(q[0] / cell)), int(math.floor(q[1] / cell))), [])
            ends.append({segs[k][0] for k in ks if segs[k][0] != i and near(q, segs[k][1], segs[k][2])})
        touch.append(ends)
    return touch


def _dangling(prims):
    """the prims that are not part of a closed outline. A part's edges close into loops, so each end of
    an edge touches another edge; dimension, extension, leader and centre lines and the pieces of a dashed
    line all have a free end. Peel off every line with a free end, again and again (an extension line goes
    first, then the dimension line that ended on it), and return what was peeled. Circles have no ends
    and are kept."""
    touch = _touches(prims)
    users = {}                                         # prim -> prims whose ends touch it
    for i, t in enumerate(touch):
        for side in t or ():
            for j in side:
                users.setdefault(j, set()).add(i)
    alive = [True] * len(prims)
    todo = list(range(len(prims)))
    while todo:
        i = todo.pop()
        t = touch[i]
        if not alive[i] or t is None:
            continue
        if all(any(alive[j] for j in side) for side in t):
            continue
        alive[i] = False                               # a free end: not an edge of the part
        todo.extend(users.get(i, ()))
        todo.extend(j for side in t for j in side)
    return [p for p, a in zip(prims, alive) if not a]


def _symbols(prims, size=0.3, tol=0.002):
    """small closed shapes of straight lines standing on their own - dimension tick marks and arrow heads
    drawn as outlines - are not part of the part. Shapes are joined end to end only (as an outline's edges
    meet), so an arrow whose tip lands on an edge or a hole is still a shape of its own. A symbol is also
    slim (a triangle or a sliver: its area is under 0.3 x its diagonal squared), so a small square or slot
    cut in the part is kept. Returns the symbols' prims."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union
    ends = {}
    for i, p in enumerate(prims):
        if p.kind != "circle":
            for q in (p.pts[0], p.pts[-1]):
                ends.setdefault((round(q[0] / tol), round(q[1] / tol)), []).append(i)
    parent = list(range(len(prims)))
    def f(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for (kx, ky), ids in list(ends.items()):
        near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in ends.get((kx + dx, ky + dy), ())]
        for j in near[1:]:
            parent[f(j)] = f(near[0])
    groups = {}
    for i in range(len(prims)):
        groups.setdefault(f(i), []).append(prims[i])
    out = []
    # an arrow head is a slim triangle of three short lines, even when its tip lands on the end of an edge
    # (that joins it to the outline end to end, so it is not a shape of its own)
    key = lambda q: (round(q[0] / tol), round(q[1] / tol))
    short = [i for i, p in enumerate(prims) if p.kind == "line" and math.dist(p.pts[0], p.pts[-1]) < size]
    by_end = {}
    for i in short:
        for q in (prims[i].pts[0], prims[i].pts[-1]):
            by_end.setdefault(key(q), []).append(i)
    every_end = {}                                     # all prims' ends, to see what else meets a corner
    for n, p in enumerate(prims):
        if p.kind != "circle":
            for q in (p.pts[0], p.pts[-1]):
                every_end.setdefault(key(q), set()).add(n)
    def joined_corners(tri, corners):                  # corners where something outside the triangle ends
        return sum(1 for c in corners if every_end.get(c, set()) - set(tri))
    for i in short:
        a, b = key(prims[i].pts[0]), key(prims[i].pts[-1])
        for j in by_end.get(b, ()):
            if j == i:
                continue
            c = key(prims[j].pts[-1]) if key(prims[j].pts[0]) == b else key(prims[j].pts[0])
            for k in by_end.get(c, ()):
                if k in (i, j) or a not in (key(prims[k].pts[0]), key(prims[k].pts[-1])):
                    continue
                P = [prims[i].pts[0], prims[i].pts[-1], prims[j].pts[-1] if key(prims[j].pts[0]) == b else prims[j].pts[0]]
                area = abs((P[1][0] - P[0][0]) * (P[2][1] - P[0][1]) - (P[2][0] - P[0][0]) * (P[1][1] - P[0][1])) / 2
                longest = max(math.dist(P[0], P[1]), math.dist(P[1], P[2]), math.dist(P[0], P[2]))
                # only its tip may meet other lines: a triangle joined at two or three corners is outline
                if area < 0.3 * longest * longest and joined_corners((i, j, k), (a, b, c)) <= 1:
                    out.extend(prims[n] for n in (i, j, k) if prims[n] not in out)
    for g in groups.values():
        if any(p.kind != "line" for p in g) or len(g) > 12:
            continue
        xs = [q[0] for p in g for q in p.pts]; ys = [q[1] for p in g for q in p.pts]
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        if diag >= size:
            continue
        area = sum(poly.area for poly in polygonize(unary_union([LineString(p.pts) for p in g])))
        if area < 0.3 * diag * diag:
            out.extend(g)
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
    # Some exporters draw every line at one width, with dashes as separate short strokes: then the width
    # says nothing and dimension lines, leaders and dashes all come out "visible". Sort them by shape instead.
    if prims and sum(p.cls == "visible" for p in prims) > 0.97 * len(prims):
        prims = _join_pieces(prims)        # such exporters also chop lines into tiny pieces
        for _ in range(4):                 # peel loose lines, then lone tick marks / arrow heads, and again
            vis = [p for p in prims if p.cls == "visible"]
            gone = _dangling(vis)
            for p in gone:
                p.cls = "thin"
            vis = [p for p in vis if p.cls == "visible"]
            sym = _symbols(vis)
            for p in sym:
                p.cls = "thin"
            if not sym:
                break
    page_size = (page.rect.width / 72.0, H / 72.0)
    return prims, page_size
