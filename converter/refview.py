"""Reference pictures: the 3D (isometric / pictorial) views drawn on a sheet, cut out as images so the
web app can show them next to the model built from the flat views. They are only shown, never measured.

A reference picture is a group of strokes on the sheet that
  - is not one of the views a part was built from, nor a detail or section view,
  - is not a table, title block or border (those are straight horizontal / vertical lines),
  - is not a logo or a block of text (filled shapes, or real text inside it),
  - is not a detail view's boundary (a dash-dot circle round the whole group),
  - has most of its length in slanted lines and curves (what a 3D view is made of)."""
import math
import os
import pymupdf


def find_references(pdf, pageno, exclude=(), labels=()):
    """[pymupdf.Rect] in PDF points (y down) of the reference pictures on a page, biggest first.
    exclude: [(x0, y0, x1, y1)] boxes in paper inches (y up) of the views already used.
    labels: the sheet's text lines (text.lines(): dicts with text, x0, y0, x1, y1 in paper inches, y up)."""
    doc = pymupdf.open(pdf)
    page = doc[pageno]
    W, H = page.rect.width, page.rect.height
    boxes = [pymupdf.Rect(x0 * 72, H - y1 * 72, x1 * 72, H - y0 * 72) for x0, y0, x1, y1 in exclude]
    drawings = [d for d in page.get_drawings() if d["rect"].width < 0.3 * W and d["rect"].height < 0.3 * H]
    if not drawings:
        return []
    words = [pymupdf.Rect(w[:4]) for w in page.get_text("words")]
    out = []
    for r in page.cluster_drawings(drawings=drawings, x_tolerance=4, y_tolerance=4):
        if r.width < 36 or r.height < 36:                      # under half an inch: a symbol, an arrow
            continue
        if any(abs(r & b) > 0.2 * min(abs(b), abs(r)) for b in boxes):
            continue
        inside = [d for d in drawings if r.contains(d["rect"])]
        fills = sum(1 for d in inside if d["type"] == "f")
        strokes = len(inside) - fills
        if strokes == 0 or fills > 0.2 * strokes:              # text drawn as outlines, logos
            continue
        if sum(1 for w in words if r.contains(w)) > 0:          # dimensions or notes inside
            continue
        if any(all(it[0] == "c" for it in d["items"]) and d["rect"].width > 0.85 * r.width
               and d["rect"].height > 0.85 * r.height for d in inside):
            continue                                            # a detail view's boundary circle
        if sum(len(d["items"]) for d in inside) < 20:          # a few arcs: a dashed circle's leftovers
            continue
        if _labelled(r, H, labels):
            continue                                            # "DETAIL M", "SECTION A-A" under it
        tot = odd = 0.0
        for d in inside:
            for it in d["items"]:
                if it[0] == "l":
                    p, q = it[1], it[2]
                elif it[0] == "c":
                    p, q = it[1], it[4]
                else:
                    continue
                L = abs(q - p)
                a = math.degrees(math.atan2(q.y - p.y, q.x - p.x)) % 90
                tot += L
                if it[0] == "c" or 3 < a < 87:
                    odd += L
        if tot > 0 and odd / tot > 0.3:
            out.append((r, _slant(inside)))
    doc.close()
    # a page with a clear 3D view: the flat-looking leftovers (partial views, unlabelled details) go
    best = max((sc for _, sc in out), default=0)
    if best >= 0.4:
        out = [(r, sc) for r, sc in out if sc >= 0.5 * best]
    return [r for r, _ in sorted(out, key=lambda t: -abs(t[0]))]


def _slant(inside):
    """share of the straight-line length running in the two strongest slanted directions: high for a 3D
    view, whose box edges all run the same few ways (about 30 / 150 deg); low for a flat view (horizontal /
    vertical) and for circles and their dashes (every direction a little)"""
    bins, tot = {}, 0.0
    for d in inside:
        for it in d["items"]:
            if it[0] != "l":
                continue
            L = abs(it[2] - it[1])
            if L < 0.05:
                continue
            a = math.degrees(math.atan2(it[2].y - it[1].y, it[2].x - it[1].x)) % 180
            tot += L
            if min(a % 90, 90 - a % 90) > 4:
                k = int(a // 6)
                bins[k] = bins.get(k, 0.0) + L
    top = sorted(bins.values(), reverse=True)[:2]
    return sum(top) / tot if tot else 0.0


def _labelled(r, H, labels, below=0.9):
    """the view's own label (the nearest text line just under it, overlapping it sideways) names a
    detail or a section view"""
    x0, x1, bottom = r.x0 / 72, r.x1 / 72, (H - r.y1) / 72
    near = [L for L in labels if L["x1"] > x0 and L["x0"] < x1 and bottom - below < L["y1"] <= bottom + 0.05]
    if not near:
        return False
    top = max(near, key=lambda L: L["y1"])
    return str(top["text"]).strip().upper().startswith(("DETAIL", "SECTION"))


def save_references(pdf, pageno, rects, out_dir, prefix, dpi=150):
    """render each picture to <out_dir>/<prefix>_ref<k>.png; returns the file names"""
    doc = pymupdf.open(pdf)
    page = doc[pageno]
    names = []
    for k, r in enumerate(rects, 1):
        name = f"{prefix}_ref{k}.png"
        clip = (r + (-12, -12, 12, 12)) & page.rect
        page.get_pixmap(dpi=dpi, clip=clip).save(os.path.join(out_dir, name))
        names.append(name)
    doc.close()
    return names


def nearest(rects, names, x, y, page_h):
    """the picture closest to a point in paper inches (y up)"""
    if not rects:
        return None
    px, py = x * 72, page_h - y * 72
    k = min(range(len(rects)), key=lambda i: math.dist(((rects[i].x0 + rects[i].x1) / 2, (rects[i].y0 + rects[i].y1) / 2), (px, py)))
    return names[k]
