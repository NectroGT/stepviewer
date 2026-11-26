"""Drawing -> 3D model with an open vision-language model (VLM) - LLaVA, Qwen-VL or InternVL - running on this
computer or on a server in your network, checked against the drawing.

    python vlm.py drawing.pdf -o out/                                   # rules first, the VLM only if needed
    python vlm.py drawing.pdf -o out/ --model OpenGVLab/InternVL3-2B-hf  # another model
    python vlm.py drawing.pdf -o out/ --vlm-only                        # skip the rules converter
    python vlm.py drawing.pdf -o out/ --url http://gpu-box:8000/v1 --model Qwen/Qwen2.5-VL-32B-Instruct

Two ways to run the model:
  in this Python process (default): Hugging Face transformers loads the model's weights (downloaded once from
      huggingface.co into its cache, then used offline). Needs: pip install torch transformers accelerate.
      Models (Hugging Face ids): Qwen/Qwen2.5-VL-3B-Instruct (default), Qwen/Qwen2.5-VL-7B-Instruct,
      OpenGVLab/InternVL3-1B-hf / -2B-hf / -8B-hf, llava-hf/llava-onevision-qwen2-0.5b-ov-hf / -7b-ov-hf,
      llava-hf/llava-1.5-7b-hf. Bigger models read drawings much better and need a GPU.
  a model server (--url): an OpenAI-compatible server such as vLLM or LMDeploy serving one of those models on a GPU
      machine. An address outside this computer and your private network is refused unless --allow-remote.
Either way the drawing stays on this computer or that server.

0. The rules converter (pdf2step) goes first; when its model passes the check (check.py), that is the result and the
   VLM is not used.
1. The converter reads the drawing (views, scale, units, text, hole notes) and puts every view's outline, edges
   and circles into one model frame in millimetres (see check.py).
2. The VLM gets the sheet as an image plus those exact numbers, and writes a CadQuery build() function.
3. The code runs in a separate Python process (time-limited). Its model is projected back into the top, front
   and end views and compared with the drawing's outlines and edges. The errors, the scores and a picture of the
   model's views over the drawing's outlines go back to the VLM, which corrects the code. The best attempt is kept.
   A MATCHES answer the measured check disagrees with is refused.

A page that is only a picture (a scan or photo saved as PDF) goes straight to the VLM: it gets the sheet, its four
quarters enlarged and the text OCR found, takes every size from the dimension text, and checks its model by eye.

Writes out/model.step (millimetres), out/attempt_N.py, out/attempt_N.png and out/report.json ("matched": whether the
saved model passed the check). The code the VLM writes runs on this computer, in its own process with a time limit.
"""
import argparse, base64, ipaddress, json, math, os, re, socket, subprocess, sys, tempfile, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"  # Hugging Face id (in-process) or the name a --url server serves

SYSTEM = """You turn engineering drawings into 3D models written as CadQuery (Python) code.

You get one drawing sheet as an image, and what a measuring program read from it: the views, the scale, every view's
outline, edges and circles in millimetres, and the text on the sheet. Build the part those views describe.

Model frame, in millimetres: x runs along the part as the front view shows it (left to right), y runs from the front
face backwards, z is up. Put the part's lowest corner at the origin, so it lies in x >= 0, y >= 0, z >= 0.
The views (third angle projection):
- top view: looks down, shows (x, y)
- front view: looks from the front, shows (x, z)
- end view: looks from the right, shows (y, z)
All coordinates you are given are already in this frame and in millimetres.

How to work:
- Take sizes from the given coordinates and from the dimension text, never by eye from the image. The coordinates are
  measured from the drawing's lines and are exact to about 0.05 mm; dimension text is exact.
- Work out each feature from all the views together. A solid line is a visible edge; a dashed (hidden) line is an edge
  behind material (an internal hole, pocket, slot or undercut). A slanted line in one view is a sloped face seen
  edge-on. A closed area in a view can be material, a hole, or a sloped face seen at an angle - check the other views.
- Hole notes apply to every hole of that kind ("2X" = two holes). CSK = countersink (diameter X included angle), CBORE
  or the counterbore sign = counterbore, THRU = through. Countersinks open on the face whose view shows the cone's wide end.
- Radii (R) round corners or ends as drawn; a chamfer note gives size x angle.

Answer with one Python code block that defines build(), which returns a cadquery Workplane or Shape of the single
solid part, in millimetres. Use only cadquery (as cq) and math. Keep the code plain: build the main body, then add
and cut features. Before the code block, list in a few lines the features you found and the views that show them."""


