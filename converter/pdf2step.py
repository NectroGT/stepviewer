"""pdf2step: build 3D STEP models of plate-type parts from a vector PDF drawing.

usage: python pdf2step.py drawing.pdf --page 13 --scale 3 [-o outdir]
Each part on the page needs a plan view and at least one side view next to it (third-angle)."""
import argparse, json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import page_prims
from views import find_views, dominant_angle, rotate_prims
from recognize import group_parts, recognize, turned_parts
from build import build, export


def _apply_notes(pm, notes, thin, details=(), side=(), plan=None, scale=1.0):
    from notes import attach, apply_hole
    from text import TAP
    groups, idx = [], []
    for i, h in enumerate(pm.holes):
        for (x, y), r in (h.drawn or []):
            groups.append(dict(x=x, y=y, r=[(r, "")])); idx.append(i)
    sides = []
    for i, h in enumerate(pm.side_holes):
        if h.paper is not None and plan is not None:
            dx, dy = h.direction
            a = (plan.x0 + h.entry[0] / scale, plan.y0 + h.entry[1] / scale)
            b = (a[0] + dx * h.depth / scale, a[1] + dy * h.depth / scale)
            sides.append((i, a, b, h.r / scale, h.paper))
    side_views = []
    for where, v in side:
        horizontal = where in ("below", "above")
        cols = []
        for i, h in enumerate(pm.holes):
            if h.paper:
                cols.append((len(groups), h.paper[0] if horizontal else h.paper[1], max(h.radii_paper)))
                groups.append(dict(x=1e9, y=1e9, r=[(0, "")])); idx.append(i)      # placeholder index for this hole
        side_views.append((v, horizontal, cols))
    hits = attach(notes, thin, groups, sides, details, side_views)
    n = 0
    for (kind, i), info in hits.items():
        if kind == "hole":
            ref = pm.holes[idx[i]]
            sig = sorted(round(r, 3) for r in ref.radii_paper)
            same = [h for h in pm.holes if h.radii_paper and sorted(round(r, 3) for r in h.radii_paper) == sig] \
                if info.get("count", 1) > 1 else [ref]
            for h in same:
                apply_hole(h, info, pm.thickness, pm.notes, "")
                n += 1
        else:
            ref = pm.side_holes[i]
            same = [h for h in pm.side_holes if abs(h.r - ref.r) < 1e-3] if info.get("count", 1) > 1 else [ref]
            for h in same:
                if info.get("thread") in TAP:
                    h.r = TAP[info["thread"]] / 2; h.note = info["thread"]
                if "depth" in info:
                    h.depth = info["depth"]
                n += 1
    if n:
        pm.notes.append(f"hole notes applied to {n} hole(s)")


def _part_scale(text_lines, plan, sheet_scale):
    """a part drawn at its own scale carries "SCALE: 1 / 1" in its label near the views; detail and section
    titles are not part labels"""
    import re
    best = None
    for i, L in enumerate(text_lines):
        m = re.search(r"SCALE\W*(\d+)\s*[:/ ]\s*(\d+)", L["text"].upper())
        if not m or not int(m.group(1)) or not int(m.group(2)):
            continue
        above = [N["text"].upper() for N in text_lines if 0 < N["y0"] - L["y1"] < 0.3 and abs(N["x0"] - L["x0"]) < 1.0]
        if any("DETAIL" in t or "SECTION" in t for t in above):
            continue
        cx, cy = (L["x0"] + L["x1"]) / 2, L["yc"]
        dx = max(plan.x0 - cx, 0, cx - plan.x1); dy = max(plan.y0 - cy, 0, cy - plan.y1)
        d = math.hypot(dx, dy)
        if d < 2.5 and (best is None or d < best[0]):
            best = (d, int(m.group(2)) / int(m.group(1)))
    return best[1] if best else sheet_scale


