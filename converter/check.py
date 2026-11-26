"""Checks a 3D model against the drawing it was built from - no AI, all on this computer.

    python check.py drawing.pdf model.step [--page 1] [--png out.png]

The drawing's views are read as pdf2step reads them and put into one model frame, in millimetres:
x along the part as the front view shows it (left to right), y from the front face backwards, z up; the origin is
the model's lowest corner. Views (third angle):
    top    looks down          -> (x, y)
    front  looks from the front -> (x, z)
    end    looks from the right -> (y, z)     (the end view beside the front view; from the left it is mirrored)
The model is turned (quarter turns) into that frame and projected into each view, then compared:
    outline   overlap of the model's outer outline with the drawing's (1.0 = same)
    edges     share of the drawing's visible lines that have a model edge within 0.2 mm (drawing_explained):
              outlines alone miss what is inside them (a channel, a pocket, a step); the edges show it
A model matches when the mean outline overlap is 0.998 or more and every view's edge share is 0.93 or more
(calibrated: exact models score 0.94-1.0; a base missing its channel, with matching outlines, 0.35-0.56).
The web app runs this on every converted part (convert_and_check) and marks it "matches drawing" or "differs".
"""
import argparse, json, math, os, sys
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VIEW_AXES = {"top": (0, 1), "front": (0, 2), "end": (1, 2)}
# projector for hidden-line drawings: (direction towards the viewer, x direction of the 2D view)
PROJECTORS = {"top": ((0, 0, 1), (1, 0, 0)), "front": ((0, -1, 0), (1, 0, 0)), "end": ((1, 0, 0), (0, 1, 0))}


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