PICTURE_NOTE = """This drawing sheet is only a picture (a scan or photo): no lines or text could be measured from it, so
there are no coordinates. Take every size from the dimension text, never by scaling the picture. Use the units the
sheet states (inch dimensions: multiply by 25.4 for millimetres; fractions like 5/8 are inches on an inch drawing).
Tolerances (+0.003/+0.001, K7 and so on): build the nominal size. A section view (cutting line A-A) shows the part
cut open: hatched areas are material cut by the plane, unhatched gaps inside are holes and pockets. Choose as the
front view the drawing view that carries most of the dimensions, and keep the model frame described above."""


# ------------------------------------------------------------------ reading the drawing

def read_drawing(pdf, page=1, scale=None):
    """the converter's reading of one sheet: views in the model frame (mm), text, notes and a sheet image.
    scale: 1:scale as entered by the user (None: from the title block / the dimensions)"""
    import pymupdf
    from extract import page_prims
    from views import find_views
    from recognize import faces, group_parts
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from text import read_words, lines, sheet_scale, dimension_scale, drawing_units, callouts
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

    def r2(p):
        return [round(float(p[0]), 2), round(float(p[1]), 2)]

    out_views = {}
    for name, (v, M) in frames.items():
        fs = faces(list(v.prims))
        outline = None
        if fs:
            u = unary_union(fs)
            u = max(u.geoms, key=lambda g: g.area) if u.geom_type == "MultiPolygon" else u
            outline = Polygon([M(q) for q in u.exterior.coords]).buffer(0)
        visible, hidden, circles = [], [], []
        edges = [[M(q) for q in (p.pts if p.kind != "line" else (p.pts[0], p.pts[-1]))]   # visible edges, for
                 for p in v.prims]                                                   # the edge check
        for group, dest in ((v.prims, visible), (v.thin, hidden)):
            for p in group:
                if p.kind == "line":
                    a, b = M(p.pts[0]), M(p.pts[-1])
                    if math.dist(a, b) > 0.3:
                        dest.append([r2(a), r2(b)])
                elif p.kind == "circle" or (p.kind == "arc" and len(p.pts) > 2):
                    c = M((p.cx, p.cy))
                    rec = dict(center=r2(c), diameter=round(2 * p.r * k, 2), hidden=group is v.thin)
                    if p.kind == "arc":
                        rec.update(arc_from=r2(M(p.pts[0])), arc_to=r2(M(p.pts[-1])))
                    circles.append(rec)
        out_views[name] = dict(
            size=[round(float((v.x1 - v.x0) * k), 2), round(float((v.y1 - v.y0) * k), 2)],
            outline=outline, edges=edges,
            outline_points=[r2(q) for q in outline.simplify(0.02).exterior.coords] if outline is not None else None,
            visible_lines=_dedupe(visible)[:400], hidden_or_thin_lines=_dedupe(hidden)[:400], circles=circles[:200],
            sheet_box=[round(v.x0, 2), round(v.y0, 2), round(v.x1, 2), round(v.y1, 2)])

    texts = [dict(text=L["text"], at_sheet_inch=[round((L["x0"] + L["x1"]) / 2, 2), round(L["yc"], 2)]) for L in tl]
    notes = [dict(text=n["text"], parsed=n["info"]) for n in callouts(tl)]

    doc = pymupdf.open(pdf)
    pg = doc[page - 1]
    zoom = 2000 / max(pg.rect.width, pg.rect.height)
    png = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
    return dict(units="mm" if mm else "inch (values below are converted to mm)", scale=f"1:{scale:.4g} ({scale_note})",
                views=out_views, texts=texts, hole_notes=notes, image_png=png,
                unused_views=[repr(v) for v in views if id(v) not in used and (not ev or v is not ev[0]) and not v.iso])


def is_picture(pdf, page=1):
    """the page is only a picture (a scan or photo saved as PDF): no drawn lines or text to measure"""
    import pymupdf
    pg = pymupdf.open(pdf)[page - 1]
    return bool(pg.get_images()) and len(pg.get_drawings()) < 20 and len(pg.get_text("words")) < 5


_OCR = None


