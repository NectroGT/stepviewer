"""Turn the views of one part into features: outline, thickness, holes, cut-outs and pockets."""
import math
from dataclasses import dataclass, field
import numpy as np
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import unary_union, polygonize
from extract import fit_circle

# sizes are rounded to this grid (model units, inch). 0.0005 in suits inch drawings; pdf2step sets a
# 0.01 mm grid for drawings dimensioned in millimetres, so 10 mm comes out 10.00, not 9.995
SNAP_INCH, SNAP_MM = 0.0005, 0.01 / 25.4
SNAP = SNAP_INCH


@dataclass
class Hole:
    x: float
    y: float
    steps: list            # [(radius, side, depth)] side "top"/"bottom"/"thru"
    note: str = None       # thread from the hole note, e.g. "#10-24"
    paper: tuple = None    # centre on the sheet (inches)
    radii_paper: list = None
    drawn: list = None     # [((x, y), r)] every place the hole is drawn on the sheet
    csk: tuple = None      # countersink (radius, included angle deg, "top" / "bottom", z of its face or None):
                           # "top" = on a face looking up (the cone narrows downwards)


@dataclass
class Cut:
    ring: list             # [("line", p0, p1) | ("arc", p0, pm, p1)] in model units
    side: str              # thru / top / bottom
    depth: float


@dataclass
class SideHole:
    entry: tuple           # (x, y) where the hole enters, on the outline
    direction: tuple       # unit (dx, dy) into the part
    z: float
    r: float
    depth: float
    note: str = None
    paper: tuple = None    # a point on the sheet where this hole is drawn


@dataclass
class Raised:
    ring: list             # outline of the raised area
    inner: list            # rings of holes in it (list of seg lists)
    z_top: float


@dataclass
class PartModel:
    outline: list
    thickness: float
    holes: list = field(default_factory=list)
    cuts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    side_holes: list = field(default_factory=list)
    base: float = 0.0              # if > 0: plate thickness outside the raised areas
    raised: list = field(default_factory=list)
    turned: tuple = None           # (axis x in the plan) for a turned (round) part; holes = axial bores
    outline_poly: object = None
    bosses: list = field(default_factory=list)   # [(x, y, r, z0, z1, [(r, depth)])]: round posts standing in a
                                                 # pocket, from its floor z0 up to z1, with holes from their top
    rounds: list = field(default_factory=list)   # [(along, u, z, radius)]: round the edges running along
                                                 # "x" or "y" that pass the corner (u = the other plan coordinate, z)


# ---------------------------------------------------------------- views -> part groups
def group_parts(views, pair_tol=0.03):
    """plan view + its side views (thin neighbours) + an optional bottom view (mirrored twin below)."""
    used, parts = set(), []
    cands = [v for v in views if not v.iso]
    # of two same-size views stacked vertically the upper one is the plan, the lower its bottom view
    cands.sort(key=lambda v: (-round(v.w * v.h, 1), -v.y0))
    for plan in cands:
        if id(plan) in used:
            continue
        side, bottom = [], None
        small = min(plan.w, plan.h)
        for v in cands:
            if v is plan or id(v) in used:
                continue
            if abs(v.x0 - plan.x0) < pair_tol and abs(v.x1 - plan.x1) < pair_tol:
                d = "below" if v.y1 < plan.y0 else "above"
                if v.h < 0.8 * plan.h + 1e-3 and abs(v.h - plan.h) > pair_tol:      # a 40 deep part on a 50 high view is 0.8 exactly
                    side.append((d, v))
                elif abs(v.h - plan.h) < pair_tol and d == "below" and bottom is None:
                    bottom = v
            elif abs(v.y0 - plan.y0) < pair_tol and abs(v.y1 - plan.y1) < pair_tol and v.w < 0.8 * plan.w + 1e-3:
                side.append(("right" if v.x0 > plan.x1 else "left", v))
            elif _end_view(v, plan, "x"):
                side.append(("below" if v.y1 < plan.y0 else "above", v))
            elif _end_view(v, plan, "y"):
                side.append(("right" if v.x0 > plan.x1 else "left", v))
        if not side:
            continue
        used.add(id(plan)); used.update(id(v) for _, v in side)
        if bottom is not None:
            used.add(id(bottom))
        parts.append((plan, side, bottom))
    return parts


def _end_view(v, plan, along, tol=0.03):
    """the end view of a stepped round part (shaft): a round view, smaller than the plan's widest step,
    centred on the plan's centre line and beside it along that line (end views of a shaft are only as
    big as the step facing them, so their edges do not line up with the plan's)."""
    arcs = [p for p in v.prims if p.kind in ("circle", "arc")]
    if not arcs:
        return False
    big = max(arcs, key=lambda p: p.r)
    if abs(2 * big.r - max(v.w, v.h)) > tol:
        return False
    if along == "y":     # beside it (left / right): centres level, end view not taller than the plan
        return (v.x0 > plan.x1 or v.x1 < plan.x0) and abs(big.cy - (plan.y0 + plan.y1) / 2) < tol \
            and 2 * big.r < plan.h + tol and v.w < 0.8 * plan.w
    return (v.y0 > plan.y1 or v.y1 < plan.y0) and abs(big.cx - (plan.x0 + plan.x1) / 2) < tol \
        and 2 * big.r < plan.w + tol and v.h < 0.8 * plan.h


# ---------------------------------------------------------------- geometry helpers
def turned_parts(unused, prims, used):
    """views drawn turned on the sheet: straighten each group of same-angle views and pair them again.
    Returns [((plan, side, bottom), angle)]; the ids of the views used are added to `used`."""
    from views import find_views, dominant_angle, rotate_prims
    tilted = {}
    for v in unused:
        a = dominant_angle(v)
        if abs(a) > 1.0:
            key = next((k for k in tilted if abs(k - a) < 0.5), a)
            tilted.setdefault(key, []).append(v)
    rotated_parts = []
    thin_all = [p for p in prims if p.cls == "thin"]
    for a, vs in tilted.items():
        sub = rotate_prims([p for v in vs for p in v.prims] + thin_all, -a)
        rviews = find_views(sub, (1e3, 1e3))
        for pr in group_parts(rviews):
            rotated_parts.append((pr, a))
            used.update(id(v) for v in vs)
    return rotated_parts


def _geoms(prims):
    out = []
    for p in prims:
        if p.kind == "circle":
            out.append(LineString([(p.cx + p.r * math.cos(t), p.cy + p.r * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 73)]))
        elif len(p.pts) >= 2:
            out.append(LineString(p.pts))
    return out


