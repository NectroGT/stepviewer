"""Section views: a hatched view is the part's real cross-section along a cutting line on another view.
Where the section shows no material but the model built so far has some, that material is cut away:
- swept around a centre (revolved) when the cutting line runs through the centre of arcs on the plan and the
  empty area lies at those arcs' radii (ring seats, grooves, lips around a bore);
- otherwise cut straight across the cutting line, as wide as the nearest pair of plan lines either side of it."""
import math
import numpy as np
import cadquery as cq
from shapely.geometry import LineString, Polygon, box, Point
from shapely.ops import unary_union
from recognize import faces


def find_sections(prims, views, min_hatch=10):
    hatch = [p for p in prims if p.cls == "hatch" and p.kind == "line"]
    out = []
    for v in views:
        hs = [h for h in hatch if v.x0 - 0.01 <= h.pts[0][0] <= v.x1 + 0.01 and v.y0 - 0.01 <= h.pts[0][1] <= v.y1 + 0.01]
        if len(hs) < min_hatch:
            continue
        fs = faces(list(v.prims))
        mat = [f for f in fs if sum(1 for h in hs if f.buffer(-0.002).intersects(LineString(h.pts))) >= 1]
        if mat:
            out.append(dict(view=v, material=unary_union(mat), hatch=len(hs)))
    return out


def cutting_lines(prims, plan):
    """long straight heavy (section-line) strokes crossing the plan view: [(x or y, 'v'|'h')]"""
    segs = []
    for p in prims:
        if p.cls != "heavy" or p.kind != "line":
            continue
        (x0, y0), (x1, y1) = p.pts[0], p.pts[-1]
        if abs(x1 - x0) < 1e-3 and plan.x0 < x0 < plan.x1:
            segs.append(("v", round(x0, 3), abs(y1 - y0)))
        if abs(y1 - y0) < 1e-3 and plan.y0 < y0 < plan.y1:
            segs.append(("h", round(y0, 3), abs(x1 - x0)))
    tot = {}
    for k, u, L in segs:
        tot[(k, u)] = tot.get((k, u), 0) + L
    # a cutting line runs most of the way across the view (arrow stems at its ends are short)
    return [(u, k) for (k, u), L in tot.items() if L > 0.5 * (plan.y1 - plan.y0 if k == "v" else plan.x1 - plan.x0)]


def apply_sections(body, pm, plan, scale, prims, sections, snap=0.0005):
    """cut away what the sections show as empty. body: CadQuery Workplane in model units (origin = plan corner)."""
    T = pm.thickness
    notes = []
    for u, kind in cutting_lines(prims, plan):
        if kind != "v":
            continue                       # vertical cutting lines only for now
        xc = (u - plan.x0) * scale         # model x of the cut
        # the section view of this cut: its material is as wide as the part is thick
        sec = None
        for s in sections:
            M = s["material"]
            x0, y0, x1, y1 = M.bounds
            k = (x1 - x0) / T if T else 0  # paper per model unit in the section
            if k > 0 and abs((y1 - y0) / k) < (plan.y1 - plan.y0) * scale * 1.05:
                sec = (s, k); break
        if sec is None:
            continue
        s, k = sec
        M = s["material"]; mx0, my0, mx1, my1 = M.bounds
        # section coordinates -> model (y, z): top of the section = the plan's top edge at the cut; the arrow on
        # the cutting line looks left, so the left side of the section is the top face
        ytop = _top_at(body, xc)
        if ytop is None:
            continue
        to_model = lambda q: (ytop - (my1 - q[1]) / k, T - (q[0] - mx0) / k)
        Mm = _map(M, to_model)
        S = _slice(body, xc)
        if S is None or S.is_empty:
            continue
        voids = S.difference(Mm.buffer(0.003)).buffer(-0.002).buffer(0.002)
        arcs = [(p.cx, p.cy, p.r) for p in plan.prims + plan.thin if p.kind in ("arc", "circle") and abs(p.cx - u) < 0.01]
        centres = sorted({round((p[1] - plan.y0) * scale, 3) for p in arcs})
        n_rev = n_ext = 0
        for v in getattr(voids, "geoms", [voids]):
            if v.area < 1e-4:
                continue
            v = v.simplify(0.003, preserve_topology=True)      # fewer facets on the swept faces
            vy0, vz0, vy1, vz1 = v.bounds
            cy = next((c for c in centres
                       if any(abs(abs(y - c) - (p[2] * scale)) < 0.02 for y in (vy0, vy1) for p in arcs
                              if abs((p[1] - plan.y0) * scale - c) < 0.01)), None)
            try:
                if cy is not None:
                    prof = [(y - cy, z) for y, z in v.exterior.coords[:-1]]
                    if min(r for r, _ in prof) < -1e-6:      # profile must sit on one side of the axis
                        prof = [(abs(r), z) for r, z in prof]
                    ring = (cq.Workplane("XZ").polyline(prof).close()
                            .revolve(360, (0, 0, 0), (0, 1, 0)))
                    # XZ workplane: local x -> model x (radius), local y -> model z; revolve about the z axis
                    ring = ring.translate((xc, cy, 0))
                    body = body.cut(ring); n_rev += 1
                else:
                    w = _width(plan, u, (vy0 / scale + plan.y0, vy1 / scale + plan.y0), scale)
                    if w is None:
                        notes.append(f"section at x={xc:.3f}: an empty area at y {vy0:.3f}-{vy1:.3f} has no width on the plan - skipped")
                        continue
                    prism = (cq.Workplane("YZ").polyline(list(v.exterior.coords[:-1])).close()
                             .extrude(w).translate((xc - w / 2, 0, 0)))
                    body = body.cut(prism); n_ext += 1
            except Exception as e:
                notes.append(f"section cut failed ({type(e).__name__})")
        if n_rev or n_ext:
            notes.append(f"section at x={xc:.3f}: {n_rev} swept and {n_ext} straight cut(s) from the section view")
    pm.notes.extend(notes)
    return body