def read_picture(pdf, page, out):
    """a picture-only sheet: the image (whole and in four enlarged quarters, for the small text) and the text OCR
    finds on it, with pixel positions in the whole image. Nothing can be measured from it."""
    import io
    import numpy as np
    import pymupdf
    from PIL import Image
    global _OCR
    doc = pymupdf.open(pdf)
    pg = doc[page - 1]
    xref = max(pg.get_images(full=True), key=lambda im: im[2] * im[3])[0]
    im = Image.open(io.BytesIO(doc.extract_image(xref)["image"])).convert("RGB")
    if max(im.size) > 2000:
        f = 2000 / max(im.size)
        im = im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    W, H = im.size
    tiles = []
    for i, (x0, y0) in enumerate(((0, 0), (0.45, 0), (0, 0.45), (0.45, 0.45))):
        box = (int(x0 * W), int(y0 * H), int((x0 + 0.55) * W), int((y0 + 0.55) * H))
        t = im.crop(box)
        f = 1800 / max(t.size)
        t = t.resize((round(t.width * f), round(t.height * f)), Image.LANCZOS)
        path = os.path.join(out, f"sheet_part{i + 1}.png")
        t.save(path)
        tiles.append(path)
    texts = []
    try:
        if _OCR is None:
            try:
                from rapidocr import RapidOCR
                _OCR = (RapidOCR(params={"Global.log_level": "warning"}), True)
            except ImportError:
                from rapidocr_onnxruntime import RapidOCR
                _OCR = (RapidOCR(), False)
        big = im.resize((W * 2, H * 2), Image.LANCZOS)
        r = _OCR[0](np.array(big))
        res = (list(zip(r.boxes, r.txts, r.scores)) if r.boxes is not None else []) if _OCR[1] else (r[0] or [])
        for box, t, conf in res:
            if float(conf) >= 0.5 and t.strip():
                xs = [p[0] / 2 for p in box]
                ys = [p[1] / 2 for p in box]
                texts.append(dict(text=t.strip(), at_px=[round(sum(xs) / 4), round(sum(ys) / 4)]))
    except Exception:                                 # OCR is a help only: the VLM reads the image anyway
        pass
    return dict(picture=True, views={}, image_png=buf.getvalue(), tiles=tiles, texts=texts, size_px=[W, H])


def _dedupe(segs):
    seen, out = set(), []
    for a, b in segs:
        key = tuple(sorted((tuple(a), tuple(b))))
        if key not in seen:
            seen.add(key); out.append([a, b])
    return out


def context_text(d, brief=False):
    """the measured facts sent with the image (brief: fewer lines, for models with a short context)"""
    views = {}
    n = 60 if brief else 400
    for name, v in d["views"].items():
        views[name] = {k: v[k] for k in ("size", "outline_points", "circles")}
        views[name].update(visible_lines=v["visible_lines"][:n], hidden_or_thin_lines=v["hidden_or_thin_lines"][:n])
    return json.dumps(dict(units=d["units"], scale=d["scale"],
                           views_in_model_frame_mm=views,
                           note_on_lines="hidden_or_thin_lines holds hidden (dashed) edges and also dimension, centre "
                                         "and leader lines that lie inside the view's box",
                           hole_notes=d["hole_notes"], all_text_on_sheet=d["texts"][:80 if brief else 1000],
                           other_views_not_placed=d["unused_views"]),
                      default=lambda o: round(float(o), 3) if hasattr(o, "__float__") else str(o))


# ------------------------------------------------------------------ running a candidate

RUNNER = r'''
import json, sys, traceback, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, sys.argv[3])
out = {}
try:
    import cadquery as cq
    ns = {"cq": cq, "__name__": "candidate"}
    exec(compile(open(sys.argv[1], encoding="utf-8").read(), "candidate.py", "exec"), ns)
    r = ns["build"]()
    s = r.val() if hasattr(r, "val") else r
    if hasattr(s, "Solids") and len(s.Solids()) > 1:
        s = cq.Compound.makeCompound(s.Solids())
    from check import moved_to_origin
    s = moved_to_origin(s)
    cq.exporters.export(cq.Workplane().add(s), sys.argv[2])
    bb = s.BoundingBox()
    out = dict(ok=True, size=[round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)], volume=round(s.Volume(), 2),
               solids=len(s.Solids()), valid=s.isValid())
except Exception:
    out = dict(ok=False, error=traceback.format_exc()[-3000:])
print("RESULT " + json.dumps(out))
'''


