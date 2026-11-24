"""PartModel -> CadQuery solid -> STEP"""
import cadquery as cq


import math


def _ok_arc(s):
    (x0, y0), (xm, ym), (x1, y1) = s[1], s[2], s[3]
    cr = (xm - x0) * (y1 - y0) - (ym - y0) * (x1 - x0)
    return abs(cr) > 1e-7 and math.dist(s[1], s[3]) > 1e-4 and math.dist(s[1], s[2]) > 1e-4 and math.dist(s[2], s[3]) > 1e-4


def _wire(segs, arcs=True):
    segs = [s for s in segs if math.dist(s[1], s[-1]) > 1e-6]
    wp = cq.Workplane("XY").moveTo(*segs[0][1])
    for s in segs:
        if s[0] == "line" or not arcs or not _ok_arc(s):
            if s[0] == "arc" and arcs:
                wp = wp.lineTo(*s[2])
            wp = wp.lineTo(*s[-1])
        else:
            wp = wp.threePointArc(s[2], s[3])
    return wp.close()


def _full_circles(segs):
    """an outline that is one whole circle comes as one arc ending where it starts (its middle point is
    opposite the start): split it into two half arcs so it makes a wire"""
    out = []
    for s in segs:
        if s[0] == "arc" and math.dist(s[1], s[3]) < 1e-6 and math.dist(s[1], s[2]) > 1e-4:
            (x0, y0), (xm, ym) = s[1], s[2]
            cx, cy = (x0 + xm) / 2, (y0 + ym) / 2
            dx, dy = x0 - cx, y0 - cy
            out.append(("arc", (x0, y0), (cx - dy, cy + dx), (xm, ym)))
            out.append(("arc", (xm, ym), (cx + dy, cy - dx), (x0, y0)))
        else:
            out.append(s)
    return out


def _solid(segs, h):
    segs = _full_circles(segs)
    if len(segs) < 2:
        raise ValueError("empty ring")
    for arcs in (True, False):
        try:
            s = _wire(segs, arcs).extrude(h)
            if s.val().isValid():
                return s
        except Exception:
            pass
    # last resort: a cleaned polygon through the segment ends
    import shapely.geometry as sg
    poly = sg.Polygon([q[1] for q in segs]).buffer(0).simplify(5e-4)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    return cq.Workplane("XY").polyline(list(poly.exterior.coords)[:-1]).close().extrude(h)


def _build_turned(pm):
    from shapely.geometry import box
    axis, kind, bores = pm.turned
    poly = pm.outline_poly
    # outline in model units, keep the half on one side of the axis and spin it
    from shapely.affinity import affine_transform
    x0, y0 = poly.bounds[0], poly.bounds[1]
    xs = (poly.bounds[2] - x0)
    sc = None
    return None


def build(pm):
    if pm.turned:
        return _turned(pm)
    T = pm.thickness
    if pm.base and pm.raised:
        body = _solid(pm.outline, pm.base)
        for rz in pm.raised:
            try:
                pad = _solid(rz.ring, rz.z_top)
                for inner in rz.inner:
                    try:
                        pad = pad.cut(_solid(inner, rz.z_top))
                    except Exception:
                        pass
                body = body.union(pad)
            except Exception as e:
                pm.notes.append(f"raised area skipped ({type(e).__name__})")
    else:
        body = _solid(pm.outline, T)
    for c in pm.cuts:
        d = c.depth if c.depth else T
        z0 = 0 if c.side in ("thru", "bottom") else T - d
        h = T if c.side == "thru" else d
        try:
            body = body.cut(_solid(c.ring, h).translate((0, 0, z0)))
        except Exception as e:
            pm.notes.append(f"cut-out skipped ({type(e).__name__})")
    for x, y, r, z0, z1, bholes in pm.bosses:          # posts standing in a pocket, holes from their top
        try:
            body = body.union(cq.Workplane("XY").workplane(offset=z0 - 1e-4).center(x, y).circle(r).extrude(z1 - z0 + 1e-4))
            for rh, d in bholes:
                body = body.cut(cq.Workplane("XY").workplane(offset=z1 - d).center(x, y).circle(rh).extrude(d + 1e-3))
        except Exception as e:
            pm.notes.append(f"boss at ({x:.3f}, {y:.3f}) skipped ({type(e).__name__})")
    for h in pm.holes:
        for r, side, depth in sorted(h.steps):
            d = depth if depth else T * 0.5
            if side == "thru":
                z0, hh = 0, T
            elif side == "top":
                z0, hh = T - d, d
            else:
                z0, hh = 0, d
            try:
                body = body.cut(cq.Workplane("XY").workplane(offset=z0).center(h.x, h.y).circle(r).extrude(hh))
            except Exception as e:
                pm.notes.append(f"hole at ({h.x}, {h.y}) skipped ({type(e).__name__})")
    for sh in pm.side_holes:
        (x, y), (dx, dy) = sh.entry, sh.direction
        try:
            cyl = cq.Solid.makeCylinder(sh.r, sh.depth + 0.02, cq.Vector(x - 0.02 * dx, y - 0.02 * dy, sh.z), cq.Vector(dx, dy, 0))
            body = body.cut(cq.Workplane().add(cyl))
        except Exception as e:
            pm.notes.append(f"side hole skipped ({type(e).__name__})")
    return body


def export(body, path, to_mm=True):
    s = body.val()
    if to_mm:
        s = s.scale(25.4)
    cq.exporters.export(cq.Workplane().add(s), path)
    return s


