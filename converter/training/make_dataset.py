"""Training examples for the VLM from generated parts (gen_parts.py): drawing image(s) + the prompt vlm.py sends ->
the answer (feature lines + build()). Every kept answer is checked to rebuild exactly the part on the sheet.

    python training/make_dataset.py GEN_DIR OUT_DIR [--s2d ../step2drawing] [--scans 0.5] [--lines 0.5]
                                    [--easy 0.15] [--full] [--workers K]

The VLM is only needed where the rules converter fails: scans, sheets whose text is drawn as lines, and complicated
parts. So the data leans that way:
- each part's sheet comes in up to three versions: the sheet as drawn; with --lines, the same sheet with its text
  turned into lines (filled letter outlines, or stroked ones as AutoCAD SHX fonts export - no PDF text left); with
  --scans, a fake scan (rendered, tilted, blurred, noisy, grey paper, JPEG, saved as a picture-only PDF)
- the rules converter (pdf2step + check.py) runs on every drawn version; where it already builds the part correctly,
  that version is kept only with probability --easy (default 0.15)
- a drawn version the converter cannot read at all goes through vlm.py's picture route, as it would in use

Steps per part:
1. step2drawing draws it (STEP -> dimensioned PDF sheet) and saves the part as drawn (parts/*/part.step)
2. the quarter turn between the generated part and the drawn one is found from the two solids, added to the end of
   build() and the part moved back to the origin; the final answer is built in its own process and must match the
   drawn part (same solid, volume difference < 0.01%) - kept only then
3. each version is read exactly as vlm.py reads it (measured context, or picture + 4 quarters + OCR text)
4. writes OUT_DIR/images/... and one JSON line per example (system, user with image(s) + text, assistant) to
   OUT_DIR/train.jsonl or OUT_DIR/val.jsonl (about 5% of parts, by id); OUT_DIR/log.jsonl says what happened to each

--full writes the full measured context (as sent to a model server, --url); the default is the brief one sent to
in-process models. Train with the one you will run with. Everything runs on this computer."""
import argparse, hashlib, io, json, os, random, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
CONV = os.path.dirname(HERE)
sys.path.insert(0, CONV)


# ------------------------------------------------------------------ 1. drawings

def drawn_step(gen, pid):
    d = os.path.join(gen, "drawings", pid, "parts")
    if not os.path.isdir(d):
        return None
    for sub in sorted(os.listdir(d)):
        p = os.path.join(d, sub, "part.step")
        if os.path.exists(p):
            return p
    return None


def draw_all(gen, s2d, workers):
    """step2drawing on every part not drawn yet; returns {id: pdf}"""
    ids = sorted(os.listdir(os.path.join(gen, "parts")))
    ok = [i for i in ids if json.load(open(os.path.join(gen, "parts", i, "meta.json")))["ok"]]
    drw = os.path.join(gen, "drawings")
    pdf = lambda i: os.path.join(drw, i, f"{i}_drawings.pdf")
    todo = [i for i in ok if not os.path.exists(pdf(i))]
    if todo:
        inp = os.path.join(gen, "step_in")
        os.makedirs(inp, exist_ok=True)
        for i in todo:
            shutil.copy(os.path.join(gen, "parts", i, "model.step"), os.path.join(inp, f"{i}.step"))
        env = dict(os.environ, PYTHONIOENCODING="utf-8", OMP_NUM_THREADS="1")
        batches = [todo[j:j + 100] for j in range(0, len(todo), 100)]
        print(f"drawing {len(todo)} parts with step2drawing ({min(workers, len(batches))} processes)", flush=True)
        running = []
        for b in batches:
            running.append(subprocess.Popen([sys.executable, os.path.join(s2d, "convert.py"),
                                             *[os.path.join(inp, f"{i}.step") for i in b], "-o", drw],
                                            cwd=s2d, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            while len(running) >= workers:
                running.pop(0).wait()
        for p in running:
            p.wait()
        shutil.rmtree(inp, ignore_errors=True)
    return {i: pdf(i) for i in ok if os.path.exists(pdf(i)) and drawn_step(gen, i)}


# ------------------------------------------------------------------ 2. the quarter turn

R90 = {"x": ((1, 0, 0), ((1, 0, 0), (0, 0, -1), (0, 1, 0))),
       "y": ((0, 1, 0), ((0, 0, 1), (0, 1, 0), (-1, 0, 0))),
       "z": ((0, 0, 1), ((0, -1, 0), (1, 0, 0), (0, 0, 1)))}
IDENTITY = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def _mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def turns():
    """every proper quarter-turn matrix -> the shortest list of (axis, degrees) cadquery rotations that makes it"""
    seen, frontier = {IDENTITY: []}, [IDENTITY]
    while frontier:
        nxt = []
        for m in frontier:
            for ax, (_, r) in R90.items():
                n = _mul(r, m)                         # .rotate(ax) after the rotations so far
                if n not in seen:
                    seen[n] = seen[m] + [ax]
                    nxt.append(n)
        frontier = nxt
    out = {}
    for m, seq in seen.items():                        # x, x -> one 180 turn; x, x, x -> -90
        steps = []
        for ax in seq:
            if steps and steps[-1][0] == ax:
                steps[-1] = (ax, steps[-1][1] + 90)
            else:
                steps.append((ax, 90))
        out[m] = [(ax, -90 if d == 270 else d) for ax, d in steps]
    return out


TURNS = turns()


def _turned(shape, m):
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    t = gp_Trsf()
    t.SetValues(*[float(v) for v in m[0]], 0.0, *[float(v) for v in m[1]], 0.0, *[float(v) for v in m[2]], 0.0)
    return cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped, t, True).Shape())


