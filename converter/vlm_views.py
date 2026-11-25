"""Views of a 3D model and of a drawing in one frame, for checking a model against its drawing.

Model frame (millimetres): x along the part as the front view shows it (left to right), y from the front face
backwards, z up; the origin is the model's lowest corner. Views (third angle):
    top    looks down          -> (x, y)
    front  looks from the front -> (x, z)
    end    looks from the right -> (y, z)     (the end view beside the front view; from the left it is mirrored)
"""
import math
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

VIEW_AXES = {"top": (0, 1), "front": (0, 2), "end": (1, 2)}
# projector for hidden-line drawings: (direction towards the viewer, x direction of the 2D view)
PROJECTORS = {"top": ((0, 0, 1), (1, 0, 0)), "front": ((0, -1, 0), (1, 0, 0)), "end": ((1, 0, 0), (0, 1, 0)),
              "iso": ((1, -1, 1), (1, 1, 0))}       # iso: from front-right-top, for pictures only


def moved_to_origin(shape):
    """the shape moved so its lowest corner is at the origin"""
    import cadquery as cq
    bb = shape.BoundingBox()
    return shape.translate(cq.Vector(-bb.xmin, -bb.ymin, -bb.zmin))


def silhouette(shape, view, tol=0.05):
    """the shape's outer outline seen from one side: shapely (Multi)Polygon in that view's 2D coordinates.
    Through holes are filled in: the drawing's outline of a view is its outer edge only, so holes would count
    as a difference (holes are checked by the picture, not by this score)."""
    a, b = VIEW_AXES[view]
    tris = []
    for f in shape.Faces():
        vs, ts = f.tessellate(tol, 0.2)
        P = [(v.toTuple()[a], v.toTuple()[b]) for v in vs]
        for t in ts:
            p = Polygon([P[t[0]], P[t[1]], P[t[2]]])
            if p.area > 1e-9:
                tris.append(p)
    if not tris:
        return Polygon()
    u = unary_union(tris).buffer(0)
    return unary_union([Polygon(g.exterior) for g in getattr(u, "geoms", [u]) if not g.is_empty])


def hidden_line_edges(shape, view, samples=24):
    """the shape drawn as a drawing view: ([visible polylines], [hidden polylines]) in that view's 2D coordinates"""
    import cadquery as cq
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir
    d, x = PROJECTORS[view]
    algo = HLRBRep_Algo()
    algo.Add(shape.wrapped)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*d), gp_Dir(*x))))
    algo.Update()
    algo.Hide()
    h = HLRBRep_HLRToShape(algo)

    def lines(compounds):
        out = []
        for c in compounds:
            if c is None or c.IsNull():
                continue
            for e in cq.Shape.cast(c).Edges():
                n = 2 if e.geomType() == "LINE" else samples
                out.append([(p.x, p.y) for p in (e.positionAt(i / n) for i in range(n + 1))])
        return out
    vis = lines([h.VCompound(), h.OutLineVCompound(), h.Rg1LineVCompound()])
    hid = lines([h.HCompound(), h.OutLineHCompound()])
    return vis, hid


def compare(model_sil, drawing_sil):
    """how well two outlines agree: overlap (shared area / combined area) and the size of each"""
    if model_sil.is_empty or drawing_sil.is_empty:
        return dict(overlap=0.0)
    inter = model_sil.intersection(drawing_sil).area
    union = model_sil.union(drawing_sil).area
    mb, db = model_sil.bounds, drawing_sil.bounds
    return dict(overlap=round(inter / union, 4) if union else 0.0,
                model_size=[round(mb[2] - mb[0], 2), round(mb[3] - mb[1], 2)],
                drawing_size=[round(db[2] - db[0], 2), round(db[3] - db[1], 2)],
                extra_area=round(model_sil.difference(drawing_sil).area, 2),
                missing_area=round(drawing_sil.difference(model_sil).area, 2))


def _sample(polylines, step):
    """points along polylines, about `step` apart, with the length each point stands for"""
    from shapely.geometry import LineString
    pts = []
    for pl in polylines:
        if len(pl) < 2:
            continue
        ls = LineString(pl)
        if ls.length < 1e-6:
            continue
        n = max(1, int(math.ceil(ls.length / step)))
        w = ls.length / n
        pts += [(ls.interpolate((i + 0.5) * w), w) for i in range(n)]
    return pts


def edge_match(model_edges, drawing_edges, tol=0.2, step=0.25):
    """how well a model's visible edges in one view agree with the drawing's visible edges there (both polylines
    in the view's 2D coordinates, mm). Outlines alone miss what is inside them (a channel, a pocket, a step);
    edges show it. Returns
        drawing_explained  share of the drawing's edge length lying on a model edge (low: a feature is missing)
        model_explained    share of the model's edge length lying on a drawing edge (low: a feature is extra
                           or in the wrong place)
        missing / extra    those lengths in mm, and a few places where they are, for feedback"""
    from shapely.geometry import MultiLineString
    from shapely.prepared import prep
    def near(polylines):
        ls = [pl for pl in polylines if len(pl) >= 2]
        return prep(MultiLineString(ls).buffer(tol)) if ls else None
    out = {}
    for name, src, ref in (("drawing", drawing_edges, model_edges), ("model", model_edges, drawing_edges)):
        zone = near(ref)
        pts = _sample(src, step)
        total = sum(w for _, w in pts)
        off = [(p, w) for p, w in pts if zone is None or not zone.contains(p)]
        miss = sum(w for _, w in off)
        out[f"{name}_explained"] = round(1 - miss / total, 4) if total else 1.0
        out["missing" if name == "drawing" else "extra"] = round(miss, 1)
        # a few places where the difference is, as (x, y) in the view, farthest apart first
        spots = []
        for p, _ in off:
            if all(math.dist((p.x, p.y), q) > 2.0 for q in spots):
                spots.append((round(p.x, 1), round(p.y, 1)))
        out["missing_at" if name == "drawing" else "extra_at"] = spots[:8]
    return out


def render_views(shape, drawing_views, path, title="", iso=False):
    """a picture of the model's three views (visible edges solid, hidden dashed) with the drawing's outline of
    each view in red underneath, for the model to see where it differs"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = [v for v in ("top", "front", "end") if v in drawing_views or shape is not None] + (["iso"] if iso else [])
    fig, axs = plt.subplots(1, len(names), figsize=(6 * len(names), 5))
    axs = np.atleast_1d(axs)
    for ax, v in zip(axs, names):
        dv = drawing_views.get(v)
        if dv is not None and dv.get("outline") is not None:
            o = dv["outline"]
            for g in getattr(o, "geoms", [o]):
                xs, ys = g.exterior.xy
                ax.fill(xs, ys, color="#f4c7c3", zorder=0)
                ax.plot(xs, ys, color="#c62828", lw=1.0, zorder=1)
        if shape is not None:
            try:
                vis, hid = hidden_line_edges(shape, v)
            except Exception:
                vis, hid = [], []
            for pl in hid:
                ax.plot(*zip(*pl), color="#1f4e9a", lw=0.8, ls=(0, (4, 3)), zorder=2)
            for pl in vis:
                ax.plot(*zip(*pl), color="black", lw=1.2, zorder=3)
        ax.set_aspect("equal")
        ax.set_title("isometric view (from front, right, above)" if v == "iso" else
                     f"{v} view  ({'x, y' if v == 'top' else 'x, z' if v == 'front' else 'y, z'} in mm)")
        ax.grid(alpha=0.3)
    if title:
        fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)
