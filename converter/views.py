"""Group visible geometry into views and find which views belong together (third-angle projection)."""
import math
import numpy as np
from extract import Prim


def _union_find(n):
    par = list(range(n))
    def f(i):
        while par[i] != i:
            par[i] = par[par[i]]; i = par[i]
        return i
    def u(a, b):
        a, b = f(a), f(b)
        if a != b:
            par[a] = b
    return f, u


class View:
    def __init__(self, prims):
        self.prims = prims
        a = np.array([p.bbox() for p in prims])
        self.x0, self.y0 = a[:, 0].min(), a[:, 1].min()
        self.x1, self.y1 = a[:, 2].max(), a[:, 3].max()
        self.thin = []          # thin-line prims inside the view box (hidden + centre + some dims)
        n_slant = sum(1 for p in prims if p.kind == "line" and _slanted(p))
        n_lines = sum(1 for p in prims if p.kind == "line")
        # isometric: long edges in three directions, about 30, 90 and 150 deg (a view merely turned on the
        # sheet has only two, at right angles)
        tot = sum(_len(p) for p in prims if p.kind == "line" and _len(p) > 0.03) or 1.0
        def share(a0):
            return sum(_len(p) for p in prims if p.kind == "line" and _len(p) > 0.03 and
                       abs(((_ang(p) - a0) + 90) % 180 - 90) < 2.5) / tot
        self.iso = n_lines > 10 and share(30) > 0.12 and share(150) > 0.12
        self.role = ""

    @property
    def w(self): return self.x1 - self.x0
    @property
    def h(self): return self.y1 - self.y0

    def __repr__(self):
        return f"View({self.role or '?'} {self.x0:.2f},{self.y0:.2f} {self.w:.2f}x{self.h:.2f} n={len(self.prims)}{' iso' if self.iso else ''})"