def same_solid(a, b, tol=1e-4):
    """a and b occupy the same space (after moving both to the origin)"""
    from check import moved_to_origin
    a, b = moved_to_origin(a), moved_to_origin(b)
    v = max(a.Volume(), b.Volume())
    try:
        diff = a.cut(b).Volume() + b.cut(a).Volume()
    except Exception:
        return False
    return diff < tol * v


def drawn_turn(gen_shape, drawn_shape):
    """the quarter turn that makes the generated part into the part as step2drawing drew it, or None"""
    bb = drawn_shape.BoundingBox()
    want = (bb.xlen, bb.ylen, bb.zlen)
    for m in TURNS:
        r = _turned(gen_shape, m)
        rb = r.BoundingBox()
        if all(abs(w - l) < 0.01 for w, l in zip(want, (rb.xlen, rb.ylen, rb.zlen))) and same_solid(r, drawn_shape):
            return m
    return None


def f(v):
    v = round(float(v), 3)
    return str(int(v)) if v == int(v) else repr(v)


def turned_answer(answer, m, size):
    """the answer with the turn added at the end of build() and the part moved back to the origin"""
    if m == IDENTITY:
        return answer
    lo = [sum(min(0, m[i][j] * size[j]) for j in range(3)) for i in range(3)]
    rot = "".join(f".rotate((0, 0, 0), {R90[ax][0]}, {deg})" for ax, deg in TURNS[m])
    move = f"part = part{rot}.translate(({f(-lo[0])}, {f(-lo[1])}, {f(-lo[2])}))"
    head, code = answer.split("\n\n```python", 1)
    head += "\n- built in the frame above, then turned (quarter turns) so that its views lie as the drawing shows them"
    end = "    return part\n```"
    assert code.rstrip().endswith(end)
    code = code.rstrip()[:-len(end)] + f"    {move}\n    return part\n```\n"
    return head + "\n\n```python" + code


# ------------------------------------------------------------------ sheet versions

def lines_text(pdf, out_pdf, seed):
    """the sheet with its text turned into lines, as many CAD exports draw it: the PDF text is removed and each
    string drawn as letter outlines, filled (TrueType fonts) or stroked (SHX fonts). Returns the style."""
    import pymupdf
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    r = random.Random(seed)
    style = r.choice(["filled", "stroked"])
    width = r.uniform(0.2, 0.6)
    doc = pymupdf.open(pdf)
    pg = doc[0]
    spans = [(s, l["dir"]) for b in pg.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]
             if s["text"].strip()]
    for s, _ in spans:
        pg.add_redact_annot(pymupdf.Rect(s["bbox"]), fill=False)
    pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    fp = FontProperties(family="DejaVu Sans")
    sh = pg.new_shape()
    for s, (dx, dy) in spans:
        tp = TextPath((0, 0), s["text"], size=s["size"], prop=fp)
        polys = tp.to_polygons(closed_only=True)
        if not polys:
            continue
        x0, y0, x1, y1 = s["bbox"]
        run = (x1 - x0) if abs(dx) >= abs(dy) else (y1 - y0)
        k = run / max(tp.get_extents().x1, 1e-6)
        ox, oy = s["origin"]
        ux, uy = dy, -dx                               # "up" for the text, in PDF page coordinates (y down)
        for poly in polys:
            sh.draw_polyline([pymupdf.Point(ox + k * x * dx + y * ux, oy + k * x * dy + y * uy) for x, y in poly])
        if style == "filled":
            sh.finish(color=None, fill=(0, 0, 0), even_odd=True, closePath=True)
        else:
            sh.finish(color=(0, 0, 0), width=width, closePath=True)
    sh.commit()
    doc.save(out_pdf)
    return style


