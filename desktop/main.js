// Pocket STEP Viewer, desktop version.
// Shows the same viewer as the website (the files in ../site) in its own window, fully offline.
// The site files are served from inside the app at app://viewer/, so the page works exactly as it
// does on the web (its Content Security Policy, background worker and settings included).
const { app, BrowserWindow, Menu, dialog, ipcMain, protocol, session, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs/promises");

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
  if (files.length) win.webContents.send("open-files", files);
  if (win.isMinimized()) win.restore();
  win.focus();
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
      { type: "separator" },
      { role: "quit", label: "Exit" },
    ] },
    { label: "View", submenu: [
      { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
      { type: "separator" }, { role: "togglefullscreen" },
      { type: "separator" }, { role: "reload" }, { role: "toggleDevTools" },
    ] },
    { label: "Help", submenu: [
      { label: "About Pocket STEP Viewer", click: () => dialog.showMessageBox(win, {
        type: "info", title: "About", message: `Pocket STEP Viewer ${app.getVersion()}`,
        detail: "Opens STEP, IGES and BREP files on this computer and measures parts.\nFiles are never uploaded.",
      }) },
    ] },
  ]);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 360, minHeight: 520,
    title: "Pocket STEP Viewer", backgroundColor: "#eef1f4",
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
  win.on("closed", () => { win = null; viewerReady = false; });
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