def run_candidate(code, step_path, timeout=180):
    """run build() in a separate Python process; returns its result dict"""
    with tempfile.TemporaryDirectory() as td:
        src, runner = os.path.join(td, "candidate.py"), os.path.join(td, "runner.py")
        open(src, "w", encoding="utf-8").write(code)
        open(runner, "w", encoding="utf-8").write(RUNNER)
        try:
            p = subprocess.run([sys.executable, runner, src, step_path, HERE], capture_output=True, text=True,
                               timeout=timeout, cwd=td)
        except subprocess.TimeoutExpired:
            return dict(ok=False, error=f"build() did not finish within {timeout} s")
        line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            return dict(ok=False, error=(p.stderr or p.stdout)[-3000:] or "the code produced no result")
        return json.loads(line[len("RESULT "):])


from check import MATCH, EDGE_MATCH, assess, oriented, render_views, moved_to_origin   # the no-AI check


def check(step_path, d, png_path, title):
    """compare the built model with the drawing's views; returns (scores, summary) and draws the picture"""
    import cadquery as cq
    s = cq.importers.importStep(step_path).val()
    scores, summary = assess(s, d)
    render_views(s, d["views"], png_path, title)
    return scores, summary


def edge_notes(scores):
    """the edge differences in words, for the VLM"""
    out = []
    for name, sc in scores.items():
        e = sc.get("edges", {})
        if "drawing_explained" not in e:
            continue
        if e["drawing_explained"] < 0.995 and e["missing"] > 0.5:
            out.append(f"{name} view: {e['missing']} mm of the drawing's lines have no model edge on them "
                       f"(a feature is missing or misplaced), near {e['missing_at']}")
        if e["model_explained"] < 0.9 and e["extra"] > 0.5:
            out.append(f"{name} view: {e['extra']} mm of the model's visible edges are not on the drawing "
                       f"(extra or misplaced, or an edge the drawing leaves out), near {e['extra_at']}")
    return out


# ------------------------------------------------------------------ talking to the VLM

def check_host(url, allow_remote=False):
    """refuse an address outside this computer and the private network (drawings must not leave it)"""
    name = urllib.parse.urlparse(url).hostname
    if not name:
        raise SystemExit(f"not an address: {url}")
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(name, None)}
    except socket.gaierror as e:
        raise SystemExit(f"cannot find {name}: {e}")
    ip = lambda a: ipaddress.ip_address(a.split("%")[0])
    outside = [a for a in addrs if not (ip(a).is_private or ip(a).is_loopback)]
    if outside and not allow_remote:
        raise SystemExit(f"{url} is outside this computer and your private network ({', '.join(sorted(outside))}): "
                         "drawings would leave it. Use a local or private-network address, or --allow-remote.")


def _small(path, side):
    """the image, no longer than side pixels (vision models turn every pixel block into tokens)"""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if max(im.size) > side:
        f = side / max(im.size)
        im = im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS)
    return im


class LocalChat:
    """a vision model (Qwen-VL, InternVL, LLaVA ...) run in this process by Hugging Face transformers. The weights
    come from huggingface.co once (then the local cache); drawings are never sent anywhere."""
    name = "transformers"
    brief = True

    def __init__(self, model=DEFAULT_MODEL, max_new_tokens=3072, image_side=1344, dtype="auto", **_):
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        try:
            import torch
            from transformers import AutoProcessor, AutoModelForImageTextToText
        except ImportError:
            raise SystemExit("running a model in this process needs: pip install torch transformers accelerate")
        self.torch = torch
        gpu = torch.cuda.is_available()
        # auto: bf16 on a GPU; fp32 on a CPU (most CPUs have no bf16 arithmetic: bf16 there is several times slower)
        dt = {"auto": torch.bfloat16 if gpu else torch.float32, "bf16": torch.bfloat16, "fp16": torch.float16,
              "fp32": torch.float32}[dtype]
        t0 = time.time()
        self.processor = AutoProcessor.from_pretrained(model)
        self.model = AutoModelForImageTextToText.from_pretrained(model, dtype=dt,
                                                                 device_map="auto" if gpu else None)
        if not gpu:
            self.model = self.model.to("cpu")
        self.model.eval()
        self.model_id, self.device = model, ("cuda" if gpu else "cpu")
        self.max_new_tokens, self.image_side = max_new_tokens, image_side
        self.load_seconds = round(time.time() - t0, 1)
        self.messages = [{"role": "system", "content": [{"type": "text", "text": SYSTEM}]}]
        self.tokens_in = self.tokens_out = 0
        self.seconds = 0.0

    def send(self, text, images=()):
        # earlier pictures are dropped from the conversation (each costs thousands of tokens): their text stays
        for m in self.messages:
            if m["role"] == "user":
                m["content"] = [c if c["type"] == "text" else {"type": "text", "text": "(a picture shown earlier)"}
                                for c in m["content"]]
        content = [{"type": "image", "image": _small(p, self.image_side)} for p in images]
        self.messages.append({"role": "user", "content": content + [{"type": "text", "text": text}]})
        inputs = self.processor.apply_chat_template(self.messages, add_generation_prompt=True, tokenize=True,
                                                    return_dict=True, return_tensors="pt").to(self.model.device)
        t0 = time.time()
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False)
        self.seconds += time.time() - t0
        n_in = inputs["input_ids"].shape[1]
        answer = self.processor.decode(out[0, n_in:], skip_special_tokens=True)
        self.tokens_in += int(n_in)
        self.tokens_out += int(out.shape[1] - n_in)
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": answer}]})
        return answer

    def report(self):
        return dict(backend=self.name, model=self.model_id, device=self.device, load_seconds=self.load_seconds,
                    input_tokens=self.tokens_in, output_tokens=self.tokens_out, model_seconds=round(self.seconds, 1))