def _ang(p):
    (x0, y0), (x1, y1) = p.pts[0], p.pts[-1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def _len(p):
    return math.dist(p.pts[0], p.pts[-1])


def _slanted(p):
    a = abs(_ang(p)) % 90
    return 2 < a < 88 and math.dist(p.pts[0], p.pts[-1]) > 0.05


def find_views(prims, page_size, gap=0.08):
    """connected groups of visible geometry (touching within `gap` inches) = views; the sheet border,
    title block and tables are dropped (they span a large part of the page or are tables of straight lines)."""
    vis = [p for p in prims if p.cls == "visible"]
    if not vis:
        return []
    boxes = [p.bbox() for p in vis]           # plain lists: much faster than numpy for one value at a time
    f, u = _union_find(len(vis))
    order = sorted(range(len(vis)), key=lambda i: boxes[i][0])
    # sweep in x to join boxes that overlap (expanded by gap); pairs already in one group need no test
    active = []
    for i in order:
        bx0, by0, _, by1 = boxes[i]
        x0 = bx0 - 2 * gap
        active = [j for j in active if boxes[j][2] >= x0]
        ri = f(i)
        for j in active:
            b = boxes[j]
            if b[1] - gap <= by1 and by0 - gap <= b[3] and f(j) != ri and _touch(vis[i], vis[j], gap):
                u(i, j)
                ri = f(i)
        active.append(i)
    groups = {}
    for i in range(len(vis)):
        groups.setdefault(f(i), []).append(vis[i])
    views = [View(g) for g in groups.values() if len(g) >= 1]
    # a group lying inside a bigger group's box (an isolated hole, a symbol) is part of that view
    views.sort(key=lambda v: -v.w * v.h)
    merged = []
    for v in views:
        host = next((m for m in merged if v.x0 >= m.x0 - 1e-3 and v.x1 <= m.x1 + 1e-3 and v.y0 >= m.y0 - 1e-3 and v.y1 <= m.y1 + 1e-3), None)
        if host:
            host.prims.extend(v.prims)
        else:
            merged.append(v)
    views = [View(v.prims) for v in merged if len(v.prims) >= 4]
    W, H = page_size
    views = [v for v in views if not (v.w > 0.6 * W or v.h > 0.6 * H) and v.w * v.h > 0.01]
    # thin lines inside each view box (with margin) belong to it
    thin = [p for p in prims if p.cls == "thin"]
    for v in views:
        m = 0.02
        v.thin = [p for p in thin if _inside(p, v.x0 - m, v.y0 - m, v.x1 + m, v.y1 + m)]
    return sorted(views, key=lambda v: -v.w * v.h)


def _inside(p, x0, y0, x1, y1):
    a, b, c, d = p.bbox()
    return a >= x0 and b >= y0 and c <= x1 and d <= y1


def _pts(p):
    if p.kind == "circle":
        return [(p.cx + p.r * math.cos(t), p.cy + p.r * math.sin(t)) for t in np.linspace(0, 2 * math.pi, 9)]
    return p.pts


def _touch(a, b, gap):
    pa, pb = _pts(a), _pts(b)
    if a.kind == "line" and b.kind == "line":
        return _seg_seg(pa[0], pa[1], pb[0], pb[1]) <= gap
    return min(math.dist(p, q) for p in pa for q in pb) <= gap or \
        (a.kind == "circle" and b.kind == "circle" and math.dist((a.cx, a.cy), (b.cx, b.cy)) < max(a.r, b.r))


def _seg_seg(a, b, c, d):
    def pd(p, a, b):
        ax, ay = b[0] - a[0], b[1] - a[1]
        L = ax * ax + ay * ay
        t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / L))
        return math.dist(p, (a[0] + t * ax, a[1] + t * ay))
    def inter(a, b, c, d):
        def o(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return o(a, b, c) * o(a, b, d) < 0 and o(c, d, a) * o(c, d, b) < 0
    if inter(a, b, c, d):
        return 0.0
    return min(pd(a, c, d), pd(b, c, d), pd(c, a, b), pd(d, a, b))


def pair_views(views, tol=0.03):
    """third-angle neighbours: a view directly below/above shares the x extent, one left/right shares y."""
    pairs = []
    for i, a in enumerate(views):
        for j, b in enumerate(views):
            if i >= j or a.iso or b.iso:
                continue
            if abs(a.x0 - b.x0) < tol and abs(a.x1 - b.x1) < tol and (b.y1 < a.y0 or a.y1 < b.y0):
                pairs.append(("v", a, b))
            if abs(a.y0 - b.y0) < tol and abs(a.y1 - b.y1) < tol and (b.x1 < a.x0 or a.x1 < b.x0):
                pairs.append(("h", a, b))
    return pairs


def dominant_angle(view):
    """angle (deg, -45..45] that the view's long straight edges are turned by; 0 for a normal view"""
    hist = {}
    for p in view.prims:
        if p.kind != "line":
            continue
        L = _len(p)
        if L < 0.05:
            continue
        a = _ang(p) % 90
        if a > 45:
            a -= 90
        k = round(a * 2) / 2
        hist[k] = hist.get(k, 0) + L
    if not hist:
        return 0.0
    k = max(hist, key=hist.get)
    # refine with a length-weighted mean of the lines in that bin
    num = den = 0.0
    for p in view.prims:
        if p.kind == "line" and _len(p) >= 0.05:
            a = _ang(p) % 90
            if a > 45:
                a -= 90
            if abs(a - k) <= 0.5:
                num += a * _len(p); den += _len(p)
    return num / den if den else k


def rotate_prims(prims, deg):
    """copies of prims turned by deg about the page origin"""
    import copy
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    R = lambda q: (q[0] * c - q[1] * s, q[0] * s + q[1] * c)
    out = []
    for p in prims:
        q = copy.copy(p)
        q.pts = [R(t) for t in p.pts]
        if p.kind in ("circle", "arc"):
            q.cx, q.cy = R((p.cx, p.cy))
            q.a0, q.a1 = p.a0 + deg, p.a1 + deg
        out.append(q)
    return out
