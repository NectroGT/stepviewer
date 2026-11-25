"""Attach hole notes (callouts) to the holes they point at, and apply them."""
import math
from text import TAP


def leader_tips(note, thin_lines, reach=0.08, join=0.012):
    """follow the leader line(s) that start at a note's text box; return the far end points"""
    x0, y0, x1, y1 = note["x0"] - reach, note["y0"] - reach, note["x1"] + reach, note["y1"] + reach
    segs = [(p.pts[0], p.pts[-1]) for p in thin_lines if math.dist(p.pts[0], p.pts[-1]) > 0.02]
    inbox = lambda q: x0 <= q[0] <= x1 and y0 <= q[1] <= y1
    def crosses(a, b):                         # a line running under / through the note (an underline
        if inbox(a) or inbox(b):               # sticking out past both ends of the text)
            return False
        from shapely.geometry import LineString, box
        return LineString([a, b]).intersects(box(x0, y0, x1, y1))
    tips = []
    for a, b in segs:
        under = crosses(a, b)
        for s_, e_ in ((a, b), (b, a)):
            if not under and (not inbox(s_) or inbox(e_)):
                continue
            cur, prev, seen = e_, s_, {(a, b)}
            for _ in range(4):                 # a leader may bend once or twice
                nxt = next(((c, d) for c, d in segs if (c, d) not in seen and
                            (math.dist(c, cur) < join or math.dist(d, cur) < join)), None)
                if nxt is None:
                    break
                seen.add(nxt)
                prev, cur = cur, (nxt[1] if math.dist(nxt[0], cur) < join else nxt[0])
            tips.append(_through_arrow(cur, prev, segs, join))
    return tips


def _through_arrow(cur, prev, segs, join):
    """a leader that ends on the back edge of an arrow head drawn as an outline: the arrow's tip is where
    it points - the corner of the head farthest along the leader"""
    L = math.dist(cur, prev)
    if L < 1e-9:
        return cur
    ux, uy = (cur[0] - prev[0]) / L, (cur[1] - prev[1]) / L
    base = [(c, d) for c, d in segs if math.dist(c, d) < 0.25 and _seg_dist(cur, c, d) < join
            and min(math.dist(c, cur), math.dist(d, cur)) > join]
    if not base:
        return cur
    # the head's corners: the back edge's ends and the far ends of the short sides joined to them
    corners = {q for c, d in base for q in (c, d)}
    for c, d in segs:
        if math.dist(c, d) < 0.25:
            for q, o in ((c, d), (d, c)):
                if any(math.dist(q, k) < join for k in list(corners)):
                    corners.add(o)
    ahead = [((q[0] - cur[0]) * ux + (q[1] - cur[1]) * uy, q) for q in corners]
    best = max(ahead, default=(0, cur))
    return best[1] if best[0] > join else cur


def _seg_dist(p, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    L = ax * ax + ay * ay
    t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L))
    return math.dist(p, (a[0] + t * ax, a[1] + t * ay))


def attach(notes, thin_lines, hole_groups, side_holes_paper, details=(), side_views=()):
    """hole_groups: [dict(x, y, r=[(radius, side)]) in paper units, where drawn];
    side_holes_paper: [(index, (x0, y0), (x1, y1), r)] the hole's axis as drawn on the sheet;
    details: detail views found on the sheet (a tip inside one is traced back to its parent view);
    side_views: [(view, horizontal, [(hole index, u, r)])] - a tip on a side / section view picks the hole
    whose edges it lies between. Returns {("hole", i) | ("side", i): info}"""
    from details import inside, to_parent
    out = {}
    for n in notes:
        for tip in leader_tips(n, thin_lines):
            for d in details:
                if inside(d, tip):
                    tip = to_parent(d, tip); break
            best = None
            for i, g in enumerate(hole_groups):
                for r, _ in g["r"]:
                    dd = abs(math.dist(tip, (g["x"], g["y"])) - r)
                    if math.dist(tip, (g["x"], g["y"])) <= r + 0.03 and (best is None or dd < best[0]):
                        best = (dd, ("hole", i))
            for i, a, b, r, drawn in side_holes_paper:
                dd = _seg_dist(tip, a, b) - r
                if drawn is not None:                   # also where it is drawn as a circle in a side view
                    dd = min(dd, math.dist(tip, drawn) - r)
                if dd < 0.03 and (best is None or dd < best[0]):
                    best = (max(dd, 0), ("side", i))
            if best is None:
                for v, horizontal, cols in side_views:
                    if v.x0 - 0.02 <= tip[0] <= v.x1 + 0.02 and v.y0 - 0.02 <= tip[1] <= v.y1 + 0.02:
                        u = tip[0] if horizontal else tip[1]
                        for i, uc, r in cols:
                            if abs(u - uc) <= r + 0.01 and (best is None or abs(u - uc) < best[0]):
                                best = (abs(u - uc), ("hole", i))
                        if best is None:          # an edge hole seen from the side: u falls on its axis
                            for i, a, b, r, _ in side_holes_paper:
                                lo, hi = sorted((a[0], b[0]) if horizontal else (a[1], b[1]))
                                if lo - r - 0.02 <= u <= hi + r + 0.02:
                                    best = (0.0, ("side", i)); break
            if best:
                out[best[1]] = n["info"]
    return out


def apply_hole(h, info, T, notes, label):
    """change a Hole's steps to follow its note"""
    steps = sorted(h.steps)
    if not steps:
        return
    r0, side0, d0 = steps[0]
    tag = []
    if "thread" in info and info["thread"] in TAP:
        r0 = TAP[info["thread"]] / 2
        tag.append(info["thread"])
    elif "dia" in info and abs(info["dia"] / 2 - r0) < 0.15 * r0:
        r0 = info["dia"] / 2
    if info.get("thru") or ("csk_d" in info and "depth" not in info):     # a countersunk hole goes through
        side0, d0 = "thru", T
    elif "depth" in info and info["depth"] < T:
        if side0 == "thru":
            side0 = "top"
        d0 = info["depth"]
    steps[0] = (r0, side0, d0)
    if "cbore_d" in info and len(steps) > 1:
        R, sd, d = steps[1]
        if abs(info["cbore_d"] / 2 - R) < 0.2 * R:
            steps[1] = (info["cbore_d"] / 2, sd, info.get("cbore_depth", d))
    h.steps = steps
    h.note = " ".join(tag) or None
