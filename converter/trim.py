"""Trim a part to the outlines of its other views.

The body is built from the top (plan) view, with heights read from a side view. Faces that slope across the
part - a hat-shaped profile, a V groove underneath, angled ends - do not show in the plan, but they are the
outlines of the front view and the end view. Each of those outlines, pushed straight through the part, is a
prism the part lies inside; the body is cut down to them. Holes and cut-outs already in the body stay.

Layout (third angle): the front view is below the plan and shares its x; the end view is beside the front
view (right or left of it) and shares its heights. Model axes: x along the plan, y across it (0 at the plan's
lower edge, the front face), z up (0 at the bottom).
"""
import math
import cadquery as cq
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Trsf
from shapely.ops import unary_union


def end_view(front, views, used, tol=0.03):
    """the view beside the front view at the same height (its end view): (view, "right" | "left") or None"""
    best = None
    for v in views:
        if id(v) in used or v is front or v.iso:
            continue
        if abs(v.y0 - front.y0) < tol and abs(v.y1 - front.y1) < tol and (v.x0 > front.x1 or v.x1 < front.x0):
            gap = v.x0 - front.x1 if v.x0 > front.x1 else front.x0 - v.x1
            if best is None or gap < best[0]:
                best = (gap, v, "right" if v.x0 > front.x1 else "left")
    return best[1:] if best else None


def _outline(view, to_model):
    """the view's outer outline as segments in model (u, v) coordinates, arcs kept as arcs; None if the view
    does not close into an outline or its outline is just its box (nothing to trim)"""
    from recognize import faces, to_segments
    fs = faces(list(view.prims))
    if not fs:
        return None
    outer = unary_union(fs)
    if outer.geom_type == "MultiPolygon":
        outer = max(outer.geoms, key=lambda g: g.area)
    x0, y0, x1, y1 = outer.bounds
    if outer.area > 0.995 * (x1 - x0) * (y1 - y0):
        return None
    circles = [(p.cx, p.cy, p.r) for p in view.prims if p.kind in ("circle", "arc")]
    segs = to_segments(list(outer.exterior.coords), circles, 0.002)
    return [(k, *[to_model(q) for q in pts]) for k, *pts in segs]


def _place(solid, rows, t):
    """move a solid by the rotation with these rows (a quarter-turn frame) and the shift t"""
    tr = gp_Trsf()
    tr.SetValues(*[float(v) for v in rows[0]], float(t[0]), *[float(v) for v in rows[1]], float(t[1]),
                 *[float(v) for v in rows[2]], float(t[2]))
    return cq.Shape.cast(BRepBuilderAPI_Transform(solid.wrapped, tr, True).Shape())


def view_prisms(pm, plan, side, ev, scale):
    """the prisms of the front view's and the end view's outlines (see the module notes), as
    [(name, solid)]; ev is end_view()'s answer for this part. Outlines that are plain rectangles give no
    prism. Call before building: when the end view gives a profile, the body is to be built as a full-height
    block of the plan outline (pm is changed so), since its slopes and steps come from the prisms."""
    from build import _solid
    front = next((v for w, v in side if w == "below"), None)
    if front is None:
        return []
    L = (plan.x1 - plan.x0) * scale          # length (x), depth (y), height (z) of the part
    D = (plan.y1 - plan.y0) * scale
    H = (front.y1 - front.y0) * scale
    m = 0.05                                   # prisms run a little past the part
    out = []
    # the front view: (x, z) on the sheet, pushed through along y
    segs = _outline(front, lambda q: ((q[0] - plan.x0) * scale, (q[1] - front.y0) * scale))
    if segs:
        try:
            prism = _solid(segs, D + 2 * m).val()                     # (u, v, w) = (x, z, -y)
            out.append(("front", _place(prism, [(1, 0, 0), (0, 0, -1), (0, 1, 0)], (0, D + m, 0))))
        except Exception as e:
            pm.notes.append(f"front view outline not applied ({type(e).__name__})")
    if ev:
        v, where = ev
        if abs((v.x1 - v.x0) * scale - D) > 0.02 * max(D, 0.1):
            pm.notes.append("end view is not as wide as the part is deep - not used")
        else:
            # right of the front view: its left edge is the front face (y = 0); left of it: its right edge
            ymap = (lambda px: (px - v.x0) * scale) if where == "right" else (lambda px: (v.x1 - px) * scale)
            segs = _outline(v, lambda q: (ymap(q[0]), (q[1] - v.y0) * scale))
            if segs:
                try:
                    prism = _solid(segs, L + 2 * m).val()             # (u, v, w) = (y, z, x)
                    out.append(("end", _place(prism, [(0, 0, 1), (1, 0, 0), (0, 1, 0)], (-m, 0, 0))))
                except Exception as e:
                    pm.notes.append(f"end view outline not applied ({type(e).__name__})")
    if any(n == "end" for n, _ in out):
        # the cross-section comes from the end view: start from the full-height block, not plan steps
        pm.base, pm.raised, pm.thickness = 0.0, [], H
        pm.notes = [n for n in pm.notes if not n.startswith("stepped part")]
    return out


def trim_to_views(body, pm, prisms):
    """body cut down to view_prisms()'s prisms"""
    for name, prism in prisms:
        try:
            body = body.intersect(cq.Workplane().add(prism))
        except Exception as e:
            pm.notes.append(f"{name} view outline not applied ({type(e).__name__})")
            prisms = [p for p in prisms if p[0] != name]
    if prisms:
        pm.notes.append("shape trimmed to the outline of the " + " and ".join(f"{n} view" for n, _ in prisms))
    return body