def _snap_endpoints(prims, tol):
    """close tiny gaps between line ends so the outline polygonizes"""
    ends = []
    for p in prims:
        if p.kind != "circle":
            ends.append(p.pts[0]); ends.append(p.pts[-1])
    if not ends:
        return
    # group ends lying within tol of each other (chains too) and move every end of a group to the group's
    # mean, so they meet exactly (each end moved to its own neighbours' mean could still miss the others)
    n = len(ends)
    parent = list(range(n))
    def f(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    cells = {}
    for i, q in enumerate(ends):
        cells.setdefault((math.floor(q[0] / tol), math.floor(q[1] / tol)), []).append(i)
    for (cx, cy), ids in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in cells.get((cx + dx, cy + dy), ()):
                    for i in ids:
                        if i < j and math.dist(ends[i], ends[j]) < tol:
                            parent[f(i)] = f(j)
    groups = {}
    for i in range(n):
        groups.setdefault(f(i), []).append(i)
    where = [None] * n
    for ids in groups.values():
        if len(ids) > 1:
            m = (sum(ends[i][0] for i in ids) / len(ids), sum(ends[i][1] for i in ids) / len(ids))
            for i in ids:
                where[i] = m
    i = 0
    for p in prims:
        if p.kind == "circle":
            continue
        for k in (0, -1):
            if where[i] is not None:
                p.pts[k] = where[i]
            i += 1


def _snap_onto_lines(prims, tol, cell=0.1):
    """an end that meets no other end but lands (within tol) on the middle of another line - a T junction
    drawn a hair off - is moved onto that line, so the outline closes there"""
    ends = {}
    for i, p in enumerate(prims):
        if p.kind != "circle":
            for k in (0, -1):
                ends.setdefault((round(p.pts[k][0] / tol), round(p.pts[k][1] / tol)), []).append((i, k))
    loose = []
    for (gx, gy), lst in ends.items():
        n = sum(len(ends.get((gx + dx, gy + dy), ())) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        if n == 1:
            loose.extend(lst)
    if not loose:
        return
    grid = {}
    for i, p in enumerate(prims):
        for a, b in zip(p.pts, p.pts[1:]):
            for x in range(int(math.floor((min(a[0], b[0]) - tol) / cell)), int(math.floor((max(a[0], b[0]) + tol) / cell)) + 1):
                for y in range(int(math.floor((min(a[1], b[1]) - tol) / cell)), int(math.floor((max(a[1], b[1]) + tol) / cell)) + 1):
                    grid.setdefault((x, y), []).append((i, a, b))
    for i, k in loose:
        q = prims[i].pts[k]
        best = None
        for j, a, b in grid.get((int(math.floor(q[0] / cell)), int(math.floor(q[1] / cell))), ()):
            if j == i:
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / L2))
            r = (a[0] + t * dx, a[1] + t * dy)
            d = math.dist(q, r)
            if d < tol and (best is None or d < best[0]):
                best = (d, r)
        if best and best[0] > 1e-9:
            # just across the line (a crossing is found for certain; landing exactly on it may round off)
            (d, r) = best
            prims[i].pts[k] = (r[0] + (r[0] - q[0]) / d * 1e-4, r[1] + (r[1] - q[1]) / d * 1e-4)


def faces(prims, snap=0.004):
    _snap_endpoints(prims, snap)
    _snap_onto_lines(prims, snap)
    lines = unary_union(_geoms(prims))
    return list(polygonize(lines))


def circle_of_ring(ring, tol):
    cx, cy, r, rms = fit_circle(np.array(ring.coords)[:-1])
    return (cx, cy, r) if rms < tol and r > 0 else None