def render_views(shape, drawing_views, path, title=""):
    """a picture of the model's three views (visible edges solid, hidden dashed) with the drawing's outline of
    each view in red underneath, to see where it differs"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = [v for v in ("top", "front", "end") if v in drawing_views or shape is not None]
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
        ax.set_title(f"{v} view  ({'x, y' if v == 'top' else 'x, z' if v == 'front' else 'y, z'} in mm)")
        ax.grid(alpha=0.3)
    if title:
        fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


# ------------------------------------------------------------------ the drawing and the check

def read_drawing(pdf, page=1, scale=None):
    """the converter's reading of one sheet, for the check: each view's outline and visible edges in the model
    frame (mm).
    scale: 1:scale as entered by the user (None: from the title block / the dimensions)"""
    from extract import page_prims
    from views import find_views
    from recognize import faces, group_parts
    from text import read_words, lines, sheet_scale, dimension_scale, drawing_units
    from trim import end_view

    prims, size = page_prims(pdf, page - 1)
    tl = lines(read_words(pdf, page - 1))
    mm = drawing_units(tl) == "mm"
    views = find_views(prims, size)
    if scale:
        scale, scale_note, ds = float(scale), "as entered", None
    else:
        scale, scale_note = sheet_scale(tl), "from the title block"
        ds = dimension_scale(prims, tl, mm, views)
    if ds and (scale is None or abs(ds / scale - 1) > 0.03):
        scale, scale_note = ds, "measured from the dimension lines (the title block scale does not match the sheet)"
    if scale is None:
        raise SystemExit("could not read the sheet scale")
    k = scale * 25.4                                  # mm of the part per inch of paper

    parts = group_parts(views)
    if not parts:
        raise SystemExit("no plan view with a lined-up side view found on this page")
    plan, side, _ = parts[0]
    below = next((v for w, v in side if w == "below"), None)
    above = next((v for w, v in side if w == "above"), None)
    if below is not None:
        top, front = plan, below
    elif above is not None:                           # the biggest view is the front view, the top view above it
        top, front = above, plan
    else:
        top, front = plan, None
    used = {id(plan)} | {id(v) for _, v in side}
    ev = end_view(front, views, used) if front is not None else None

    # each view: sheet point -> model frame (mm), and which model axes it shows
    frames = {"top": (top, lambda q: ((q[0] - top.x0) * k, (q[1] - top.y0) * k))}
    if front is not None:
        frames["front"] = (front, lambda q: ((q[0] - front.x0) * k, (q[1] - front.y0) * k))
    if ev:
        v, where = ev
        if where == "right":
            frames["end"] = (v, lambda q, v=v: ((q[0] - v.x0) * k, (q[1] - v.y0) * k))
        else:
            frames["end"] = (v, lambda q, v=v: ((v.x1 - q[0]) * k, (q[1] - v.y0) * k))

    out_views = {}
    for name, (v, M) in frames.items():
        fs = faces(list(v.prims))
        outline = None
        if fs:
            u = unary_union(fs)
            u = max(u.geoms, key=lambda g: g.area) if u.geom_type == "MultiPolygon" else u
            outline = Polygon([M(q) for q in u.exterior.coords]).buffer(0)
        edges = [[M(q) for q in (p.pts if p.kind != "line" else (p.pts[0], p.pts[-1]))] for p in v.prims]
        out_views[name] = dict(outline=outline, edges=edges)
    return dict(units="mm" if mm else "inch", scale=f"1:{scale:.4g} ({scale_note})", views=out_views)


MATCH = 0.998              # mean outline overlap at which a model's outlines match the drawing
EDGE_MATCH = 0.93          # every view: share of the drawing's visible lines the model reproduces (calibrated on
                           # exact models: >= 0.94; a base missing its channel: 0.35-0.56)


def assess(shape, d):
    """how well a model (cadquery shape, model frame) agrees with the drawing read by read_drawing.
    Per view: the outline overlap and the edge agreement. Returns (scores, summary) where summary has
    mean_overlap, edges (lowest drawing_explained over the views), score (one number to rank orientations) and
    matched (outlines and edges both agree)."""
    s = moved_to_origin(shape)
    scores = {}
    for name, v in d["views"].items():
        if v["outline"] is None:
            continue
        sc = compare(silhouette(s, name), v["outline"])
        if v.get("edges"):
            try:
                e = edge_match(hidden_line_edges(s, name)[0], v["edges"])
                sc["edges"] = {k: e[k] for k in ("drawing_explained", "model_explained", "missing", "missing_at",
                                                 "extra", "extra_at")}
            except Exception as ex:                       # projection failed: judge this view by its outline only
                sc["edges"] = dict(error=str(ex))
        scores[name] = sc
    mean = sum(x["overlap"] for x in scores.values()) / len(scores) if scores else 0.0
    ex = [x["edges"]["drawing_explained"] for x in scores.values() if "drawing_explained" in x.get("edges", {})]
    edges = min(ex) if ex else 1.0
    return scores, dict(mean_overlap=round(mean, 4), edges=round(edges, 4),
                        score=round((mean + (sum(ex) / len(ex) if ex else mean)) / 2, 4),
                        matched=bool(scores) and mean >= MATCH and edges >= EDGE_MATCH)


def drawing_size(d):
    """the part's size (x, y, z) in mm as the drawing's view outlines give it (None where no view shows it)"""
    size = [None, None, None]
    for name, v in d["views"].items():
        if v["outline"] is None:
            continue
        b = v["outline"].bounds
        a, c = VIEW_AXES[name]
        size[a] = size[a] or b[2] - b[0]
        size[c] = size[c] or b[3] - b[1]
    return size


def oriented(shape, d, tol=0.5):
    """the shape turned (quarter turns) into the drawing's model frame: of the orientations whose size fits the
    drawing, the one that agrees best with it. Returns (shape, scores, summary)."""
    import itertools
    import numpy as np
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    want = drawing_size(d)
    best = None
    for perm in itertools.permutations(range(3)):
        for sg in itertools.product((1, -1), repeat=3):
            m = np.zeros((3, 3))
            for i in range(3):
                m[i, perm[i]] = sg[i]
            if np.linalg.det(m) < 0:
                continue
            t = gp_Trsf()
            t.SetValues(*[float(v) for v in m[0]], 0.0, *[float(v) for v in m[1]], 0.0,
                        *[float(v) for v in m[2]], 0.0)
            r = cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, t, True).Shape())
            bb = r.BoundingBox()
            if any(w is not None and abs(w - l) > tol for w, l in zip(want, (bb.xlen, bb.ylen, bb.zlen))):
                continue
            scores, summ = assess(r, d)
            if best is None or summ["score"] > best[2]["score"]:
                best = (r, scores, summ)
    if best is None:                                  # no orientation has the drawing's size: judge it as it is
        scores, summ = assess(shape, d)
        best = (shape, scores, summ)
    return best


def check_part(pdf, page, step, scale=None, png=None):
    """the check for a model already built (the web app's badge): dict(matched, mean_overlap, edges, views) or
    dict(error=...). png: also draw the model's views over the drawing's outlines there"""
    try:
        import cadquery as cq
        d = read_drawing(pdf, page, scale)
        s, scores, summ = oriented(cq.importers.importStep(step).val(), d)
        if png:
            render_views(moved_to_origin(s), d["views"], png, "the model's views (black) over the drawing's (red)")
    except BaseException as e:                       # SystemExit too: read_drawing exits when it cannot read the scale
        return dict(error=f"{type(e).__name__}: {e}")
    return dict(matched=summ["matched"], mean_overlap=summ["mean_overlap"], edges=summ["edges"],
                views={k: dict(overlap=v["overlap"], edges=v.get("edges", {}).get("drawing_explained"))
                       for k, v in scores.items()})


def convert_and_check(pdf, page, scale, out, words=None):
    """pdf2step.convert_page, then the check on its part (pages with one part: the drawing reading the check
    uses follows the page's first part only). Each checked part gets "check" and <name>_check.png."""
    from pdf2step import convert_page
    results = convert_page(pdf, page, scale, out, words=words)
    built = [r for r in results if "size_mm" in r and os.path.exists(os.path.join(out, r["name"] + ".step"))]
    if len(built) == 1:
        r = built[0]
        r["check"] = check_part(pdf, page, os.path.join(out, r["name"] + ".step"), scale,
                                os.path.join(out, r["name"] + "_check.png"))
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("step")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--scale", type=float, help="1:SCALE, when the title block does not give it")
    ap.add_argument("--png", help="also draw the model's views over the drawing's outlines")
    a = ap.parse_args()
    print(json.dumps(check_part(a.pdf, a.page, a.step, a.scale, a.png), indent=1))


if __name__ == "__main__":
    main()