def _turned(pm):
    """revolve the half outline (model units) about the axis line"""
    axis, kind, bores = pm.turned
    pts = []
    for s in pm.outline:
        pts.append(s[1])
    import shapely.geometry as sg
    poly = sg.Polygon(pts).buffer(0)
    if kind == "x":                      # axis parallel to model y at x = axis
        half = poly.intersection(sg.box(axis, -1e3, 1e3, 1e3))
        prof = [(x - axis, y) for x, y in half.exterior.coords[:-1]]
        prof = [(max(r, 0.0), y) for r, y in prof]
        body = cq.Workplane("XY").polyline(prof).close().revolve(360, (0, 0, 0), (0, 1, 0))
        ymin, ymax = half.bounds[1], half.bounds[3]
        for r, d, where in bores:
            y0 = ymax - d if where == "above" else ymin
            body = body.cut(cq.Workplane("XZ").workplane(offset=-y0).circle(r).extrude(-d))
    else:
        half = poly.intersection(sg.box(-1e3, axis, 1e3, 1e3))
        prof = [(x, y - axis) for x, y in half.exterior.coords[:-1]]
        prof = [(x, max(r, 0.0)) for x, r in prof]
        body = cq.Workplane("XY").polyline(prof).close().revolve(360, (0, 0, 0), (1, 0, 0))
    # keyways: the slot's outline from the plan, cut from its floor (depth = floor above the axis) outwards
    R = max(abs(v) for q in prof for v in (q[0] if kind == "x" else q[1],)) + 0.01
    for c in pm.cuts:
        dx, dy = (-axis, 0) if kind == "x" else (0, -axis)
        z0, h = (c.depth, R - c.depth) if c.side == "top" else (-R, R - c.depth)
        try:
            slot = _solid(c.ring, h).translate((dx, dy, z0))
            body = body.cut(slot)
        except Exception as e:
            pm.notes.append(f"keyway skipped ({type(e).__name__})")
    return body


def _round_edges(solid, along, u, z, tol_z=0.008, tol_u=0.012):
    """edges at height z that run along x (or y) and pass u in the other plan coordinate"""
    out = []
    for e in solid.Edges():
        p = [e.positionAt(k / 10) for k in range(11)]
        if any(abs(q.z - z) > tol_z for q in p):
            continue
        xs = [q.x for q in p]; ys = [q.y for q in p]
        a, b = (xs, ys) if along == "x" else (ys, xs)
        if max(a) - min(a) < 0.02 or max(a) - min(a) < 2 * (max(b) - min(b)):
            continue
        if min(b) - tol_u <= u <= max(b) + tol_u:
            out.append(e)
    # a corner edge is often several pieces (straight + arc + straight); rounding only some of them fails,
    # so carry on along the chain: connected edges at the same height that continue smoothly (< 35 deg turn)
    def flat(e):
        return all(abs(e.positionAt(k / 4).z - z) <= tol_z for k in range(5))
    chain = list(out)
    grew = True
    while grew:
        grew = False
        for e in solid.Edges():
            if any(e.isSame(c) for c in chain) or not flat(e):
                continue
            for c in list(chain):
                for pe, te in ((e.startPoint(), e.tangentAt(0)), (e.endPoint(), e.tangentAt(1))):
                    for pc, tc in ((c.startPoint(), c.tangentAt(0)), (c.endPoint(), c.tangentAt(1))):
                        if pe.sub(pc).Length < 1e-5 and abs(te.normalized().dot(tc.normalized())) > 0.82:
                            chain.append(e); grew = True
                            break
                    if grew and chain[-1] is e:
                        break
                if grew and chain[-1] is e:
                    break
    return chain


def _fillet_all(solid, picks):
    """one fillet operation with a radius per edge (rounds meeting at a corner must be made together)"""
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    mk = BRepFilletAPI_MakeFillet(solid.wrapped)
    for e, R in picks:
        mk.Add(R, e.wrapped)
    mk.Build()
    if not mk.IsDone():
        raise ValueError("fillet failed")
    out = cq.Shape.cast(mk.Shape())
    if not out.isValid():
        raise ValueError("fillet gave an invalid solid")
    return out


def apply_rounds(body, pm):
    """round the edges found in the side views (pm.rounds). Rounds meeting at a corner must be made in one
    operation, so every round is added to one growing fillet, biggest first; a round that makes it fail is
    left out (sharp) and noted, and the rest still go in."""
    if not getattr(pm, "rounds", None):
        return body
    solid = body.val()
    taken, groups = [], []                       # each round's own edges (an edge goes to the first round taking it)
    for along, u, z, R in sorted(pm.rounds, key=lambda r: -r[3]):
        g = [(e, R) for e in _round_edges(solid, along, u, z) if not any(e.isSame(t) for t in taken)]
        if g:
            taken += [e for e, _ in g]
            groups.append((R, g))
    keep, best, skipped = [], None, []
    for R, g in groups:
        try:
            best = _fillet_all(solid, keep + g)
            keep += g
        except Exception:
            skipped.append((R, len(g)))
    if keep:
        made = len(groups) - len(skipped)
        rs = sorted({R for _, R in keep})
        pm.notes.append(f"{made} rounded edge(s) from the side views made (R {', '.join(f'{r:g}' for r in rs)})")
    if skipped:
        pm.notes.append(f"{len(skipped)} rounded edge(s) (R {', '.join(sorted({f'{r:g}' for r, _ in skipped}))}) "
                        "could not be made - left sharp")
    return cq.Workplane("XY").add(best) if best is not None else body