def fake_scan(pdf, out_pdf, seed):
    """the sheet as a poor scan: grey paper, a small tilt, blur, noise, JPEG; saved as a picture-only PDF"""
    import numpy as np
    import pymupdf
    from PIL import Image, ImageFilter
    r = random.Random(seed)
    pg = pymupdf.open(pdf)[0]
    im = Image.open(io.BytesIO(pg.get_pixmap(dpi=r.choice([110, 130, 150, 200]), alpha=False).tobytes("png"))).convert("L")
    paper = r.randint(205, 245)
    a = np.asarray(im, dtype=np.float32) / 255.0
    im = Image.fromarray((paper * a + r.randint(0, 40) * (1 - a)).clip(0, 255).astype(np.uint8))
    im = im.rotate(r.uniform(-1.5, 1.5), resample=Image.BICUBIC, expand=False, fillcolor=paper)
    im = im.filter(ImageFilter.GaussianBlur(r.uniform(0.3, 1.1)))
    a = np.asarray(im, dtype=np.float32) + np.random.default_rng(seed).normal(0, r.uniform(3, 12), im.size[::-1])
    buf = io.BytesIO()
    Image.fromarray(a.clip(0, 255).astype(np.uint8)).save(buf, "JPEG", quality=r.randint(45, 85))
    doc = pymupdf.open()
    p = doc.new_page(width=pg.rect.width, height=pg.rect.height)
    p.insert_image(p.rect, stream=buf.getvalue())
    doc.save(out_pdf)


def rules_build(pdf):
    """does the rules converter already build this sheet's part correctly (its check says it matches)?"""
    from check import convert_and_check
    with tempfile.TemporaryDirectory() as td:
        try:
            res = convert_and_check(pdf, 1, None, td)
        except Exception:
            return False
        return any((r.get("check") or {}).get("matched") for r in res)


# ------------------------------------------------------------------ one part