def _map(geom, f):
    from shapely.ops import transform
    return transform(lambda x, y, z=None: tuple(zip(*[f(q) for q in zip(np.atleast_1d(x), np.atleast_1d(y))])), geom)


def _slice(body, xc):
    """the model's cross-section at x = xc as a shapely polygon in (y, z)"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    from shapely.ops import polygonize
    sh = body.val()
    sec = BRepAlgoAPI_Section(sh.wrapped, gp_Pln(gp_Pnt(xc, 0, 0), gp_Dir(1, 0, 0)))
    sec.Build()
    edges = cq.Shape.cast(sec.Shape()).Edges()
    lines = []
    for e in edges:
        pts = [e.positionAt(i / 24) for i in range(25)] if e.geomType() != "LINE" else [e.startPoint(), e.endPoint()]
        q = [(round(p.y, 4), round(p.z, 4)) for p in pts]
        if len(set(q)) >= 2:
            lines.append(LineString(q))
    if not lines:
        return None
    polys = list(polygonize(unary_union(lines)))
    if not polys:
        return None
    # faces nested inside others are holes: keep the even-depth ones
    polys.sort(key=lambda p: -p.area)
    out = None
    for p in polys:
        depth = sum(1 for q in polys if q is not p and q.area > p.area and q.contains(p.representative_point()))
        if depth % 2 == 0:
            out = p if out is None else out.union(p)
        else:
            out = out.difference(p) if out is not None else out
    return out


def _top_at(body, xc):
    S = _slice(body, xc)
    return None if S is None or S.is_empty else S.bounds[2]


def _width(plan, u, yr, scale):
    """width of a straight feature across the cutting line: the nearest pair of plan lines parallel to the cut,
    one each side, spanning the feature's y range"""
    ya, yb = sorted(yr)
    lefts, rights = [], []
    for p in plan.prims + plan.thin:
        if p.kind != "line":
            continue
        (x0, y0), (x1, y1) = p.pts[0], p.pts[-1]
        if abs(x1 - x0) > 1e-3:
            continue
        lo, hi = sorted((y0, y1))
        if hi < ya - 0.01 or lo > yb + 0.01:
            continue
        d = x0 - u
        if d < -0.005:
            lefts.append(-d)
        elif d > 0.005:
            rights.append(d)
    for a in sorted(lefts):
        b = next((r for r in sorted(rights) if abs(r - a) < 0.004), None)
        if b is not None:
            return (a + b) * scale
    return None
