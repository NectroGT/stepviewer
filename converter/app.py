"""Web front end for pdf2step: upload a PDF, scan every page for parts that can be built, convert them,
preview them in 3D and download the STEP files.

usage: python app.py            then open http://127.0.0.1:5000
Pages are scanned and converted side by side in worker processes (scan.workers() of them)."""
import collections, io, json, os, shutil, sys, threading, time, traceback, uuid, warnings, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")

import mimetypes
mimetypes.add_type("application/wasm", ".wasm")   # Windows' registry often lacks it; WebAssembly needs it
mimetypes.add_type("text/javascript", ".js")
import pymupdf
from flask import Flask, jsonify, request, send_file, send_from_directory, abort, redirect

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.environ.get("PDF2STEP_JOBS") or os.path.join(HERE, "web_jobs")   # the desktop app keeps them in its user folder
# the STP viewer from the same repo (stepviewer/site): served at /viewer/ so converted parts open in it
SITE = os.environ.get("PDF2STEP_SITE") or os.path.normpath(os.path.join(HERE, "..", "site"))
HAS_VIEWER = os.path.isfile(os.path.join(SITE, "index.html"))
os.makedirs(JOBS, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

jobs = {}                 # job id -> state dict (kept in memory; files live in web_jobs/<id>/)
futures = {}              # (job id, page) -> Future of a running conversion
lock = threading.RLock()  # re-entrant: start_convert runs with it held and pool() takes it too
_pool = None


def pool():
    """one pool of worker processes for scans and conversions, started on first use"""
    global _pool
    with lock:
        if _pool is None:
            import scan
            _pool = scan.pool()
        return _pool


# Work waits here, not in the pool's own first-come queue, so a conversion you asked for goes ahead of
# pages still waiting to be scanned. Only as many tasks as there are workers are handed to the pool.
_waiting = {"convert": collections.deque(), "scan": collections.deque()}
_running = 0


def schedule(kind, fn, args, kwargs, on_start, on_done):
    """queue fn(*args, **kwargs) for a worker; on_start(future) runs when a worker takes it (lock held),
    on_done(future) when it has finished"""
    with lock:
        _waiting[kind].append((fn, args, kwargs, on_start, on_done))
        _dispatch()


def _dispatch():
    global _running
    import scan
    with lock:
        while _running < scan.workers() and (_waiting["convert"] or _waiting["scan"]):
            fn, args, kwargs, on_start, on_done = (_waiting["convert"] or _waiting["scan"]).popleft()
            _running += 1
            fut = pool().submit(fn, *args, **kwargs)
            on_start(fut)
            fut.add_done_callback(lambda f, cb=on_done: _finished(f, cb))


def _finished(fut, on_done):
    global _running
    try:
        on_done(fut)
    except Exception:
        traceback.print_exc()
    finally:
        with lock:
            _running -= 1
            _dispatch()


# ---------------------------------------------------------------- scanning and converting

def start_scan(job):
    import scan
    order = scan.biggest_first(job["pdf"])
    left = [len(order)]

    def done(fut, n):
        try:
            r = fut.result()
        except Exception as e:
            r = dict(status="error", reason=f"could not read the page: {type(e).__name__}: {e}", words=None)
        words = r.pop("words", None)
        if words is not None:
            try:
                with open(os.path.join(job["dir"], f"words_{n}.json"), "w", encoding="utf-8") as f:
                    json.dump(words, f)
            except Exception:                  # the conversion then reads the text itself
                traceback.print_exc()
        with lock:
            p = job["pages"][n - 1]
            p.update({k: v for k, v in r.items() if k != "page"})
            p["scan"] = r["status"]
            left[0] -= 1
            if not left[0]:
                job["scanned"] = True; job["scan_time"] = round(time.time() - job["created"], 1)

    for i in order:                            # pages show "waiting" until a worker takes them
        schedule("scan", scan.scan_page, (job["pdf"], i), {},
                 lambda fut, i=i: job["pages"][i].update(scan="scanning"),
                 lambda fut, n=i + 1: done(fut, n))


def start_convert(job, n):
    """queue page n for conversion (called with the lock held)"""
    from check import convert_and_check
    p = job["pages"][n - 1]
    if p.get("convert") in ("queued", "converting"):
        return
    p["convert"] = "queued"; p.pop("convert_error", None); p.pop("parts_out", None); p.pop("extra", None)
    words = None
    wpath = os.path.join(job["dir"], f"words_{n}.json")
    if os.path.exists(wpath):                  # the text read by the scan: no second OCR
        with open(wpath, encoding="utf-8") as f:
            words = [tuple(w) for w in json.load(f)]
    out = os.path.join(job["dir"], f"p{n:02d}")

    def started(fut):                          # a worker has taken it: shows as "converting"
        futures[(job["id"], n)] = fut

    def done(fut):
        with lock:
            futures.pop((job["id"], n), None)
            try:
                results = fut.result()
            except Exception as e:
                traceback.print_exception(e)
                p["convert"] = "failed"; p["convert_error"] = str(e) or type(e).__name__
                return
            parts = [dict(r, built="size_mm" in r and os.path.exists(os.path.join(out, r["name"] + ".step")))
                     for r in results if "size_mm" in r or "error" in r]
            p["parts_out"] = parts
            p["extra"] = [r for r in results if "size_mm" not in r and "error" not in r]
            p["convert"] = "done" if any(x["built"] for x in parts) else "failed"
            if p["convert"] == "failed" and not parts:
                p["convert_error"] = "no part could be built from this page"

    schedule("convert", convert_and_check, (job["pdf"], n, p.get("scale"), out), {"words": words}, started, done)


# ---------------------------------------------------------------- routes

def get_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        abort(404)
    return job


@app.get("/")
def index():
    with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
        html = f.read()
    if HAS_VIEWER:                             # turns on the "Open in viewer" buttons
        html = html.replace('<html lang="en">', '<html lang="en" data-viewer="1">', 1)
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}


