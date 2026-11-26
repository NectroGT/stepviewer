"""Find the pages of a PDF that can be built in 3D, and say why the others cannot.

usage: python scan.py drawing.pdf [--no-text] [--jobs N]

A page is usable when its views pair up into a part: a plan view with a side view lined up next to it
(third-angle), also for parts drawn turned on the sheet. The sheet text (PDF text, else OCR) is read too, to
spot assembly drawings: they pass the geometry test but show several parts together."""
import os, re, sys, warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")

# "SEE ASSY" / "REF ASSEMBLY" are notes on single-part sheets pointing back to the assembly: not a sign of one
ASSEMBLY = re.compile(r"\bBOM\b|PARTS?\s+LIST|BILL\s+OF\s+MATERIAL|(?<!SEE )(?<!REF )\b(?:ASSY|ASSEMBLY)\b"
                      r"|OMITTED\s+FOR\s+CLARITY|\bINSERT\s+ITEM|\bITEMS?\s+\d+\s*-\s*\d+|\bP/\s?NO\b", re.I)


def read_page_words(pdf, i):
    """[(text, x0, y0, x1, y1)] in paper inches, y up: the PDF's own text when it has some, else OCR"""
    import pymupdf
    page = pymupdf.open(pdf)[i]
    H = page.rect.height
    words = [(w[4], w[0] / 72, (H - w[3]) / 72, w[2] / 72, (H - w[1]) / 72) for w in page.get_text("words")]
    if len(words) < 5:
        from text import read_words
        words = read_words(pdf, i)
    return [(str(t), float(a), float(b), float(c), float(d)) for t, a, b, c, d in words]   # plain floats (OCR gives numpy)


def _image_cover(pdf, i):
    """share of the page covered by pictures (a scanned drawing is one big picture)"""
    import pymupdf
    page = pymupdf.open(pdf)[i]
    area = page.rect.width * page.rect.height or 1
    return min(1.0, sum(abs(pymupdf.Rect(im["bbox"])) for im in page.get_image_info()) / area)


def scan_page(pdf, i, text=True):
    """quick check of page i (0-based) -> dict(page, status usable|none|error, parts, views, reason, warn, words)"""
    from extract import page_prims
    from views import find_views
    from recognize import group_parts, turned_parts
    r = dict(page=i + 1, status="none", parts=0, views=0, turned=0, reason="", warn="", words=None)
    try:
        prims, size = page_prims(pdf, i)
        views = find_views(prims, size)
        parts = group_parts(views)
        used = {id(v) for pl, sd, bt in parts for v in [pl, bt] + [x for _, x in sd] if v is not None}
        unused = [v for v in views if id(v) not in used and not v.iso and v.w * v.h > 0.01]
        turned = turned_parts(unused, prims, used)
        flat = [v for v in views if not v.iso]
        r.update(views=len(views), parts=len(parts) + len(turned), turned=len(turned), lines=len(prims))
    except Exception as e:
        r.update(status="error", reason=f"could not read the page: {type(e).__name__}: {e}")
        return r

    assembly = False
    if text:
        try:
            r["words"] = read_page_words(pdf, i)
            from text import lines
            joined = " | ".join(L["text"] for L in lines(r["words"]))
            assembly = bool(ASSEMBLY.search(joined))
        except Exception:
            pass                               # no OCR installed: geometry only

    if r["parts"]:
        r["status"] = "usable"
        if assembly:
            r["warn"] = "Looks like an assembly drawing (parts list or item callouts) - check that what is built is one real part."
        return r
    if len(prims) < 500 and _image_cover(pdf, i) > 0.5:
        r["reason"] = "Scanned image: the page has no drawn lines to read. Only vector PDFs exported from CAD work."
    elif not views:
        r["reason"] = "No drawing views on this page" + (" (text only)." if r["words"] else ".")
    elif assembly:
        r["reason"] = "Assembly drawing (parts list or item callouts): it shows several parts together, not one part to build."
    elif not flat:
        r["reason"] = "Only 3D (isometric) views - there are no flat views to measure from."
    else:
        r["reason"] = (f"{len(flat)} view{'s' if len(flat) > 1 else ''} found, but none has a side view lined up next to it, "
                       "so the thickness cannot be measured.")
    return r


def biggest_first(pdf):
    """page indexes, the pages with the most drawing data first, so the slowest page does not start last"""
    import pymupdf
    doc = pymupdf.open(pdf)
    return sorted(range(doc.page_count), key=lambda i: -len(doc[i].read_contents()))


def workers():
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def init_worker(n):
    """each of the n worker processes gets an equal share of the cores for OCR, so they do not fight"""
    os.environ["PDF2STEP_OCR_THREADS"] = str(max(1, (os.cpu_count() or n) // n))
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[k] = "1"                    # numpy & co. would otherwise start a thread per core in every process
    warnings.filterwarnings("ignore")


def pool(n=None):
    n = n or workers()
    return ProcessPoolExecutor(n, initializer=init_worker, initargs=(n,))


if __name__ == "__main__":
    import argparse, pymupdf
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("--no-text", action="store_true"); ap.add_argument("--jobs", type=int, default=workers())
    a = ap.parse_args()
    n = pymupdf.open(a.pdf).page_count
    with pool(a.jobs) as ex:
        futs = [ex.submit(scan_page, a.pdf, i, not a.no_text) for i in biggest_first(a.pdf)]
        res = sorted((f.result() for f in as_completed(futs)), key=lambda r: r["page"])
    for r in res:
        if r["status"] == "usable":
            print(f"page {r['page']:3d}  usable  {r['parts']} part(s)" + (f"   ! {r['warn']}" if r["warn"] else ""))
        else:
            print(f"page {r['page']:3d}  -       {r['reason']}")