class ServerChat:
    """a vision model on an OpenAI-compatible server in your network (vLLM, LMDeploy, SGLang, llama.cpp ...)"""
    name = "server"
    brief = False

    def __init__(self, model=DEFAULT_MODEL, url=None, max_new_tokens=4096, image_side=1600, allow_remote=False,
                 timeout=3600, **_):
        self.url = url.rstrip("/") if "://" in url else "http://" + url.rstrip("/")
        check_host(self.url, allow_remote)
        self.model_id, self.max_new_tokens, self.image_side, self.timeout = model, max_new_tokens, image_side, timeout
        self.messages = [{"role": "system", "content": SYSTEM}]
        self.tokens_in = self.tokens_out = 0
        self.seconds = 0.0

    def send(self, text, images=()):
        import io
        parts = []
        for p in images:
            buf = io.BytesIO()
            _small(p, self.image_side).save(buf, "PNG")
            parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(buf.getvalue())}})
        self.messages.append({"role": "user", "content": parts + [{"type": "text", "text": text}]})
        body = json.dumps(dict(model=self.model_id, messages=self.messages, max_tokens=self.max_new_tokens,
                               temperature=0.2)).encode()
        req = urllib.request.Request(self.url + "/chat/completions", body, {"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"model server {self.url}: {e.code} {e.read().decode('utf-8', 'replace')[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach the model server at {self.url} ({e.reason})")
        self.seconds += time.time() - t0
        answer = out["choices"][0]["message"].get("content") or ""
        self.messages.append({"role": "assistant", "content": answer})
        u = out.get("usage") or {}
        self.tokens_in += u.get("prompt_tokens") or 0
        self.tokens_out += u.get("completion_tokens") or 0
        return answer

    def report(self):
        return dict(backend=self.name, url=self.url, model=self.model_id, input_tokens=self.tokens_in,
                    output_tokens=self.tokens_out, model_seconds=round(self.seconds, 1))


_LOADED = {}


def make_chat(opts):
    """ServerChat with a url, else LocalChat (the loaded model is kept for the next drawing)"""
    opts = dict(opts or {})
    if opts.get("url"):
        return ServerChat(**opts)
    key = (opts.get("model") or DEFAULT_MODEL, opts.get("dtype", "auto"))
    if key not in _LOADED:
        _LOADED[key] = LocalChat(**opts)
    chat = _LOADED[key]
    chat.messages = chat.messages[:1]                 # a new conversation with the same weights
    chat.tokens_in = chat.tokens_out = 0
    chat.seconds = 0.0
    return chat


# ------------------------------------------------------------------ the loop

def _code_of(text):
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    blocks = [b for b in blocks if "def build" in b]
    return blocks[-1] if blocks else None


def _b64(data):
    return base64.standard_b64encode(data).decode("ascii")


def _same_code(a, b):
    norm = lambda c: "\n".join(l.rstrip() for l in c.strip().splitlines() if l.strip())
    return norm(a) == norm(b)


def generate(pdf, out, page=1, rounds=3, review=True, log=print, scale=None, chat=None):
    """the VLM builds the part; chat: options for make_chat (model, url, allow_remote, max_new_tokens, dtype)"""
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    picture = is_picture(pdf, page)
    d = read_picture(pdf, page, out) if picture else read_drawing(pdf, page, scale)
    sheet = os.path.join(out, "sheet.png")
    open(sheet, "wb").write(d["image_png"])
    log("loading the model" if not (chat or {}).get("url") else f"model server {(chat or {})['url']}")
    chat = make_chat(chat)
    if picture:
        log(f"read the drawing: a picture (scan or photo), no lines to measure; OCR found {len(d['texts'])} texts")
        text = (PICTURE_NOTE + "\nThe first image is the whole sheet; the next four are its quarters (top left, top "
                "right, bottom left, bottom right), enlarged so the small text can be read.\nText an OCR program found "
                "on the sheet (it can misread; positions are pixels in the first image, "
                f"{d['size_px'][0]} x {d['size_px'][1]}):\n" + json.dumps(d["texts"])
                + "\n\nBuild the part. Answer with the feature list and one code block defining build().")
        images = [sheet] + d["tiles"]
    else:
        open(os.path.join(out, "context.json"), "w", encoding="utf-8").write(context_text(d, chat.brief))
        log(f"read the drawing: views {sorted(d['views'])}, scale {d['scale']}")
        text = ("The image is the drawing sheet. Measured from this drawing (model frame, mm):\n" + context_text(d, chat.brief)
                + "\n\nBuild the part. Answer with the feature list and one code block defining build().")
        images = [sheet]
    report = dict(drawing=os.path.abspath(pdf), page=page, rounds=[])
    best, prev_code, stop, worse = None, None, None, 0
    last_match, last_text = None, ""      # the measured check of the newest attempt, and what was sent about it
    reviewed = not review                     # with review on, a model whose outlines match gets one inner-edge check
    for n in range(1, rounds + 1):
        try:
            answer = chat.send(text, images)
        except Exception as e:
            report["rounds"].append(dict(round=n, error=f"{type(e).__name__}: {e}"))
            log(f"round {n}: {type(e).__name__}: {e}")
            break
        open(os.path.join(out, f"answer_{n}.txt"), "w", encoding="utf-8").write(answer)   # what the VLM said
        code = _code_of(answer)
        rec = dict(round=n)
        images = []
        if code is None and re.search(r"\bMATCHES\b", answer) and (picture or last_match):
            stop = "the VLM checked its model against the drawing and found it matches"
        elif code is None and re.search(r"\bMATCHES\b", answer):
            # the measured check disagrees (or nothing is built yet): a VLM's MATCHES alone does not end it
            rec["error"] = "answered MATCHES, but the check says the model does not match the drawing"
            if best:
                text = ("The check measured that your model does NOT match the drawing (see the scores and notes "
                        "below). Do not answer MATCHES while the check disagrees. Correct the model: answer with the "
                        "changes and one code block defining build().\n\n" + last_text)
                images = [os.path.join(out, f"attempt_{best[1]}.png"), sheet]
            else:
                text = "Nothing is built yet. Answer with one Python code block defining build()."
        elif code is not None and prev_code is not None and _same_code(code, prev_code):
            stop = "the code did not change"
        elif code is None:
            rec["error"] = "no code block with build()"
            text = "I found no Python code block defining build(). Answer with one."
        else:
            prev_code = code
            open(os.path.join(out, f"attempt_{n}.py"), "w", encoding="utf-8").write(code)
            step = os.path.join(out, f"attempt_{n}.step")
            res = run_candidate(code, step)
            rec["run"] = res
            if not res.get("ok"):
                text = ("Running build() failed:\n" + res["error"]
                        + "\n\nFix the code. Answer with one code block defining build().")
            else:
                png = os.path.join(out, f"attempt_{n}.png")
                problems = []
                if res.get("solids", 1) != 1:
                    problems.append(f"the result has {res['solids']} separate solids; the part is one solid")
                if not res.get("valid", True):
                    problems.append("the solid is not valid (check booleans and coincident faces)")
                if picture:                           # nothing measured to score against: the VLM compares by eye
                    import cadquery as cq
                    render_views(moved_to_origin(cq.importers.importStep(step).val()), {}, png, f"attempt {n}",
                                 iso=True)
                    if best is None or not problems:
                        best = (0.0, n, step)         # the newest attempt that builds one valid solid
                    images = [png, sheet]
                    text = ("The first image is your model, drawn as top, front and end views and an isometric view "
                            "(black visible edges, blue dashed hidden edges); the second is the drawing sheet again.\n"
                            f"Built size (x, y, z): {res['size']} mm, volume {res['volume']} mm3."
                            + ("\nAlso: " + "; ".join(problems) if problems else "") + "\n\n"
                            "Compare them feature by feature with every view of the drawing (sections and 3D views "
                            "included): overall sizes against the dimensions, every hole, slot, step, pocket, chamfer "
                            "and its position. If the model matches the drawing, answer with the single word MATCHES "
                            "and no code. Otherwise answer with the changes and one code block defining build().")
                else:
                    scores, summ = check(step, d, png, f"attempt {n}")
                    mean = summ["score"]
                    rec.update(scores=scores, mean_overlap=summ["mean_overlap"], edges=summ["edges"], score=mean)
                    matched = summ["matched"] and not problems
                    before = best[0] if best else None
                    # keep the newest attempt that is as good: a correction the checks cannot see keeps the score
                    if best is None or mean >= best[0] - 0.0005:
                        best = (mean, n, step)
                    rec["views_match"] = matched
                    last_match = matched
                    # one weaker attempt gets a second chance from the best code; two in a row end it
                    worse = worse + 1 if not matched and before is not None and mean <= before + 0.0005 else 0
                    back = ""
                    if matched and reviewed:
                        stop = "the outlines and edges match the drawing"
                    elif worse >= 2:
                        stop = "two attempts in a row did not improve on the best one"
                    elif worse and best[1] != n:
                        back = (f"This attempt scores {mean:.4f}, lower than attempt {best[1]} ({best[0]:.4f}). Start again "
                                f"from attempt {best[1]}'s code (below) and change only what the notes above point to:\n"
                                "```python\n" + open(os.path.join(out, f"attempt_{best[1]}.py"), encoding="utf-8").read()
                                + "\n```\n\n")
                    images = [png, sheet]
                    head = ("The first image is your model, drawn as top, front and end views (black visible edges, blue "
                            "dashed hidden edges) over the drawing's outline of each view (red); the second is the drawing "
                            "sheet again.\n"
                            f"Built size (x, y, z): {res['size']} mm, volume {res['volume']} mm3.\n"
                            "Agreement per view (outline overlap 1.0 = same, areas in mm2; edges: drawing_explained = "
                            "share of the drawing's visible lines lying on a model edge, model_explained = the other way "
                            "round, places as (x, y) in that view):\n" + json.dumps(scores)
                            + "".join("\n- " + t for t in edge_notes(scores))
                            + ("\nAlso: " + "; ".join(problems) if problems else "") + "\n\n" + back)
                    if matched:
                        reviewed = True
                        text = head + ("The outlines and edges match. Now check everything inside them against the sheet: holes, "
                                       "counterbores and countersinks, pockets, slots and channels, steps, and the hidden "
                                       "(dashed) edges. If the model matches the drawing, answer with the single word "
                                       "MATCHES and no code. Otherwise answer with the changes and one code block defining "
                                       "build().")
                    else:
                        text = head + ("Compare them, edges inside the views included, not just the outlines. Correct what "
                                       "differs from the drawing: answer with the changes you make and one code block "
                                       "defining build(). If you are sure the model already matches the drawing, answer "
                                       "with the single word MATCHES and no code.")
                    last_text = head
        if stop:
            rec["stopped"] = stop
        report["rounds"].append(rec)
        if "run" not in rec:
            msg = rec.get("error") or stop
        elif not rec["run"].get("ok"):
            msg = "build failed"
        else:
            msg = ((f"overlap {rec['mean_overlap']:.4f}, edges {rec['edges']:.3f}" if "edges" in rec else "built")
                   + (f" - {stop}" if stop else ""))
        log(f"round {n}: {msg}")
        if stop:
            break
    report["stopped"] = stop or (f"reached the round limit ({rounds})" if best else "no attempt built a model")

    if best:
        import shutil
        shutil.copyfile(best[2], os.path.join(out, "model.step"))
        report.update(best_round=best[1], best_score=round(best[0], 4), model_step=os.path.join(out, "model.step"))
        # did the saved model pass the measured check? (None for a picture: only the VLM's own comparison)
        rec = next((r for r in report["rounds"] if r.get("round") == best[1]), {})
        report["matched"] = None if picture else bool(rec.get("views_match"))
    report["route"] = "vlm"
    report.update(chat.report())
    report["seconds"] = round(time.time() - t0, 1)
    json.dump(report, open(os.path.join(out, "report.json"), "w", encoding="utf-8"), indent=1, default=str)
    return report


def rules_attempt(pdf, out, page, d, scale=None):
    """the rules converter's model of the page, checked like the VLM's attempts: (step path, scores, summary), or
    (None, None, dict(error=...)) when it builds nothing"""
    import cadquery as cq
    from pdf2step import run
    rdir = os.path.join(out, "rules")
    try:
        res = run(pdf, page, scale, rdir)
    except BaseException as e:                       # SystemExit too: run() exits when it cannot read the scale
        return None, None, dict(error=f"{type(e).__name__}: {e}")
    built = [r for r in res if "size_mm" in r]
    if not built:
        return None, None, dict(error=next((r.get("error") or r.get("note") for r in res), None) or "nothing built")
    step = os.path.join(rdir, built[0]["name"] + ".step")
    try:
        s, scores, summ = oriented(cq.importers.importStep(step).val(), d)
        s = moved_to_origin(s)
        step = os.path.join(rdir, "model_frame.step")      # the model turned into the drawing's frame
        cq.exporters.export(s, step)
        render_views(s, d["views"], os.path.join(out, "rules.png"), "rules converter")
    except Exception as e:
        return None, None, dict(error=f"checking failed: {type(e).__name__}: {e}")
    return step, scores, summ


def convert(pdf, out, page=1, rounds=3, review=True, log=print, scale=None, chat=None):
    """the free rules converter first; the VLM (generate) only when its model does not match the drawing's
    outlines and edges. Writes out/model.step either way; the report says which route made it ("route": rules or vlm)."""
    import shutil
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    if is_picture(pdf, page):                         # a scan or photo: the rules converter has nothing to read
        log("the page is a picture (scan or photo): straight to the VLM")
        report = generate(pdf, out, page, rounds, review, log, scale, chat)
        report.update(route="vlm", rules=dict(error="the page is a picture: no lines to read"),
                      seconds=round(time.time() - t0, 1))
        json.dump(report, open(os.path.join(out, "report.json"), "w", encoding="utf-8"), indent=1, default=str)
        return report
    d = read_drawing(pdf, page, scale)
    step, scores, summ = rules_attempt(pdf, out, page, d, scale)
    rules = dict(summ, scores=scores) if scores else summ
    if step and summ.get("matched"):
        shutil.copyfile(step, os.path.join(out, "model.step"))
        log(f"rules converter: overlap {summ['mean_overlap']:.4f}, edges {summ['edges']:.3f} - matches, the VLM is not needed")
        report = dict(drawing=os.path.abspath(pdf), page=page, route="rules", matched=True, rules=rules, rounds=[],
                      stopped="the rules converter's model matches the drawing",
                      model_step=os.path.join(out, "model.step"), seconds=round(time.time() - t0, 1))
        json.dump(report, open(os.path.join(out, "report.json"), "w", encoding="utf-8"), indent=1, default=str)
        return report
    log("rules converter: " + (f"overlap {summ['mean_overlap']:.4f}, edges {summ['edges']:.3f} - does not match"
                               if step else summ["error"]) + "; asking the VLM")
    report = generate(pdf, out, page, rounds, review, log, scale, chat)
    report.update(route="vlm", rules=rules, seconds=round(time.time() - t0, 1))
    json.dump(report, open(os.path.join(out, "report.json"), "w", encoding="utf-8"), indent=1, default=str)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Hugging Face id of a Qwen-VL / InternVL / LLaVA model (default {DEFAULT_MODEL}), or the "
                         "name the --url server serves")
    ap.add_argument("--url", help="an OpenAI-compatible model server in your network, e.g. http://gpu-box:8000/v1 "
                                  "(vLLM, LMDeploy); without it the model runs in this process")
    ap.add_argument("--allow-remote", action="store_true",
                    help="allow a --url outside this computer and your private network")
    ap.add_argument("--max-new-tokens", type=int, default=3072, help="longest answer")
    ap.add_argument("--dtype", default="auto", choices=["auto", "bf16", "fp16", "fp32"],
                    help="in-process: number format of the weights (auto = bf16 on a GPU, fp32 on a CPU)")
    ap.add_argument("--no-review", action="store_true",
                    help="stop as soon as the outlines match, without asking the VLM to check the edges inside them")
    ap.add_argument("--vlm-only", action="store_true", help="skip the rules converter and go straight to the VLM")
    a = ap.parse_args()
    chat = dict(model=a.model, url=a.url, allow_remote=a.allow_remote, max_new_tokens=a.max_new_tokens, dtype=a.dtype)
    r = (generate if a.vlm_only else convert)(a.pdf, a.out, a.page, a.rounds, review=not a.no_review, chat=chat)
    print(json.dumps({k: r.get(k) for k in ("route", "matched", "best_round", "best_score", "stopped", "model_step",
                                            "backend", "model", "seconds")}))


if __name__ == "__main__":
    main()
