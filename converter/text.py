"""Read the sheet's text. CAD exports often draw text as filled outlines, not as PDF text, so the page is
rendered, every stroked line is painted out, and what is left (lettering) goes through Tesseract OCR."""
import math, os, re
import numpy as np
import pymupdf
from PIL import Image, ImageDraw

DPI = 500


def text_layer(pdf, pageno, dpi=DPI):
    doc = pymupdf.open(pdf)
    page = doc[pageno]
    k = dpi / 72.0
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    d = ImageDraw.Draw(im)
    for dr in page.get_drawings():
        if dr["type"] != "s":
            continue
        w = max(3, int((dr.get("width") or 0.5) * k * 1.6) + 2)
        for it in dr["items"]:
            if it[0] == "l":
                d.line([(it[1].x * k, it[1].y * k), (it[2].x * k, it[2].y * k)], fill=255, width=w)
            elif it[0] == "c":
                pts = []
                for i in range(9):
                    t = i / 8; mt = 1 - t
                    pts.append(tuple(mt ** 3 * a + 3 * mt * mt * t * b + 3 * mt * t * t * c + t ** 3 * e
                                     for a, b, c, e in zip((it[1].x * k, it[1].y * k), (it[2].x * k, it[2].y * k),
                                                           (it[3].x * k, it[3].y * k), (it[4].x * k, it[4].y * k))))
                d.line(pts, fill=255, width=w)
            elif it[0] == "re":
                r = it[1]
                d.rectangle([r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k], outline=255, width=w)
    return im, k, page.rect.height


_RAPID = None


