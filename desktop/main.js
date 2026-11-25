// ConnectIQ STP Viewer, desktop version.
// Shows the same viewer as the website (the files in ../site) in its own window, fully offline.
// The site files are served from inside the app at app://viewer/, so the page works exactly as it
// does on the web (its Content Security Policy, background worker and settings included).
const { app, BrowserWindow, Menu, dialog, ipcMain, protocol, session, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs/promises");
const { existsSync } = require("node:fs");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");

const SITE = app.isPackaged ? path.join(process.resourcesPath, "site") : path.join(__dirname, "..", "site");
const ORIGIN = "app://viewer";
const MODEL = /\.(stp|step|igs|iges|brep|brp)$/i;
const TYPES = {
  ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".wasm": "application/wasm",
  ".png": "image/png", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".json": "application/json",
  ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
};

// app:// behaves like a normal secure website origin (fetch, workers, local storage)
protocol.registerSchemesAsPrivileged([
  { scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } },
]);

// one window only: opening another file while the app runs adds it as a tab there
if (!app.requestSingleInstanceLock()) app.quit();

let win = null, viewerReady = false;
let pending = modelPaths(process.argv); // files Windows asked us to open (double-click / "Open with")

function modelPaths(argv) {
  return argv.slice(1).filter((a) => !a.startsWith("-") && MODEL.test(a));
}

// read files from disk and hand them to the viewer, which opens each one as a tab
async function openFiles(paths) {
  if (!paths.length) return;
  if (!win || !viewerReady) { pending.push(...paths); return; } // sent once the page says it's ready
  const files = [];
  for (const p of paths) {
    try { files.push({ name: path.basename(p), data: await fs.readFile(p) }); }
    catch (e) { dialog.showErrorBox("Couldn't open file", `${p}\n\n${e.message}`); }
  }
  showFiles(files);
}

// files already in memory ({ name, data }) -> tabs in the viewer window
function showFiles(files) {
  if (!win) return;
  if (files.length) win.webContents.send("open-files", files);
  if (win.isMinimized()) win.restore();
  win.focus();
}

// ---------- PDF to STEP (../converter, packaged as pdf2step-server.exe) ----------
// Only in the "+ PDF to STEP" build. The converter is a small web server on 127.0.0.1 that only this app
// can use: it needs a random key, which this app hands to the converter window once as a cookie.
const CONVERTER = (() => {
  const exe = app.isPackaged
    ? path.join(process.resourcesPath, "converter", "pdf2step-server.exe")
    : path.join(__dirname, "..", "converter", "dist", "pdf2step-server", "pdf2step-server.exe");
  if (existsSync(exe)) return { cmd: exe, args: [] };
  const py = path.join(__dirname, "..", "converter", "desktop_server.py"); // running from source with Python
  return !app.isPackaged && existsSync(py) ? { cmd: "python", args: [py] } : null;
})();
const conv = { proc: null, ready: null, origin: "", key: crypto.randomBytes(24).toString("base64url"), win: null };

function startConverter() {
  if (conv.ready) return conv.ready;
  conv.ready = new Promise((resolve, reject) => {
    // stdin "ignore": a converter thread blocked reading a pipe would stop its worker processes starting
    const p = spawn(CONVERTER.cmd, CONVERTER.args, {
      windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env, PDF2STEP_TOKEN: conv.key, PDF2STEP_JOBS: path.join(app.getPath("userData"), "pdf2step-jobs"),
        PDF2STEP_SITE: SITE, PDF2STEP_PARENT_PID: String(process.pid), // it quits by itself if this app dies
        PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8",
      },
    });
    conv.proc = p;
    let out = "", log = "";
    p.stdout.on("data", (d) => {
      out += d;
      const m = /READY (\d+)/.exec(out);
      if (m && !conv.origin) { conv.origin = `http://127.0.0.1:${m[1]}`; resolve(conv.origin); }
    });
    p.stderr.on("data", (d) => { log = (log + d).slice(-4000); });
    const fail = (why) => {
      conv.proc = null; conv.ready = null; conv.origin = "";
      reject(new Error(`${why}\n\n${log.trim().split("\n").slice(-8).join("\n")}`));
    };
    p.on("error", (e) => fail(e.message));
    p.on("exit", (code) => fail(`The converter stopped (exit code ${code}).`));
  });
  return conv.ready;
}