def run(pdf, page, scale, out, units="inch", words=None):
    """words: the page's text if already read ([(text, x0, y0, x1, y1)], as text.read_words gives it)"""
    prims, size = page_prims(pdf, page - 1)
    notes, text_scale, text_lines = [], None, []
    try:
        from text import read_words, lines, callouts, sheet_scale
        tl = lines(words if words is not None else read_words(pdf, page - 1))
        notes, text_scale = callouts(tl), sheet_scale(tl)
        text_lines = tl
    except Exception as e:                       # no Tesseract: geometry only
        print(f"(text not read: {type(e).__name__}: {e})", file=sys.stderr)
    if scale is None:
        if text_scale is None:
            raise SystemExit("could not read the sheet scale - give it with --scale")
        scale = text_scale
    views = find_views(prims, size)
    details = []
    if text_lines:
        from details import find_details
        details = find_details(text_lines, views, scale)
    detail_views = {id(d["view"]) for d in details}
    views_main = [v for v in views if id(v) not in detail_views]
    from sections import find_sections
    sections = find_sections(prims, views_main)
    section_views = {id(s["view"]) for s in sections}
    parts = group_parts(views_main)
    used = {id(v) for pl, sd, bt in parts for v in [pl, bt] + [x for _, x in sd] if v is not None}
    unused = [v for v in views_main if id(v) not in used and not v.iso and v.w * v.h > 0.01]
    rotated_parts = turned_parts(unused, prims, used)
    parts = [(p_, 0.0) for p_ in parts] + rotated_parts
    skipped = [repr(v) for v in unused if id(v) not in used and v.w * v.h > 0.05]
    os.makedirs(out, exist_ok=True)
    results = []
    for k, ((plan, side, bottom), turned) in enumerate(parts, 1):
        name = f"p{page:02d}_{k:02d}"
        pscale = _part_scale(text_lines, plan, scale) if not turned else scale
        try:
            pm = recognize(plan, side, bottom, pscale)
            if notes and not turned:
                _apply_notes(pm, notes, [p for p in prims if p.cls == "thin"], details, side, plan, pscale)
            body = build(pm)
            secs = [s for s in sections if s["view"] not in [v for _, v in side]]
            if secs and not turned:
                from sections import apply_sections
                body = apply_sections(body, pm, plan, pscale, prims, secs)
            from build import apply_rounds
            body = apply_rounds(body, pm)
            s = export(body, os.path.join(out, name + ".step"), to_mm=units == "inch")
            bb = s.BoundingBox()
            if abs(turned) > 0:
                pm.notes.append(f"drawn turned {turned:.2f} deg on the sheet - straightened")
            threads = sorted({h.note for h in pm.holes + pm.side_holes if getattr(h, "note", None)})
            results.append(dict(name=name, scale=pscale, threads=threads, size_mm=[round(bb.xlen, 2), round(bb.ylen, 2), round(bb.zlen, 2)],
                                holes=len(pm.holes), side_holes=len(pm.side_holes), cuts=len(pm.cuts), notes=pm.notes,
                                views=[repr(plan)] + [f"{w}: {v!r}" for w, v in side] + ([f"bottom: {bottom!r}"] if bottom else [])))
        except Exception as e:
            results.append(dict(name=name, error=f"{type(e).__name__}: {e}", views=[repr(plan)]))
    # 3D pictures on the sheet, cut out as reference images for the preview (shown, never measured)
    try:
        from refview import find_references, save_references, nearest
        taken = [(v.x0, v.y0, v.x1, v.y1) for v in views
                 if id(v) in used or id(v) in detail_views or id(v) in section_views]
        refs = find_references(pdf, page - 1, taken, text_lines)
        names = save_references(pdf, page - 1, refs, out, f"p{page:02d}")
        for r, ((plan, _, _), turned) in zip(results, parts):
            if names and "size_mm" in r:
                r["ref"] = nearest(refs, names, (plan.x0 + plan.x1) / 2, (plan.y0 + plan.y1) / 2, size[1])                     if not turned else names[0]
    except Exception as e:
        print(f"(reference pictures skipped: {type(e).__name__}: {e})", file=sys.stderr)
    if details:
        results.append(dict(name=f"p{page:02d}_details", details=[f"DETAIL {d['name']} x{d['k']:g} on {d['parent']!r}" for d in details]))
    if skipped:
        results.append(dict(name=f"p{page:02d}_unused_views", note="no aligned side view found - not built", views=skipped))
    with open(os.path.join(out, f"p{page:02d}_report.json"), "w") as f:
        json.dump(results, f, indent=1)
    return results


def convert_page(pdf, page, scale, out, units="inch", words=None):
    """run() plus an STL mesh of each built part (for previews). Returns run()'s results."""
    import shutil
    import cadquery as cq
    shutil.rmtree(out, ignore_errors=True)
    try:
        results = run(pdf, page, scale, out, units, words)
    except SystemExit as e:                    # unreadable scale: a plain error, not the end of the worker
        raise ValueError(str(e)) from None
    for r in results:
        step = os.path.join(out, r["name"] + ".step")
        if "size_mm" in r and os.path.exists(step):
            try:
                shape = cq.importers.importStep(step)
                cq.exporters.export(shape, os.path.join(out, r["name"] + ".stl"), tolerance=0.05, angularTolerance=0.2)
            except Exception:
                pass
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--scale", type=float, default=None,
                    help="drawing scale, e.g. 3 for 1:3 (read from the title block when left out)")
    ap.add_argument("-o", "--out", default="pdf2step_out")
    a = ap.parse_args()
    for r in run(a.pdf, a.page, a.scale, a.out):
        print(json.dumps(r))
