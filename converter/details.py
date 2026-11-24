"""Detail views: an enlarged copy of a small area of another view ("DETAIL AG  SCALE 1:1").
Each detail is located on its parent view by matching its geometry (shrunk to the sheet scale) against the
parent's, so anything pointed at inside a detail can be traced back to the main views."""
import math, re
from collections import Counter


def _endpoints(view):
    pts = []
    for p in view.prims:
        if p.kind == "line":
            pts.append(p.pts[0]); pts.append(p.pts[-1])
        elif p.kind in ("circle", "arc"):
            pts.append((p.cx, p.cy))
    return pts


def find_details(text_lines, views, sheet_scale):
    out = []
    for L in text_lines:
        m = re.match(r"\s*DETAIL\s*([A-Z]{1,3})\b", L["text"].upper())
        if not m:
            continue
        # the detail's own scale is written just under its title
        ds = None
        for N in text_lines:
            if 0 < L["y0"] - N["y1"] < 0.25 and abs(N["x0"] - L["x0"]) < 0.6:
                mm = re.search(r"SCALE\W*(\d+)\s*[:/ ]\s*(\d+)", N["text"].upper())
                if mm and int(mm.group(1)) and int(mm.group(2)):
                    ds = int(mm.group(2)) / int(mm.group(1))
        if not ds or not sheet_scale:
            continue
        k = sheet_scale / ds                              # how many times larger the detail is drawn
        above = [v for v in views if not v.iso and v.y0 > L["y1"] - 0.05 and v.y0 - L["y1"] < 2.0
                 and v.x0 - 0.5 < (L["x0"] + L["x1"]) / 2 < v.x1 + 0.5]
        if not above or abs(k - 1) < 1e-6 and False:
            continue
        det = min(above, key=lambda v: v.y0 - L["y1"])
        q = [(x / k, y / k) for x, y in _endpoints(det)]
        best = None
        for par in views:
            if par is det or par.iso:
                continue
            pp = _endpoints(par)
            votes = Counter()
            for a in q:
                for b in pp:
                    votes[(round((b[0] - a[0]) / 0.004), round((b[1] - a[1]) / 0.004))] += 1
            if not votes:
                continue
            (tx, ty), n = votes.most_common(1)[0]
            score = n / max(len(q), 1)
            if best is None or score > best[0]:
                best = (score, par, (tx * 0.004, ty * 0.004))
        if best and best[0] > 0.3:
            out.append(dict(name=m.group(1), view=det, k=k, parent=best[1], t=best[2], score=round(best[0], 2)))
    return out


def to_parent(det, q):
    """a point drawn in the detail -> the same point on its parent view"""
    return (q[0] / det["k"] + det["t"][0], q[1] / det["k"] + det["t"][1])


def inside(det, q, margin=0.4):
    v = det["view"]
    return v.x0 - margin <= q[0] <= v.x1 + margin and v.y0 - margin <= q[1] <= v.y1 + margin