@app.get("/viewer")
def viewer_root():
    return redirect("/viewer/", 301)


@app.get("/viewer/")
@app.get("/viewer/<path:name>")
def viewer_file(name="index.html"):
    if not HAS_VIEWER:
        abort(404)
    return send_from_directory(SITE, name)


@app.get("/web/<path:name>")
def web_file(name):
    return send_from_directory(os.path.join(HERE, "web"), name)


@app.post("/api/upload")
def upload():
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify(error="please choose a PDF file"), 400
    job_id = uuid.uuid4().hex[:12]
    d = os.path.join(JOBS, job_id)
    os.makedirs(d)
    path = os.path.join(d, "drawing.pdf")
    f.save(path)
    try:
        n = pymupdf.open(path).page_count
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=f"could not open the PDF: {e}"), 400
    job = dict(id=job_id, name=f.filename, pdf=path, dir=d, created=time.time(), scanned=False,
               pages=[dict(page=i + 1, scan="waiting") for i in range(n)])
    with lock:
        jobs[job_id] = job
    start_scan(job)
    return jsonify(id=job_id)


@app.get("/api/job/<job_id>")
def job_state(job_id):
    job = get_job(job_id)
    with lock:
        for p in job["pages"]:
            fut = futures.get((job_id, p["page"]))
            if fut is not None and p.get("convert") == "queued" and fut.running():
                p["convert"] = "converting"
        return jsonify({k: v for k, v in job.items() if k not in ("pdf", "dir")})


@app.post("/api/job/<job_id>/convert")
def convert(job_id):
    job = get_job(job_id)
    body = request.get_json(silent=True) or {}
    with lock:
        pages = body.get("pages")
        if pages == "usable":
            pages = [p["page"] for p in job["pages"] if p.get("scan") == "usable"]
        for n in pages or []:
            n = int(n)
            if 1 <= n <= len(job["pages"]):
                if "scale" in body:
                    job["pages"][n - 1]["scale"] = float(body["scale"]) if body["scale"] else None
                start_convert(job, n)
    return jsonify(ok=True)


@app.get("/api/job/<job_id>/thumb/<int:page>.png")
def thumb(job_id, page):
    job = get_job(job_id)
    path = os.path.join(job["dir"], f"thumb_{page}.png")
    if not os.path.exists(path):
        doc = pymupdf.open(job["pdf"])
        if not 1 <= page <= doc.page_count:
            abort(404)
        pg = doc[page - 1]
        zoom = 520 / max(pg.rect.width, pg.rect.height)
        pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(path)
    return send_file(path, max_age=3600)


@app.get("/api/job/<job_id>/file/<int:page>/<name>")
def part_file(job_id, page, name):
    job = get_job(job_id)
    if not name.endswith((".step", ".stl", ".json", ".png")) or "/" in name or "\\" in name or ".." in name:
        abort(404)
    d = os.path.join(job["dir"], f"p{page:02d}")
    return send_from_directory(d, name, as_attachment=name.endswith(".step"))


@app.get("/api/job/<job_id>/all.zip")
def all_zip(job_id):
    job = get_job(job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(job["dir"]):
            for fn in files:
                if fn.endswith((".step", "_report.json")):
                    z.write(os.path.join(root, fn), fn)
    buf.seek(0)
    stem = os.path.splitext(job["name"])[0]
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{stem}_step.zip")


if __name__ == "__main__":
    import argparse, socket
    ap = argparse.ArgumentParser(description="pdf2step web app")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; 0.0.0.0 opens it to other computers on the network (default: this PC only)")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    with socket.socket() as s:                 # Windows lets a second server share the port: refuse instead
        if s.connect_ex(("127.0.0.1", a.port)) == 0:
            sys.exit(f"Something is already running on port {a.port} (an older copy of this app?). "
                     "Close it first, or pick another --port.")
    import scan
    print(f"pdf2step web: open http://127.0.0.1:{a.port}   ({scan.workers()} worker processes)")
    if HAS_VIEWER:
        print(f"  STP viewer: http://127.0.0.1:{a.port}/viewer/")
    if a.host not in ("127.0.0.1", "localhost"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the LAN address
                lan = s.getsockname()[0]
        except OSError:
            lan = socket.gethostbyname(socket.gethostname())
        print(f"  from other computers: http://{lan}:{a.port}")
        print("  note: there is no login - anyone who can reach this address can upload and download drawings")
    app.run(host=a.host, port=a.port, debug=False, threaded=True)