def to_segments(coords, circles, tol):
    """ring coordinates -> list of ("line", p0, p1) / ("arc", p0, pm, p1), arcs re-made from source circles"""
    pts = [tuple(c) for c in coords[:-1]]
    n = len(pts)
    # every source circle each vertex lies on (a vertex where two arcs meet lies on both)
    on = [tuple(c for c in circles if abs(math.dist(p, (c[0], c[1])) - c[2]) < tol) for p in pts]
    segs, i = [], 0
    # start at a vertex that is not inside an arc run
    start = next((k for k in range(n) if not on[k] or not set(on[k - 1]) & set(on[k])), 0)
    order = [(start + k) % n for k in range(n)] + [start]
    k = 0
    while k < len(order) - 1:
        a = order[k]
        shared = [c for c in on[a] if c in on[order[k + 1]]]
        if shared:
            c = shared[0]
            j = k + 1
            while j < len(order) - 1 and c in on[order[j + 1]]:
                j += 1
            # order[k..j] all lie on c: the arc ends at the last of them, not at the vertex after it
            run = order[k:j + 1]
            if len(run) >= 4:
                p0, p1 = pts[run[0]], pts[run[-1]]
                if math.dist(p0, p1) < 1e-9:
                    # a whole circle: two half arcs about the source circle's centre (the ring's middle
                    # vertex need not be opposite the start - pieces of a circle can be unevenly sampled)
                    cx, cy, r = c
                    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
                    q = [(cx + r * math.cos(a0 + t * math.pi / 2), cy + r * math.sin(a0 + t * math.pi / 2)) for t in (1, 2, 3)]
                    segs.append(("arc", p0, q[0], q[1]))
                    segs.append(("arc", q[1], q[2], p0))
                else:
                    pm = pts[run[len(run) // 2]]
                    segs.append(("arc", p0, pm, p1))
                k = j
                continue
        segs.append(("line", pts[a], pts[order[k + 1]]))
        k += 1
    segs = _fit_polyline_arcs(segs, tol)
    # merge collinear lines
    out = []
    for s in segs:
        if out and s[0] == "line" and out[-1][0] == "line":
            p0, p1 = out[-1][1], out[-1][2]
            q1 = s[2]
            v1 = (p1[0] - p0[0], p1[1] - p0[1]); v2 = (q1[0] - p1[0], q1[1] - p1[1])
            cr = v1[0] * v2[1] - v1[1] * v2[0]
            if abs(cr) < 1e-6 * max(1e-9, math.hypot(*v1) * math.hypot(*v2)) * 1e3 and v1[0] * v2[0] + v1[1] * v2[1] > 0:
                out[-1] = ("line", p0, q1); continue
        out.append(s)
    return out


def _fit_polyline_arcs(segs, tol):
    """arcs that the PDF drew as many short straight pieces -> one arc"""
    out, i = [], 0
    while i < len(segs):
        if segs[i][0] != "line" or math.dist(segs[i][1], segs[i][2]) > 0.12:
            out.append(segs[i]); i += 1; continue
        j, best = i, None
        while j + 1 < len(segs) and segs[j + 1][0] == "line" and math.dist(segs[j + 1][1], segs[j + 1][2]) <= 0.12:
            j += 1
            if j - i >= 4:
                pts = [segs[i][1]] + [segs[k][2] for k in range(i, j + 1)]
                cx, cy, r, rms = fit_circle(pts)
                dev = max(abs(math.dist(q, (cx, cy)) - r) for q in pts)
                # every turn the same way (no straight run or corner folded in)
                cr = [((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) for a, b, c in zip(pts, pts[1:], pts[2:])]
                same = all(x > 1e-9 for x in cr) or all(x < -1e-9 for x in cr)
                if dev < 1.5e-4 and same and r < 50:
                    best = j
                else:
                    break
        if best is not None:
            pts = [segs[i][1]] + [segs[k][2] for k in range(i, best + 1)]
            out.append(("arc", pts[0], pts[len(pts) // 2], pts[-1]))
            i = best + 1
        else:
            out.append(segs[i]); i += 1
    return out


# ---------------------------------------------------------------- depth from a side view
class SideReader:
    """reads feature depths from a side / section view next to the plan view."""
    def __init__(self, where, view, plan, scale):
        self.where, self.v, self.scale = where, view, scale
        self.horizontal = where in ("below", "above")          # shares the plan's x axis
        if self.horizontal:
            self.t0, self.t1 = view.y0, view.y1
            top_at_high = where == "below"                     # third angle: the edge next to the plan is its front face
        else:
            self.t0, self.t1 = view.x0, view.x1
            top_at_high = where == "left"
        self.top_at_high = top_at_high
        self.T = (self.t1 - self.t0) * scale
        self.lines, self.flats, self.vflats, self.hflats = [], [], [], []
        for p in view.prims + view.thin:
            if p.kind != "line":
                continue
            (x0, y0), (x1, y1) = p.pts[0], p.pts[-1]
            if self.horizontal and abs(x1 - x0) < 1e-3:
                self.lines.append((x0, min(y0, y1), max(y0, y1)))
            if not self.horizontal and abs(y1 - y0) < 1e-3:
                self.lines.append((y0, min(x0, x1), max(x0, x1)))
            if self.horizontal and abs(y1 - y0) < 1e-3:
                self.flats.append((y0, min(x0, x1), max(x0, x1)))
                if p.cls == "visible":
                    self.vflats.append((y0, min(x0, x1), max(x0, x1)))
                else:
                    self.hflats.append((y0, min(x0, x1), max(x0, x1)))
            if not self.horizontal and abs(x1 - x0) < 1e-3:
                self.flats.append((x0, min(y0, y1), max(y0, y1)))
                if p.cls == "visible":
                    self.vflats.append((x0, min(y0, y1), max(y0, y1)))
                else:
                    self.hflats.append((x0, min(y0, y1), max(y0, y1)))
        self.lines = self._merge(self.lines)

    @staticmethod
    def _merge(lines, tol=1e-3, gap=0.07):
        """join dashes on the same coordinate into spans"""
        lines.sort(key=lambda s: (round(s[0] / tol), s[1]))
        out = []
        for u, a, b in lines:
            if out and abs(out[-1][0] - u) < tol and a <= out[-1][2] + gap:
                out[-1] = (out[-1][0], min(out[-1][1], a), max(out[-1][2], b))
            else:
                out.append((u, a, b))
        return out

    def floor(self, u1, u2, tol=0.004, hidden=False):
        """model z of a flat (pocket floor) spanning u1..u2, strictly between the faces"""
        best = None
        flats = self._merge([(t, a, b) for t, a, b in (self.hflats if hidden else self.flats)])
        for t, a, b in flats:
            ov = min(b, u2) - max(a, u1)
            if ov < 0.6 * (u2 - u1) or a < u1 - 0.1 * (u2 - u1) - tol or b > u2 + 0.1 * (u2 - u1) + tol:
                continue
            z = (t - self.t0) * self.scale
            if not self.top_at_high:
                z = self.T - z
            if 0.01 < z < self.T - 0.01:
                best = z if best is None else best
        return best

    def z_range(self, u_paper, tol=0.004):
        """model z range (0 = bottom face) covered by feature lines at plan coordinate u"""
        spans = [(a, b) for u, a, b in self.lines if abs(u - u_paper) < tol]
        if not spans:
            return None
        a = min(s[0] for s in spans); b = max(s[1] for s in spans)
        za, zb = (a - self.t0) * self.scale, (b - self.t0) * self.scale
        if not self.top_at_high:
            za, zb = self.T - zb, self.T - za
        return max(0.0, za), min(self.T, zb)


def _cut_depth(readers, u_of, side_hint, T):
    """(side, depth) of a cut-out spanning plan bounds; u_of(reader) -> (u1, u2) in paper units"""
    for rdr in readers:
        u1, u2 = u_of(rdr)
        z = rdr.floor(u1, u2)
        if z is not None:
            if side_hint == "bottom":
                return "bottom", z
            if side_hint == "top":
                return "top", T - z
            a, b = rdr.z_range(u1), rdr.z_range(u2)
            touches_top = any(r and r[1] > T - 0.01 for r in (a, b))
            return ("top", T - z) if touches_top else ("bottom", z)
    return "thru", T


def _material_face(fs):
    """the face whose inner rings are the part's features. Edge-break chamfers draw a second outline just
    inside the first; the thin band between them is skipped."""
    body = max(fs, key=lambda f: Polygon(f.exterior).area)
    for _ in range(4):
        big = [r for r in body.interiors if Polygon(r).area > 0.8 * Polygon(body.exterior).area]
        if not big:
            break
        inner = Polygon(big[0])
        nxt = [f for f in fs if f is not body and abs(Polygon(f.exterior).area - inner.area) < 1e-3 * inner.area + 1e-6]
        if not nxt:
            break
        body = nxt[0]
    return body


def _open_pocket(fs, body, readers):
    """_material_face skips a second outline just inside the first as an edge-break chamfer. When the side
    view shows a hidden floor across it, it is a pocket instead (a box / enclosure with thin walls):
    returns the outer face, whose big inner ring is then cut as a pocket. None otherwise."""
    outer = max(fs, key=lambda f: Polygon(f.exterior).area)
    if body is outer:
        return None
    big = [r for r in outer.interiors if Polygon(r).area > 0.8 * Polygon(outer.exterior).area]
    if not big:
        return None
    x0, y0, x1, y1 = Polygon(big[0]).bounds
    for rdr in readers:
        u1, u2 = (x0, x1) if rdr.horizontal else (y0, y1)
        z = rdr.floor(u1, u2, hidden=True)
        if z is not None and z < 0.9 * rdr.T:
            return outer
    return None


def _bosses(plan, pocket, readers, cuts, scale, M):
    """round posts standing on a pocket floor: circles inside the pocket whose edges run, in the side
    view, from the floor up to a top below the open face; smaller circles on the same centre are holes
    drilled from the post's top, as deep as their hidden lines go."""
    ring = Polygon(max(pocket.interiors, key=lambda r: Polygon(r).area))
    T = readers[0].T
    floor = min((c.depth for c in cuts if c.side == "top"), default=None)
    if floor is None:
        return []
    z_floor = T - floor
    groups = []
    for p in plan.prims:
        if p.kind == "circle" and ring.contains(Point(p.cx, p.cy)):
            g = next((g for g in groups if math.dist(g[0], (p.cx, p.cy)) < 0.003), None)
            if g is None:
                groups.append(((p.cx, p.cy), [p.r]))
            else:
                g[1].append(p.r)
    out = []
    for (cx, cy), rs in groups:
        rs = sorted(set(round(r, 4) for r in rs))
        R = rs[-1]
        top = None
        for rdr in readers:
            u = cx if rdr.horizontal else cy
            a, b = rdr.z_range(u - R), rdr.z_range(u + R)
            if a and b and abs(min(a[0], b[0]) - z_floor) < 0.02 and max(a[1], b[1]) < T - 0.01:
                top = max(a[1], b[1]); break
        if top is None:
            continue
        holes = []
        for r in rs[:-1]:
            for rdr in readers:
                u = cx if rdr.horizontal else cy
                a, b = rdr.z_range(u - r), rdr.z_range(u + r)
                if a and b:
                    holes.append((r * scale, top - min(a[0], b[0]))); break
        x, y = M((cx, cy))
        out.append((x, y, R * scale, z_floor, top, holes))
    return out


# ---------------------------------------------------------------- recognition
def _round_end_view(plan, side):
    """a plan view that is nothing but concentric circles is the end view of a round part (bushing,
    washer, flange): the profile to spin is the side view as wide as its biggest circle. Returns
    (profile view, its side views) or None."""
    if not plan.prims or any(p.kind != "circle" for p in plan.prims):
        return None
    big = max(plan.prims, key=lambda p: p.r)
    if not all(math.dist((p.cx, p.cy), (big.cx, big.cy)) < 0.003 for p in plan.prims):
        return None
    flip = dict(above="below", below="above", left="right", right="left")
    for w, v in side:
        across = v.w if w in ("above", "below") else v.h
        if abs(across - 2 * big.r) < 0.01:
            return v, [(flip[w], plan)] + [(w2, v2) for w2, v2 in side if v2 is not v]
    return None


def recognize(plan, side, bottom, scale, snap=None):
    snap = snap or SNAP
    if bottom is None and (rv := _round_end_view(plan, side)):
        return recognize(rv[0], rv[1], None, scale, snap)
    notes = []
    readers = [SideReader(w, v, plan, scale) for w, v in side]
    rd = min(readers, key=lambda r: -len(r.lines))
    T = round(rd.T / snap) * snap
    ox, oy = plan.x0, plan.y0
    M = lambda p: (round((p[0] - ox) * scale / snap) * snap, round((p[1] - oy) * scale / snap) * snap)

    # outline and inner boundaries of the plan view
    fs = faces([p for p in plan.prims])
    if not fs:
        raise ValueError("plan view does not close into an outline")
    outer = unary_union(fs)
    if outer.geom_type == "MultiPolygon":
        outer = max(outer.geoms, key=lambda g: g.area)
    body = _material_face(fs)
    pocket = _open_pocket(fs, body, readers)
    if pocket is not None:
        body = pocket
    circles = [(p.cx, p.cy, p.r) for p in plan.prims if p.kind in ("circle", "arc")]
    ctol = 0.002
    outline = [(k, *[M(q) for q in pts]) for k, *pts in to_segments(list(outer.exterior.coords), circles, ctol)]

    # holes: concentric circles (top view visible, bottom view visible mirrored)
    groups = []
    def add_circle(cx, cy, r, side, at=None):
        at = at or (cx, cy)                     # where it is drawn (bottom-view circles are drawn elsewhere)
        for g in groups:
            if math.dist((g["x"], g["y"]), (cx, cy)) < 0.003:
                g["r"].append((r, side)); g["at"].append((at, r)); return
        groups.append(dict(x=cx, y=cy, r=[(r, side)], at=[(at, r)]))
    for p in plan.prims:
        if p.kind == "circle" and Point(p.cx, p.cy).within(body.buffer(1e-3)) and p.r < 0.45 * min(plan.w, plan.h):
            add_circle(p.cx, p.cy, p.r, "top")
    # a circle some exporters draw as arcs (a quarter and three quarters, say): arcs on one centre and radius
    # that go all the way round are a circle too
    vis_arcs = {}
    for p in plan.prims:
        if p.kind == "arc" and p.r < 0.45 * min(plan.w, plan.h) and Point(p.cx, p.cy).within(outer):
            vis_arcs.setdefault((round(p.cx / 0.002), round(p.cy / 0.002), round(p.r / 0.001)), []).append(p)
    for ps_ in vis_arcs.values():
        turn = 0.0
        for p in ps_:
            angs = [math.atan2(y - p.cy, x - p.cx) for x, y in p.pts]
            turn += sum(abs((b - a + math.pi) % (2 * math.pi) - math.pi) for a, b in zip(angs, angs[1:]))
        if turn > math.radians(350) and len(ps_) > 1:
            p = ps_[0]
            add_circle(p.cx, p.cy, p.r, "top")
    # hidden circles (dashed arcs in the plan) are features on the far face: counterbores / holes from below
    arcs = {}
    for p in plan.thin:
        if p.kind in ("arc", "circle") and p.r < 0.45 * min(plan.w, plan.h) and Point(p.cx, p.cy).within(body.buffer(1e-3)):
            k = (round(p.cx / 0.002), round(p.cy / 0.002), round(p.r / 0.001))
            arcs.setdefault(k, []).append(p)
    for k, ps_ in arcs.items():
        sweep = 0.0
        for p in ps_:
            if p.kind == "circle":
                sweep = 360.0; break
            sweep += abs(((p.a1 - p.a0) + 180) % 360 - 180)
        # dashed circles come as several short arcs; a centre-mark or thread arc is a single 270 deg arc
        if sweep >= 200 and (len(ps_) >= 3 or ps_[0].kind == "circle"):
            p = ps_[0]
            hit = next((g for g in groups if math.dist((g["x"], g["y"]), (p.cx, p.cy)) < 0.003), None)
            if hit is not None or True:
                add_circle(p.cx, p.cy, p.r, "bottom")
    # circles drawn as arcs or polylines show up as round inner rings of the material face
    for ring in body.interiors:
        c = circle_of_ring(ring, 0.0015)
        if c and c[2] < 0.45 * min(plan.w, plan.h):
            add_circle(c[0], c[1], c[2], "top")
    if bottom is not None:
        fb0 = faces([p for p in bottom.prims])
        if fb0:
            for ring in _material_face(fb0).interiors:
                c = circle_of_ring(ring, 0.0015)
                if c and c[2] < 0.45 * min(plan.w, plan.h):
                    add_circle(c[0] - bottom.x0 + plan.x0, plan.y1 - (c[1] - bottom.y0), c[2], "bottom", (c[0], c[1]))
        for p in bottom.prims:
            if p.kind == "circle":
                # bottom view = plan mirrored top-to-bottom
                add_circle(p.cx - bottom.x0 + plan.x0, plan.y1 - (p.cy - bottom.y0), p.r, "bottom", (p.cx, p.cy))

    holes = []
    for g in groups:
        rs = sorted(set((round(r, 4), s) for r, s in g["r"]))
        steps = []
        # merge radii seen from both sides
        radii = sorted(set(r for r, _ in rs))
        rmin = radii[0]
        CH = 0.07 / scale        # a ring this close to the drill with no depth info = edge chamfer
        for r in radii:
            sides = {s for rr, s in rs if rr == r}
            zr = None
            for rdr in readers:
                if not rdr.horizontal:
                    u1, u2 = g["y"] - r, g["y"] + r
                else:
                    u1, u2 = g["x"] - r, g["x"] + r
                a, b = rdr.z_range(u1), rdr.z_range(u2)
                if a and b:
                    zr = (max(a[0], b[0]), min(a[1], b[1])); break
            R = r * scale
            if r == rmin and (zr is None or (zr[0] < 0.01 and zr[1] > T - 0.01)):
                steps.append((R, "thru", T))
            elif zr is not None and zr[1] > T - 0.01 and zr[0] > 0.01:
                steps.append((R, "top", T - zr[0]))
            elif zr is not None and zr[0] < 0.01 and zr[1] < T - 0.01:
                steps.append((R, "bottom", zr[1]))
            elif zr is not None:
                steps.append((R, "thru", T))
            elif r - rmin < CH:
                continue
            else:
                side_ = "bottom" if sides == {"bottom"} else "top"
                steps.append((R, side_, None))
                notes.append(f"hole at ({(g['x']-ox)*scale:.3f}, {(g['y']-oy)*scale:.3f}) R{R:.4f}: depth not found in the side view")
        # a drill that stops short of the far face with nothing else seen: look in the side view for a
        # wider bore coming from the far face (a counterbore hidden in the plan view)
        if steps and len(steps) == 1 and steps[0][1] in ("top", "bottom") and steps[0][2] and steps[0][2] < T - 0.01:
            r0, s0, d0 = steps[0]
            z_stop = T - d0 if s0 == "top" else d0               # where the drill ends
            for rdr in readers:
                u0 = g["x"] if rdr.horizontal else g["y"]
                best = None
                for k in range(1, 60):
                    rr = r0 / scale + k * 0.002
                    a, b = rdr.z_range(u0 - rr, tol=0.0015), rdr.z_range(u0 + rr, tol=0.0015)
                    if a and b:
                        lo, hi = max(a[0], b[0]), min(a[1], b[1])
                        if s0 == "top" and lo < 0.01 and abs(hi - z_stop) < 0.01:
                            best = (rr * scale, "bottom", hi); break
                        if s0 == "bottom" and hi > T - 0.01 and abs(lo - z_stop) < 0.01:
                            best = (rr * scale, "top", T - lo); break
                if best:
                    # exact radius from the side-view lines found
                    rr = best[0] / scale
                    us = [u for u, a, b in rdr.lines if min(abs(u - (u0 - rr)), abs(u - (u0 + rr))) < 0.003]
                    if us:
                        rr = sum(abs(u - u0) for u in us) / len(us)
                    steps = [(r0, "thru", T), (round(rr * scale / SNAP) * SNAP, best[1], best[2])]
                    break
        # a drill seen from one face that meets a counterbore from the other face goes right through
        if steps:
            r0, s0, d0 = min(steps)
            for R, sd, d in steps:
                if R > r0 and s0 in ("top", "bottom") and sd in ("top", "bottom") and sd != s0 and d and d0 and d + d0 >= T - 0.01:
                    steps[steps.index(min(steps))] = (r0, "thru", T)
                    break
        holes.append(Hole(*M((g["x"], g["y"])), steps, paper=(g["x"], g["y"]), radii_paper=[r for r, _ in g["r"]], drawn=g["at"]))

    # inner boundaries that are not circles: cut-outs / pockets
    cuts = []
    for ring in body.interiors:
        poly = Polygon(ring)
        c = circle_of_ring(ring, 0.0015)
        if c and any(math.dist((g["x"], g["y"]), c[:2]) < 0.003 for g in groups):
            continue
        if Polygon(ring).area < 1e-5:
            continue
        segs = [(k, *[M(q) for q in pts]) for k, *pts in to_segments(list(ring.coords), circles, ctol)]
        x0, y0, x1, y1 = poly.bounds
        side_, d = _cut_depth(readers, lambda r: (y0, y1) if not r.horizontal else (x0, x1), "top", T)
        cuts.append(Cut(segs, side_, d))
    if bottom is not None:
        fb = faces([p for p in bottom.prims])
        if fb:
            bbody = _material_face(fb)
            mir = lambda q: M((q[0] - bottom.x0 + plan.x0, plan.y1 - (q[1] - bottom.y0)))
            bcircles = [(p.cx, p.cy, p.r) for p in bottom.prims if p.kind in ("circle", "arc")]
            for ring in bbody.interiors:
                c = circle_of_ring(ring, 0.0015)
                if c or Polygon(ring).area < 1e-5:
                    continue
                segs = [(k, *[mir(q) for q in pts]) for k, *pts in to_segments(list(ring.coords), bcircles, ctol)]
                # mirrored ring runs the other way; depth from the side views at the mirrored extent
                poly = Polygon([mir(q) for q in ring.coords])
                x0, y0, x1, y1 = poly.bounds
                bx0, by0, bx1, by1 = x0 / scale + ox, y0 / scale + oy, x1 / scale + ox, y1 / scale + oy
                side_, d = _cut_depth(readers, lambda r: (by0, by1) if not r.horizontal else (bx0, bx1), "bottom", T)
                cuts.append(Cut(segs, side_, d))
    pm = PartModel(outline, T, holes, cuts, notes)
    if pocket is not None:
        pm.bosses = _bosses(plan, pocket, readers, cuts, scale, M)
        notes.append(f"pocket open at the top" + (f", {len(pm.bosses)} boss(es) in it" if pm.bosses else ""))
    pm.outline_poly = outer
    # a turned part: its end view is a set of concentric circles as wide as the plan view
    for rdr in readers:
        circ = [p for p in rdr.v.prims if p.kind in ("circle", "arc")]
        if circ:
            big = max(circ, key=lambda p: p.r)
            if abs(2 * big.r - max(rdr.v.w, rdr.v.h)) > 0.01:
                continue
            c0 = (big.cx if rdr.horizontal else big.cy)
            # radii of the outline's steps (silhouette edges seen end-on are circles too, not bores)
            steps_r = set()
            for q in plan.prims:
                if q.kind == "line":
                    for xx, yy in q.pts:
                        steps_r.add(round(abs((xx if rdr.horizontal else yy) - c0), 3))
            width = (plan.x1 - plan.x0) if rdr.horizontal else (plan.y1 - plan.y0)
            lo, hi = (plan.x0, plan.x1) if rdr.horizontal else (plan.y0, plan.y1)
            # as wide as the plan, or (a stepped shaft) as wide as one of its steps, on the plan's centre line
            fits = abs(2 * big.r - width) < 0.01 or (
                abs(c0 - (lo + hi) / 2) < 0.01 and any(abs(big.r - r) < 0.003 for r in steps_r))
            if fits and all(math.dist((p.cx, p.cy), (big.cx, big.cy)) < 0.003 for p in circ):
                u = c0
                axis = (u - (plan.x0 if rdr.horizontal else plan.y0)) * scale
                bores = []
                for p in circ:
                    if p.r < big.r - 0.01 and not any(abs(p.r - r) < max(0.0015, 0.0025 * p.r) for r in steps_r):
                        # depth: plan lines at axis +/- r running in from the end facing this view
                        depth = None
                        for q in plan.thin + plan.prims:
                            if q.kind != "line":
                                continue
                            (ax, ay), (bx, by) = q.pts[0], q.pts[-1]
                            if rdr.horizontal and abs(ax - bx) < 1e-3 and min(abs(ax - (u - p.r)), abs(ax - (u + p.r))) < 0.003:
                                end = plan.y1 if rdr.where == "above" else plan.y0
                                near, far = sorted((abs(ay - end), abs(by - end)))
                                if near < 0.1:          # a chamfer at the mouth leaves a small gap
                                    depth = max(depth or 0, far)
                        bores.append((p.r * scale, (depth or p.r * 4) * scale, rdr.where, depth is not None))
                # a circle just outside a bore with no hidden lines of its own is the bore's chamfer edge:
                # keep it as the width of a 45 deg chamfer at the bore's mouth (the end facing this view)
                kept = []
                for b in bores:
                    if b[3]:
                        cham = max((o[0] - b[0] for o in bores
                                    if not o[3] and 0 < o[0] - b[0] < 0.1 * scale), default=0.0)
                        kept.append((*b[:3], cham))
                    elif not any(o[3] and 0 < b[0] - o[0] < 0.1 * scale for o in bores):
                        kept.append((*b[:3], 0.0))
                bores = kept
                pm.turned = (axis, "x" if rdr.horizontal else "y", bores)
                keys = _keyways(pm, plan, rdr, big, scale)
                pm.holes, pm.side_holes, pm.cuts = [], [], keys
                notes.append(f"turned part (round) - {len(bores)} axial bore(s)"
                             + (f", {len(keys)} keyway(s)" if keys else ""))
                return pm
    _steps(pm, plan, readers, fs, body, outer, scale, M, circles, ctol)
    _grooves(pm, plan, readers, fs, body, outer, scale, M, circles, ctol)
    _rounds(pm, plan, readers, scale, M)
    _side_holes(pm, plan, readers, outer, scale, M)
    _hidden_edge_holes(pm, plan, outer, scale, M)
    return pm


def _keyways(pm, plan, rdr, big, scale):
    """slots seen face-on in the plan of a round part (keyways): their depth is the flat floor in the end
    view, a straight line inside the circle across the slot's width. Returns Cuts whose depth is the
    floor's height above the axis (model units); the side says which way the slot faces."""
    out = []
    lines = [p for p in rdr.v.prims if p.kind == "line"]
    for c in pm.cuts:
        xs = [q[0] for s in c.ring for q in s[1:]]
        ys = [q[1] for s in c.ring for q in s[1:]]
        a, b = (min(xs), max(xs)) if rdr.horizontal else (min(ys), max(ys))
        a0 = a / scale + (plan.x0 if rdr.horizontal else plan.y0)     # slot edges on the sheet
        b0 = b / scale + (plan.x0 if rdr.horizontal else plan.y0)
        floor = None
        for p in lines:
            (px, py), (qx, qy) = p.pts[0], p.pts[-1]
            if rdr.horizontal:      # end view above / below: the floor is a horizontal line from a0 to b0
                if abs(py - qy) < 1e-3 and abs(min(px, qx) - a0) < 0.01 and abs(max(px, qx) - b0) < 0.01:
                    floor = py - big.cy
            else:                   # end view beside: a vertical line from a0 to b0
                if abs(px - qx) < 1e-3 and abs(min(py, qy) - a0) < 0.01 and abs(max(py, qy) - b0) < 0.01:
                    floor = px - big.cx
        if floor is None:
            continue
        # third angle: in a view on the right (or above) the side towards the viewer of the plan is its
        # left (or bottom); in a view on the left (or below) it is the right (or top)
        toward = floor < 0 if rdr.where in ("right", "above") else floor > 0
        h = abs(floor) * scale
        out.append(Cut(c.ring, "top" if toward else "bottom", h))
    return out


def _hidden_edge_holes(pm, plan, outer, scale, M):
    """holes drilled into an edge that no view looks at: in the plan they are two parallel hidden lines
    running in from the outline, one hole diameter apart. Placed at mid-thickness."""
    import numpy as np
    T = pm.thickness
    coords = list(outer.exterior.simplify(2e-4).coords)          # one segment per straight edge
    thin = [p for p in plan.thin if p.kind == "line"]
    found = 0
    for (ax, ay), (bx, by) in zip(coords, coords[1:]):
        L = math.dist((ax, ay), (bx, by))
        if L < 0.08:
            continue
        tx, ty = (bx - ax) / L, (by - ay) / L
        nx, ny = ty, -tx                                    # one of the normals; pick the one pointing inside
        mid = (ax + tx * L / 2 + nx * 0.01, ay + ty * L / 2 + ny * 0.01)
        if not outer.contains(Point(mid)):
            nx, ny = -nx, -ny
        spans = []
        for p in thin:
            (px, py), (qx, qy) = p.pts[0], p.pts[-1]
            dx, dy = qx - px, qy - py
            ln = math.hypot(dx, dy)
            if ln < 1e-4 or abs(dx * tx + dy * ty) / ln > 0.03:     # must run perpendicular to the edge
                continue
            tau = (px - ax) * tx + (py - ay) * ty
            if tau < 0.01 or tau > L - 0.01:
                continue
            n0 = (px - ax) * nx + (py - ay) * ny; n1 = (qx - ax) * nx + (qy - ay) * ny
            spans.append((tau, min(n0, n1), max(n0, n1)))
        spans.sort(key=lambda s: (round(s[0] / 0.002), s[1]))
        merged = []
        for tau, a, b in spans:
            if merged and abs(merged[-1][0] - tau) < 0.002 and a <= merged[-1][2] + 0.07:
                merged[-1] = (merged[-1][0], min(merged[-1][1], a), max(merged[-1][2], b))
            else:
                merged.append((tau, a, b))
        runs = [m for m in merged if -0.005 < m[1] < 0.03 and m[2] - m[1] > 0.04]
        cand = []
        for i in range(len(runs)):
            for j in range(i + 1, len(runs)):
                (t1, a1, b1), (t2, a2, b2) = runs[i], runs[j]
                sp = t2 - t1
                if sp * scale > 0.6 or sp * scale < 0.05 or abs(b1 - b2) > 0.01:
                    continue
                if any(t1 + 0.002 < t < t2 - 0.002 and abs(t - (t1 + t2) / 2) > 0.003 for t, a, b in runs):
                    continue                                   # other lines in between: not one hole (a centre line is fine)
                cand.append((sp, i, j))
        used = set()
        for sp, i, j in sorted(cand):                          # narrowest pairs first: a tapped hole's minor diameter
            if i in used or j in used:
                continue
            (t1, a1, b1), (t2, a2, b2) = runs[i], runs[j]
            used.update((i, j))
            # lines just outside this pair are the same hole drawn at its thread size
            used.update(k for k, (t, a, b) in enumerate(runs) if min(abs(t - t1), abs(t - t2)) < 0.012)
            tc = (t1 + t2) / 2
            if sp * scale >= T:
                continue                                       # wider than the part is thick: not a hole
            ex, ey = M((ax + tx * tc, ay + ty * tc))
            depth = (b1 + b2) / 2 * scale
            dup = next((h for h in pm.side_holes if math.dist(h.entry, (ex, ey)) < 0.02), None)
            if dup is not None:
                if dup.depth < depth:
                    dup.depth = depth                          # the plan view shows the full depth
                continue
            # a hole already found on the same axis line (from a side view, or this pair of hidden lines seen
            # from the wall's other face) is the same hole: the side view gives its true height
            mx, my = M((ax + tx * tc + nx, ay + ty * tc + ny))
            dmx, dmy = mx - ex, my - ey
            dl = math.hypot(dmx, dmy) or 1.0
            dmx, dmy = dmx / dl, dmy / dl
            def _on_axis(h):
                hx, hy = M((0, 0)); kx, ky = M(h.direction)
                hdx, hdy = kx - hx, ky - hy
                hl = math.hypot(hdx, hdy) or 1.0
                if abs(dmx * hdy - dmy * hdx) / hl > 0.02:     # not parallel
                    return False
                return abs((h.entry[0] - ex) * dmy - (h.entry[1] - ey) * dmx) < 0.02
            if any(_on_axis(h) for h in pm.side_holes):
                continue
            pc = (ax + tx * tc + nx * depth / scale / 2, ay + ty * tc + ny * depth / scale / 2)
            pm.side_holes.append(SideHole((ex, ey), (nx, ny), T / 2, sp * scale / 2, depth, paper=pc))
            found += 1
    if found:
        pm.notes.append(f"{found} edge hole(s) found from hidden lines only: placed at mid-thickness, check the height")


def _zmodel(rdr, t):
    z = (t - rdr.t0) * rdr.scale
    return z if rdr.top_at_high else rdr.T - z


def _steps(pm, plan, readers, fs, body, outer, scale, M, circles, ctol):
    """stepped thickness: the plate is `base` thick and some plan faces rise higher. A raised face is one whose
    extent along the side view matches a flat (top edge) in that view higher than the base."""
    T = pm.thickness
    cands = []
    for rdr in readers:
        span = (rdr.v.x1 - rdr.v.x0) if rdr.horizontal else (rdr.v.y1 - rdr.v.y0)
        for t, a, b in rdr._merge(list(rdr.vflats), gap=0.01):
            z = _zmodel(rdr, t)
            if 0.05 < z < T - 0.06 and (b - a) > 0.5 * span:      # lines closer to a face are edge chamfers
                cands.append(z)
    if not cands:
        return
    base = round(max(cands) / SNAP) * SNAP
    raised = []
    for f in fs:
        if f.area < 0.03 * outer.area or f.area > 0.7 * outer.area:
            continue
        x0, y0, x1, y1 = f.bounds
        for rdr in readers:
            u0, u1 = (x0, x1) if rdr.horizontal else (y0, y1)
            for t, a, b in rdr._merge(list(rdr.vflats), gap=0.01):
                z = _zmodel(rdr, t)
                chamfer = 0.001 < T - z < 0.035            # edge-break line just under the top, not a level
                if z > base + 0.03 and not chamfer and abs(a - u0) < 0.004 and abs(b - u1) < 0.004:
                    raised.append((f, z)); break
            else:
                continue
            break
    if not raised:
        pm.notes.append(f"side view shows a step at z={base:.4f} but no plan area matches it - built {T:.4f} thick everywhere")
        return
    pm.base = base
    for f, z in raised:
        ring = [(k, *[M(q) for q in pts]) for k, *pts in to_segments(list(f.exterior.coords), circles, ctol)]
        inner = [[(k, *[M(q) for q in pts]) for k, *pts in to_segments(list(r.coords), circles, ctol)] for r in f.interiors]
        pm.raised.append(Raised(ring, inner, round(z / SNAP) * SNAP))
    pm.notes.append(f"stepped part: {base:.4f} thick, {len(raised)} raised area(s) up to {max(z for _, z in raised):.4f}")


def _dash_curves(prims, gap=0.06, ang=12.0):
    """hidden edges are drawn dashed: chain the short line pieces that line up (end to end, same direction)
    into whole curves. Returns [LineString], each through its pieces' end points in order along the curve."""
    segs = []
    for p in prims:
        if p.kind != "line":
            continue
        (ax, ay), (bx, by) = p.pts[0], p.pts[-1]
        L = math.hypot(bx - ax, by - ay)
        if L > 0.004:
            segs.append(((ax, ay), (bx, by), ((bx - ax) / L, (by - ay) / L)))
    n = len(segs)
    if n < 2:
        return []
    par = list(range(n))
    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]; i = par[i]
        return i
    s_ang = math.sin(math.radians(ang))
    order = sorted(range(n), key=lambda i: min(segs[i][0][0], segs[i][1][0]))
    for k, i in enumerate(order):
        pi, qi, di = segs[i]
        xi1 = max(pi[0], qi[0])
        for j in order[k + 1:]:
            pj, qj, dj = segs[j]
            if min(pj[0], qj[0]) > xi1 + gap:
                break
            if abs(di[0] * dj[1] - di[1] * dj[0]) > s_ang:
                continue                               # not the same direction
            e1, e2 = min(((a, b) for a in (pi, qi) for b in (pj, qj)), key=lambda e: math.dist(*e))
            d = math.dist(e1, e2)
            if d > gap:
                continue
            if d > 1e-4 and abs(di[0] * (e2[1] - e1[1]) - di[1] * (e2[0] - e1[0])) > s_ang * d + 0.002:
                continue                               # the gap does not continue the line
            par[find(i)] = find(j)
    comps = {}
    for i in range(n):
        comps.setdefault(find(i), []).append(i)
    out = []
    for idx in comps.values():
        if len(idx) < 3:
            continue
        pts = [q for i in idx for q in segs[i][:2]]
        xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
        horiz = (max(xs) - min(xs)) >= (max(ys) - min(ys))
        if not horiz:
            pts = [(q[1], q[0]) for q in pts]
        # a doubled (thick) dashed stroke chains as two rows: average across them in short bins along the curve
        pts.sort()
        lo, hi = pts[0][0], pts[-1][0]
        if hi - lo < 0.08:
            continue
        nb = max(2, int((hi - lo) / 0.02))
        bins = [[] for _ in range(nb + 1)]
        for u, v in pts:
            bins[min(nb, int((u - lo) / (hi - lo) * nb))].append((u, v))
        line = [(sum(q[0] for q in b) / len(b), sum(q[1] for q in b) / len(b)) for b in bins if b]
        if not horiz:
            line = [(v, u) for u, v in line]
        if len(line) >= 2:
            out.append(LineString(line))
    return out


def _extend(ls, d):
    """the line with both ends pushed out by d along their directions (dashes stop short of the edges)"""
    c = list(ls.coords)
    def push(pts):                             # direction over a stretch of the curve, not just the last piece
        b = pts[0]
        a = next((q for q in pts[1:] if math.dist(q, b) >= 0.05), pts[-1])
        L = math.dist(a, b) or 1.0
        return (b[0] + (b[0] - a[0]) / L * d, b[1] + (b[1] - a[1]) / L * d)
    return LineString([push(c)] + c + [push(c[::-1])])


def _hidden_bands(thin, outer, edge):
    """strips between neighbouring dashed curves that both cross the part from edge to edge, trimmed to
    the outline (the edges of a groove on the far face)"""
    curves = []
    for c in _dash_curves(thin):
        e = _extend(c, 0.04)
        if len([g for g in getattr(e.intersection(edge), "geoms", [e.intersection(edge)]) if not g.is_empty]) >= 2:
            cs = list(c.coords)
            horiz = abs(cs[-1][0] - cs[0][0]) >= abs(cs[-1][1] - cs[0][1])
            curves.append((horiz, c, e))
    def along(c, horiz):
        x0, y0, x1, y1 = c.bounds
        return (x0, x1) if horiz else (y0, y1)
    def across(c, horiz):
        return c.centroid.y if horiz else c.centroid.x
    bands = []
    for i, (h, a, ae) in enumerate(curves):
        a0, a1 = along(a, h)
        best = None
        for j, (h2, b, be) in enumerate(curves):
            if j == i or h2 != h:
                continue
            b0, b1 = along(b, h)
            if min(a1, b1) - max(a0, b0) < 0.8 * min(a1 - a0, b1 - b0):
                continue                               # not the same stretch of the part
            d = across(b, h) - across(a, h)
            if 0 < d < 0.5 and (best is None or d < best[0]):
                best = (d, be)
        if best is None:
            continue
        ring = list(ae.coords) + list(reversed(best[1].coords))
        try:
            band = Polygon(ring).buffer(0).intersection(outer)
        except Exception:
            continue
        if band.geom_type != "Polygon":
            band = max(getattr(band, "geoms", []), key=lambda g: g.area, default=None)
        if band is not None and not band.is_empty and band.area > 0:
            bands.append(band)
    return bands


def _grooves(pm, plan, readers, fs, body, outer, scale, M, circles, ctol):
    """grooves across the part: a plan band running from one edge of the outline to another (not an inner
    ring, so not a pocket) whose span in a side view holds a floor line between the faces. A groove on the
    near face is bounded by visible lines, one on the far face by dashed hidden lines. It opens on the face
    its side walls run up to; the floor is the middle of the floor line's (thick) stroke. Of the bands that
    match one floor only the widest are the groove (narrow ones are the tangent lines of edge rounds)."""
    T = pm.thickness
    if T <= 0:
        return
    edge = outer.exterior.buffer(0.006)
    cands = [(f, "visible") for f in fs if f is not body]
    cands += [(g, "hidden") for g in _hidden_bands(plan.thin, outer, edge)]
    matches = []
    for f, kind in cands:
        if f.area < 0.002 * outer.area or f.area > 0.3 * outer.area or list(f.interiors):
            continue
        touch = f.exterior.intersection(edge)
        pieces = [g for g in getattr(touch, "geoms", [touch]) if not g.is_empty and g.length > 0.004]
        if len(pieces) < 2:
            continue                                   # does not run edge to edge
        x0, y0, x1, y1 = f.bounds
        for ri, rdr in enumerate(readers):
            u0, u1 = (x0, x1) if rdr.horizontal else (y0, y1)
            hits = []
            for t, a, b in rdr._merge(list(rdr.vflats), gap=0.01):
                z = _zmodel(rdr, t)
                if not (0.035 < z < T - 0.035) or b - a < 0.3 * (u1 - u0):
                    continue
                ov = min(b, u1) - max(a, u0)
                if ov >= 0.6 * (b - a) and a > u0 - 0.015 and b < u1 + 0.015:
                    hits.append((z, a, b))
            if not hits:
                continue
            # the walls: lines across the thickness at the floor line's ends; which face do they reach?
            a = min(h[1] for h in hits); b = max(h[2] for h in hits)
            zr = [rdr.z_range(u, tol=0.006) for u in (a, b)]
            top = any(r and r[1] > T - 0.04 for r in zr)   # (thick strokes: a face line sits a little inside)
            bot = any(r and r[0] < 0.04 for r in zr)
            if top == bot:
                continue                               # walls reach both faces or neither: not a groove
            if (kind == "hidden") == top:
                continue                               # hidden edges belong to the far face, visible ones to the near
            zs = sorted(h[0] for h in hits)
            if top:                                    # deepest cluster seen from the top face
                cl = [z for z in zs if z < zs[0] + 0.03]
                side_, depth = "top", T - (min(cl) + max(cl)) / 2
            else:
                cl = [z for z in zs if z > zs[-1] - 0.03]
                side_, depth = "bottom", (min(cl) + max(cl)) / 2
            depth = round(depth / SNAP) * SNAP
            if not 0.03 < depth < T - 0.03:
                continue
            width = 2 * f.area / f.length              # about the band's width, for a long thin band
            matches.append(dict(f=f, kind=kind, key=(ri, round(a, 2), round(b, 2)), side=side_, depth=depth, width=width))
            break
    # a narrow band lying against a wider matched band is the tangent strip of an edge round, not a groove
    kept = []
    for m in sorted(matches, key=lambda m: -m["width"]):
        if not any(m["f"].distance(k["f"]) < 0.01 and m["width"] < 0.7 * k["width"] for k in kept):
            kept.append(m)
    found = []
    for m in kept:
        f = m["f"]
        if m["kind"] == "visible":
            # join the thin outline-stroke slivers at its ends, so no skin of material is left at the edge
            region = f
            for g in fs:
                if g is f or g is body or g.area > 0.15 * f.area:
                    continue
                if g.distance(f) < 1e-6 and g.exterior.intersection(edge).length > 0.5 * g.length / 2:
                    region = region.union(g)
        else:
            region = f.buffer(0.004, join_style=2)     # a hidden band stops at the outline's inner stroke edge
        if region.geom_type != "Polygon":
            region = max(region.geoms, key=lambda q: q.area)
        ring = [(k, *[M(q) for q in pts]) for k, *pts in to_segments(list(region.exterior.coords), circles, ctol)]
        pm.cuts.append(Cut(ring, m["side"], m["depth"]))
        found.append(m["depth"])
    if found:
        pm.notes.append(f"{len(found)} groove(s) across the part, {', '.join(f'{d:.4f}' for d in sorted(set(found)))} deep")


def _view_arcs(view, short=0.012, turn=25.0):
    """quarter-round arcs in a view: real arcs, or runs of tiny straight pieces (how many CAD exports draw
    them). A thick stroke is drawn as its outline, so each round shows up twice (inner and outer edge),
    joined by short square ends: a run stops where the direction turns sharply. [(cx, cy, r, mid_angle)]"""
    out = []
    def close(run):
        if len(run) < 4:
            return
        pts = [run[0].pts[0]] + [p.pts[-1] for p in run]
        cx, cy, r, rms = fit_circle(pts)
        if not (0.002 < r < 0.08) or rms > 0.05 * r:
            return
        a = [math.atan2(q[1] - cy, q[0] - cx) for q in pts]
        sweep = sum(((a[k + 1] - a[k]) + math.pi) % (2 * math.pi) - math.pi for k in range(len(a) - 1))
        if 70 <= abs(math.degrees(sweep)) <= 110:
            out.append((cx, cy, r, a[0] + sweep / 2))
    run = []
    for p in view.prims:
        if p.kind == "arc":
            sw = ((p.a1 - p.a0) + 360) % 360
            if 70 <= sw <= 110 and p.r < 0.08:
                out.append((p.cx, p.cy, p.r, math.radians(p.a0 + sw / 2)))
            close(run); run = []
            continue
        L = math.dist(p.pts[0], p.pts[-1]) if p.kind == "line" else 0
        if p.kind != "line" or not 1e-5 < L < short:
            close(run); run = []
            continue
        if run and run[-1].path == p.path and math.dist(run[-1].pts[-1], p.pts[0]) < 2e-4:
            (ax, ay), (bx, by) = run[-1].pts[0], run[-1].pts[-1]
            (cx_, cy_), (dx, dy) = p.pts[0], p.pts[-1]
            d1 = math.atan2(by - ay, bx - ax); d2 = math.atan2(dy - cy_, dx - cx_)
            if abs(math.degrees((d2 - d1 + math.pi) % (2 * math.pi) - math.pi)) < turn:
                run.append(p); continue
        close(run); run = [p]
    close(run)
    return out


def _rounds(pm, plan, readers, scale, M):
    """rounded edges seen end-on in a side view (the leg-end and groove rounds of Detail N): each quarter
    arc gives a radius and the corner it replaces; the build rounds the solid's edges along that corner.
    The stroke's two arcs are concentric: the true edge is the one on the empty side (the larger arc for a
    convex corner, the smaller for an inside corner)."""
    if pm.turned or pm.thickness <= 0:
        return
    seen = []
    for rdr in readers:
        arcs = _view_arcs(rdr.v)
        if not arcs:
            continue
        prof = unary_union(faces(rdr.v.prims))
        groups = []
        for a in arcs:
            g = next((g for g in groups if math.dist(g[0][:2], a[:2]) < 0.004), None)
            (g.append(a) if g is not None else groups.append([a]))
        for g in groups:
            cx, cy = sum(a[0] for a in g) / len(g), sum(a[1] for a in g) / len(g)
            mid = g[0][3]
            convex = prof.contains(Point(cx, cy))
            r = max(a[2] for a in g) if convex else min(a[2] for a in g)
            R = round(r * scale / SNAP) * SNAP
            if R < 0.004 or R > 0.3 * pm.thickness:
                continue
            k = r * math.sqrt(2)                       # the sharp corner the round replaces
            px, py = cx + k * math.cos(mid), cy + k * math.sin(mid)
            if rdr.horizontal:                         # view below/above: u = x, t = y; edges run along y
                t, along, u = py, "y", M((px, plan.y0))[0]
            else:                                      # view left/right: u = y, t = x; edges run along x
                t, along, u = px, "x", M((plan.x0, py))[1]
            z = _zmodel(rdr, t)
            z = min(max(z, 0.0), pm.thickness)
            if z < 0.03:                               # (thick strokes: a face line sits a little inside)
                z = 0.0
            elif z > pm.thickness - 0.03:
                z = pm.thickness
            key = (along, round(u, 3), round(z, 3), R)
            if key not in seen:
                seen.append(key)
                pm.rounds.append((along, u, z, R))


def _side_holes(pm, plan, readers, outer, scale, M):
    """circles in a side view are holes drilled into that edge of the part; depth from the plan view lines"""
    T = pm.thickness
    for rdr in readers:
        circ = [p for p in rdr.v.prims if p.kind == "circle"]
        if not circ:
            continue
        for p in circ:
            u = p.cx if rdr.horizontal else p.cy
            t = p.cy if rdr.horizontal else p.cx
            z = _zmodel(rdr, t)
            # where the hole enters: the outline edge facing this view
            from shapely.geometry import LineString as LS
            if rdr.horizontal:
                ray = LS([(u, plan.y0 - 1), (u, plan.y1 + 1)])
            else:
                ray = LS([(plan.x0 - 1, u), (plan.x1 + 1, u)])
            hit = outer.exterior.intersection(ray)
            pts = [hit] if hit.geom_type == "Point" else list(getattr(hit, "geoms", []))
            if not pts:
                continue
            key = {"above": lambda q: -q.y, "below": lambda q: q.y, "right": lambda q: -q.x, "left": lambda q: q.x}[rdr.where]
            e = min(pts, key=key)
            d = {"above": (0, -1), "below": (0, 1), "right": (-1, 0), "left": (1, 0)}[rdr.where]
            # depth: plan-view lines along the hole axis at u +/- r, starting at the entry
            depth = None
            r = p.r
            for q in plan.thin + plan.prims:
                if q.kind != "line":
                    continue
                (ax, ay), (bx, by) = q.pts[0], q.pts[-1]
                if rdr.horizontal and abs(ax - bx) < 1e-3 and min(abs(ax - (u - r)), abs(ax - (u + r))) < 0.003:
                    far = max(abs(ay - e.y), abs(by - e.y))
                    near = min(abs(ay - e.y), abs(by - e.y))
                    if near < 0.03:
                        depth = max(depth or 0, far)
                if not rdr.horizontal and abs(ay - by) < 1e-3 and min(abs(ay - (u - r)), abs(ay - (u + r))) < 0.003:
                    far = max(abs(ax - e.x), abs(bx - e.x)); near = min(abs(ax - e.x), abs(bx - e.x))
                    if near < 0.03:
                        depth = max(depth or 0, far)
            if depth is None:
                pm.notes.append(f"side hole R{r * scale:.4f} at z={z:.3f}: depth not found, made 2 x diameter")
                depth = 4 * r
            ex, ey = M((e.x, e.y))
            pm.side_holes.append(SideHole((ex, ey), d, z, r * scale, depth * scale, paper=(p.cx, p.cy)))