def read_words(pdf, pageno):
    """[(text, x0, y0, x1, y1)] in paper inches, y up. Uses RapidOCR (pip only) when installed, else Tesseract."""
    global _RAPID
    try:
        try:
            from rapidocr import RapidOCR                # rapidocr >= 2 (Python 3.13+)
            new_api = True
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR    # older package, Python < 3.13
            new_api = False
        if _RAPID is None:
            threads = int(os.environ.get("PDF2STEP_OCR_THREADS", "-1"))   # set when pages run side by side
            if new_api:
                _RAPID = RapidOCR(params={"Global.log_level": "warning",
                                          "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                                          "EngineConfig.onnxruntime.inter_op_num_threads": 1 if threads > 0 else -1})
            else:
                _RAPID = RapidOCR()
        im, k, Hpt = text_layer(pdf, pageno, dpi=300)
        r = _RAPID(np.array(im.convert("RGB")))
        if new_api:
            res = list(zip(r.boxes, r.txts, r.scores)) if r.boxes is not None else []
        else:
            res = r[0]
        out = []
        for box, t, conf in res or []:
            if float(conf) < 0.4:
                continue
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            out.append((t.strip(), min(xs) / k / 72, (Hpt - max(ys) / k) / 72, max(xs) / k / 72, (Hpt - min(ys) / k) / 72))
        return out
    except ImportError:
        pass
    import pytesseract
    im, k, Hpt = text_layer(pdf, pageno)
    data = pytesseract.image_to_data(im, config="--psm 11", output_type=pytesseract.Output.DICT)
    out = []
    for t, x, y, w, h, c in zip(data["text"], data["left"], data["top"], data["width"], data["height"], data["conf"]):
        t = t.strip()
        if not t or float(c) < 20:
            continue
        x0, x1 = x / k / 72, (x + w) / k / 72
        y1, y0 = (Hpt - y / k) / 72, (Hpt - (y + h) / k) / 72
        out.append((t, x0, y0, x1, y1))
    return out


def lines(words, gap=0.12):
    """join words on the same baseline into text lines"""
    ws = sorted(words, key=lambda w: (-round((w[2] + w[4]) / 2 / 0.04), w[1]))
    out = []
    for w in ws:
        yc = (w[2] + w[4]) / 2
        hit = None
        for L in out:
            if abs(L["yc"] - yc) < 0.04 and 0 <= w[1] - L["x1"] < gap:
                hit = L; break
        if hit:
            hit["text"] += " " + w[0]; hit["x1"] = w[3]; hit["y0"] = min(hit["y0"], w[2]); hit["y1"] = max(hit["y1"], w[4])
        else:
            out.append(dict(text=w[0], x0=w[1], y0=w[2], x1=w[3], y1=w[4], yc=yc))
    return out


def sheet_scale(text_lines):
    """the title block's SCALE n:m (the lowest SCALE on the sheet; detail views carry their own) -> n/m as
    model units per paper unit, e.g. 3.0 for 1:3"""
    cands = []
    for L in text_lines:
        t = L["text"].upper()
        if "SCALE" not in t:
            continue
        m = re.search(r"SCALE\W*(\d+)\s*[:/ ]\s*(\d+)", t)
        if not m:
            near = [N for N in text_lines if N is not L and abs(N["yc"] - L["yc"]) < 0.06 and 0 < N["x0"] - L["x1"] < 1.5
                    and re.fullmatch(r"\s*\d+\s*[:/ ]\s*\d+\s*", N["text"])]
            if near:
                m = re.search(r"(\d+)\s*[:/ ]\s*(\d+)", min(near, key=lambda N: N["x0"])["text"])
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a and b and max(a, b) <= 50:
                cands.append((L["y0"], b / a))
    return min(cands)[1] if cands else None


# ------------------------------------------------------------------ hole callouts
TAP = {  # thread -> tap drill diameter (inch)
    "#2-56": 0.070, "#4-40": 0.089, "#6-32": 0.1065, "#8-32": 0.136, "#10-24": 0.1495, "#10-32": 0.159,
    "1/4-20": 0.201, "1/4-28": 0.213, "5/16-18": 0.257, "3/8-16": 0.3125, "1/2-13": 0.4219,
    "M3": 2.5 / 25.4, "M4": 3.3 / 25.4, "M5": 4.2 / 25.4, "M6": 5.0 / 25.4, "M8": 6.8 / 25.4, "M10": 8.5 / 25.4,
    "M12": 10.2 / 25.4, "M16": 14.0 / 25.4,
}
NUM = r"(\d*\.\d+|\d+\.\d*)"


def parse_callout(text):
    """fuzzy parse of a hole note as OCR reads it. Returns dict or None.
    Diameter signs come out as O, 0, @, (c), D; the depth sign as V or v; the c'bore sign as L_I, LJ, U, |_|"""
    t = text.replace("\u00a9", "@").replace("\u00d8", "@").replace("\u2300", "@")
    up = t.upper()
    out = {}
    m = re.match(r"\s*(\d+)\s*[XxK]\b", t)
    if m:
        out["count"] = int(m.group(1))
    m = re.search(r"#\s*(\d+)\s*-\s*(\d+)", t)
    if m:
        out["thread"] = f"#{m.group(1)}-{m.group(2)}"
    m = re.search(r"\bM\s*(\d+)(?:\s*[-xX]\s*(\d+(?:\.\d+)?))?", up)
    if m and "thread" not in out:
        out["thread"] = f"M{m.group(1)}"
    m = re.search(r"\b(1/4|5/16|3/8|1/2)\s*-\s*(\d+)", t)
    if m and "thread" not in out:
        out["thread"] = f"{m.group(1)}-{m.group(2)}"
    if "THRU" in up:
        out["thru"] = True
    # depth: the number after a V (the depth sign); OCR often drops the sign, so after a thread a lone
    # trailing number is the depth too
    m = re.search(r"[VvTY\u21a7]\s*" + NUM, t)
    if m:
        out["depth"] = float(m.group(1))
    elif "thread" in out and "THRU" not in up:
        m = re.search(r"(?:UNC|UNF|\d)\s*[^\d.]*" + NUM + r"\W*$", t)
        if m:
            out["depth"] = float(m.group(1))
    # c'bore: "L_I", "LJ", "|_|" ... followed by a diameter
    m = re.match(r"\s*(?:(?:L|\||U)[_\s]*[IJ]?\s*[I|]?D?)?\s*[@O0D]?\s*" + NUM + r"\s*[VTY\u21a7]\s*" + NUM + r"\s*$", up)
    if m is None:   # depth sign lost: "L@.375.19" - needs the c'bore or diameter sign in front
        m = re.match(r"\s*(?:L|\||U|\u2334)[_\s]*[IJ]?\s*[@O0D]\s*(\d*\.\d+?)(\.\d+)\s*$", up)
    if m and not re.match(r"\s*\d+\s*X", up):     # a counterbore line: (c'bore sign) dia (depth sign) depth
        return dict(cbore_d=float(m.group(1)), cbore_depth=float(m.group(2)))
    # drill diameter: a decimal after a diameter sign (or the first bare decimal)
    m = re.search(r"[@OD]\s*" + NUM, up.split("L_I")[0].split("LJ")[0])
    if m:
        out["dia"] = float(m.group(1))
    if not ({"thread", "dia", "cbore_d"} & set(out)) or not ({"thru", "depth", "count", "thread"} & set(out)):
        return None
    return out


def callouts(text_lines):
    """hole notes, with a counterbore line just below joined to the note above it"""
    notes = []
    for L in text_lines:
        c = parse_callout(L["text"])
        if c:
            notes.append(dict(L, **{"info": c}))
    out = []
    for n in sorted(notes, key=lambda n: -n["yc"]):
        if set(n["info"]) <= {"cbore_d", "cbore_depth"}:
            above = [o for o in out if 0 < o["y0"] - n["y1"] < 0.12 and abs(o["x0"] - n["x0"]) < 0.6]
            if above:
                a = above[-1]
                a["info"].update(n["info"]); a["y0"] = n["y0"]; a["x1"] = max(a["x1"], n["x1"])
                continue
        out.append(n)
    return out