function stopConverter() {
  const p = conv.proc;
  if (!p || p.exitCode !== null) return;
  // the converter and its worker processes (/T = the whole tree)
  try { spawn("taskkill", ["/pid", String(p.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }); } catch {}
}

async function openConverter() {
  if (conv.win) { if (conv.win.isMinimized()) conv.win.restore(); conv.win.focus(); return; }
  const cw = (conv.win = new BrowserWindow({
    width: 1200, height: 860, minWidth: 420, minHeight: 520,
    title: "PDF to STEP", backgroundColor: "#16181d", icon: path.join(__dirname, "build", "icon.png"),
    webPreferences: { partition: "converter", contextIsolation: true, sandbox: true, nodeIntegration: false },
  }));
  cw.on("closed", () => { conv.win = null; });
  cw.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
    `<title>PDF to STEP</title><body style="margin:0;height:100vh;display:grid;place-items:center;background:#16181d;color:#cfd5de;font:15px system-ui">
     <div>Starting PDF to STEP…</div></body>`));
  // stay on the converter; its "Open in viewer" buttons send the STEP file to a tab in the viewer window
  cw.webContents.setWindowOpenHandler(({ url }) => {
    const u = new URL(url);
    if (conv.origin && u.origin === conv.origin && u.pathname.startsWith("/viewer/")) viewerFromConverter(u);
    else if (/^https?:$/.test(u.protocol) && u.origin !== conv.origin) shell.openExternal(url);
    return { action: "deny" };
  });
  cw.webContents.on("will-navigate", (e, url) => { if (!conv.origin || new URL(url).origin !== conv.origin) e.preventDefault(); });
  try {
    const origin = await startConverter();
    if (!cw.isDestroyed()) cw.loadURL(`${origin}/?key=${conv.key}`);
  } catch (e) {
    if (!cw.isDestroyed()) cw.close();
    dialog.showErrorBox("PDF to STEP couldn't start", e.message);
  }
}

// "Open in viewer" in the converter: fetch the STEP file(s) with the converter window's cookie
async function viewerFromConverter(u) {
  const ses = session.fromPartition("converter"), files = [];
  for (const ref of u.searchParams.getAll("open")) {
    const f = new URL(ref, conv.origin);
    if (f.origin !== conv.origin) continue;
    try {
      const r = await ses.fetch(f.href);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      files.push({ name: decodeURIComponent(f.pathname.split("/").pop()), data: Buffer.from(await r.arrayBuffer()) });
    } catch (e) { dialog.showErrorBox("Couldn't open the part", e.message); }
  }
  showFiles(files);
}

async function chooseFiles() {
  const r = await dialog.showOpenDialog(win, {
    title: "Open STEP files",
    properties: ["openFile", "multiSelections"],
    filters: [
      { name: "CAD models (STEP, IGES, BREP)", extensions: ["stp", "step", "igs", "iges", "brep", "brp"] },
      { name: "All files", extensions: ["*"] },
    ],
  });
  if (!r.canceled) openFiles(r.filePaths);
}

function menu() {
  return Menu.buildFromTemplate([
    { label: "File", submenu: [
      { label: "Open…", accelerator: "CmdOrCtrl+O", click: chooseFiles },
      ...(CONVERTER ? [{ label: "PDF to STEP…", accelerator: "CmdOrCtrl+Shift+P", click: openConverter }] : []),
      { type: "separator" },
      { role: "quit", label: "Exit" },
    ] },
    { label: "View", submenu: [
      { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
      { type: "separator" }, { role: "togglefullscreen" },
      { type: "separator" }, { role: "reload" }, { role: "toggleDevTools" },
    ] },
    { label: "Help", submenu: [
      { label: "About ConnectIQ STP Viewer", click: () => dialog.showMessageBox(win, {
        type: "info", title: "About", message: `ConnectIQ STP Viewer ${app.getVersion()}`,
        detail: "Opens STEP, IGES and BREP files on this computer and measures parts.\nFiles are never uploaded.",
      }) },
    ] },
  ]);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 360, minHeight: 520,
    title: "ConnectIQ STP Viewer", backgroundColor: "#eef1f4",
    icon: path.join(__dirname, "build", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true, sandbox: true, nodeIntegration: false, // the page gets no access to the computer
    },
  });
  // stay on the viewer: web links open in the normal browser, nothing else can load in the window
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, url) => { if (!url.startsWith(ORIGIN + "/")) e.preventDefault(); });
  win.webContents.on("did-start-loading", () => { viewerReady = false; }); // e.g. View > Reload
  win.on("closed", () => { win = null; viewerReady = false; app.quit(); }); // closing the viewer closes PDF to STEP too
  win.loadURL(`${ORIGIN}/index.html`);
}

// the page has registered for files (preload.js): send any that were waiting
ipcMain.on("viewer-ready", (e) => {
  if (!win || e.sender !== win.webContents) return;
  viewerReady = true;
  const p = pending; pending = []; openFiles(p);
});

app.whenReady().then(() => {
  // serve ../site (or the packaged copy) at app://viewer/
  protocol.handle("app", async (req) => {
    const url = new URL(req.url);
    const rel = decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname);
    const file = path.normalize(path.join(SITE, rel));
    if (url.host !== "viewer" || !file.startsWith(SITE + path.sep)) return new Response("Not found", { status: 404 });
    try {
      const body = await fs.readFile(file);
      return new Response(body, { headers: { "content-type": TYPES[path.extname(file).toLowerCase()] || "application/octet-stream" } });
    } catch {
      return new Response("Not found", { status: 404 });
    }
  });
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, done) => done(false)); // no camera, mic, etc.
  Menu.setApplicationMenu(menu());
  createWindow();
});

app.on("second-instance", (_e, argv) => {
  if (win) openFiles(modelPaths(argv));
});
app.on("open-file", (e, p) => { e.preventDefault(); openFiles([p]); }); // macOS equivalent of double-click
app.on("window-all-closed", () => app.quit());
app.on("will-quit", stopConverter);