def one(gen, pid, pdf, out, brief, opts, seed):
    """returns ([(split, record)], status)"""
    import cadquery as cq
    import vlm
    from check import assess
    r = random.Random(seed)
    split = "val" if int(hashlib.md5(pid.encode()).hexdigest(), 16) % 20 == 0 else "train"
    meta = json.load(open(os.path.join(gen, "parts", pid, "meta.json")))
    status = dict(id=pid, family=meta["family"], examples=[])
    gen_shape = cq.importers.importStep(os.path.join(gen, "parts", pid, "model.step")).val()
    drawn = cq.importers.importStep(drawn_step(gen, pid)).val()
    m = drawn_turn(gen_shape, drawn)
    if m is None:
        return [], dict(status, why="step2drawing drew a different solid (no quarter turn matches)")
    answer = turned_answer(open(os.path.join(gen, "parts", pid, "answer.py"), encoding="utf-8").read(), m, meta["size"])
    work = os.path.join(out, "images", pid)
    os.makedirs(work, exist_ok=True)
    step = os.path.join(work, "check.step")
    res = vlm.run_candidate(vlm._code_of(answer), step)
    if not res.get("ok") or not same_solid(cq.importers.importStep(step).val(), drawn):
        shutil.rmtree(work, ignore_errors=True)
        return [], dict(status, why="the final answer does not rebuild the drawn part: " + res.get("error", "")[-300:])
    final = cq.importers.importStep(step).val()
    os.remove(step)
    rel = lambda p: os.path.relpath(p, out).replace("\\", "/")
    base = dict(id=pid, family=meta["family"], turned=m != IDENTITY)

    def rec(kind, text, images, **extra):
        return (split, dict(base, kind=kind, **extra, images=[rel(p) for p in images], messages=[
            dict(role="system", content=vlm.SYSTEM),
            dict(role="user", content=[dict(type="image", image=rel(p)) for p in images] + [dict(type="text", text=text)]),
            dict(role="assistant", content=answer)]))

    def as_picture(src, kind, **extra):
        sd = os.path.join(work, kind)
        os.makedirs(sd, exist_ok=True)
        dp = vlm.read_picture(src, 1, sd)
        sheet = os.path.join(sd, "sheet.png")
        open(sheet, "wb").write(dp["image_png"])
        return rec(kind, vlm.picture_prompt(dp), [sheet] + dp["tiles"], route="picture", **extra)

    recs = []
    versions = [("drawn", pdf, {})]
    if r.random() < opts.lines:
        lp = os.path.join(work, "lines.pdf")
        versions.append(("text_as_lines", lp, dict(text_style=lines_text(pdf, lp, seed))))
    for kind, src, extra in versions:
        rules_ok = rules_build(src)
        if rules_ok and r.random() >= opts.easy:
            status["examples"].append(f"{kind}: dropped (the rules converter already builds it)")
            continue
        try:
            d = vlm.read_drawing(src, 1)
        except BaseException as e:                     # the converter cannot read it: vlm.py's picture route
            if isinstance(e, KeyboardInterrupt):
                raise
            recs.append(as_picture(src, kind, rules_ok=rules_ok, **extra))
            status["examples"].append(f"{kind}: picture route (not readable: {str(e)[:80]})")
            continue
        _, summ = assess(final, d)
        sheet = os.path.join(work, f"{kind}.png")
        open(sheet, "wb").write(d["image_png"])
        recs.append(rec(kind, vlm.drawing_prompt(d, brief), [sheet], route="measured", rules_ok=rules_ok,
                        check=dict(mean_overlap=summ["mean_overlap"], edges=summ["edges"]), **extra))
        status["examples"].append(f"{kind}: measured" + (" (rules converter builds it too)" if rules_ok else ""))
    if r.random() < opts.scans:
        sp = os.path.join(work, "scan.pdf")
        fake_scan(pdf, sp, seed)
        recs.append(as_picture(sp, "scan"))
        os.remove(sp)
        status["examples"].append("scan: picture route")
    for p in ("lines.pdf",):
        if os.path.exists(os.path.join(work, p)):
            os.remove(os.path.join(work, p))
    return recs, status


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gen"); ap.add_argument("out")
    ap.add_argument("--s2d", default=os.path.join(os.path.dirname(os.path.dirname(CONV)), "step2drawing"),
                    help="the step2drawing folder (default: next to this repo)")
    ap.add_argument("--scans", type=float, default=0.5, help="share of parts that also get a fake scan (0.5)")
    ap.add_argument("--lines", type=float, default=0.5, help="share of parts that also get a text-as-lines sheet (0.5)")
    ap.add_argument("--easy", type=float, default=0.15,
                    help="keep a sheet the rules converter already builds with this probability (0.15)")
    ap.add_argument("--full", action="store_true", help="full measured context (model server) instead of the brief one")
    ap.add_argument("--workers", type=int, default=max(1, min(6, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    if not os.path.exists(os.path.join(a.s2d, "convert.py")):
        raise SystemExit(f"step2drawing not found at {a.s2d} (use --s2d)")
    t0 = time.time()
    pdfs = draw_all(a.gen, a.s2d, a.workers)
    print(f"{len(pdfs)} drawings ({time.time() - t0:.0f} s); making the examples", flush=True)
    os.makedirs(a.out, exist_ok=True)
    from scan import init_worker
    rnd = random.Random(a.seed)
    jobs = [(pid, pdf, rnd.randrange(1 << 30)) for pid, pdf in sorted(pdfs.items())]
    files = {s: open(os.path.join(a.out, f"{s}.jsonl"), "w", encoding="utf-8") for s in ("train", "val")}
    log = open(os.path.join(a.out, "log.jsonl"), "w", encoding="utf-8")
    count, n = {}, 0
    with ProcessPoolExecutor(a.workers, initializer=init_worker, initargs=(a.workers,)) as ex:
        futs = [ex.submit(one, a.gen, pid, pdf, a.out, not a.full, a, sd) for pid, pdf, sd in jobs]
        for fu in as_completed(futs):
            n += 1
            try:
                recs, st = fu.result()
            except Exception as e:
                recs, st = [], dict(why=f"{type(e).__name__}: {e}")
            for split, rec in recs:
                files[split].write(json.dumps(rec, ensure_ascii=False) + "\n")
                key = f"{rec['kind']}/{rec['route']}"
                count[key] = count.get(key, 0) + 1
            log.write(json.dumps(st) + "\n"); log.flush()
            if n % 10 == 0 or n == len(jobs):
                print(f"{n}/{len(jobs)} parts, {sum(count.values())} examples", flush=True)
    for fh in files.values():
        fh.close()
    print(f"{sum(count.values())} examples from {len(jobs)} parts in {time.time() - t0:.0f} s: "
          + ", ".join(f"{k} {v}" for k, v in sorted(count.items())))


if __name__ == "__main__":
    main()
