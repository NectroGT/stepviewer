# ConnectIQ STP Viewer: desktop app

The same viewer as the website, as a Windows program. It shows the files in `../site` in its own
window, works offline, and opens `.stp` / `.step` (and IGES, BREP) files you double-click in Windows.

## Run it from source

```
cd desktop
npm install      # first time only
npm start        # opens the app; add a file path to open it: npm start -- "C:\path\part.stp"
```

## Build the installer

```
cd desktop
npx electron-builder --win nsis
```

The installer is written to `dist/ConnectIQ.STP.Viewer.Setup.exe` (about 120 MB). It installs for
the current user, adds Start-menu and desktop shortcuts, and registers the viewer for STEP, IGES and BREP
files. Meshes (STL, OBJ, 3MF, glTF) open from File > Open or by dragging them in; the viewer is not registered for
them, so it does not take them over from a slicer or 3D printing app. To release a new version, raise `version` in `package.json` first.

The installer isn't code-signed, so Windows SmartScreen shows "Windows protected your PC" the first time:
click **More info**, then **Run anyway**. Signing needs a paid code-signing certificate.

## Build the installer with PDF to STEP

A second, larger installer adds **File > PDF to STEP…**: the converter from `../converter` (scans vector
PDF drawings and builds STEP models), packaged with Python so nobody needs Python installed. Its
**Open in viewer** buttons open each part as a tab in the viewer window.

```
cd converter
pip install -r requirements.txt pyinstaller
pyinstaller pdf2step-server.spec --noconfirm        # -> converter/dist/pdf2step-server (about 1.1 GB)
cd ../desktop
npx electron-builder --win nsis --config builder-full.js
```

The installer is `dist/ConnectIQ.STP.Viewer.PDF.to.STEP.Setup.exe` (about 400 MB; 1.4 GB installed). It is the
same app as the plain viewer (same app ID), so installing one over the other switches between them.

The converter runs as a hidden background program (`pdf2step-server.exe`) on 127.0.0.1 only, and answers only
the app's own window (a random key per launch). It closes with the app, and its worker processes with it.
Uploads and results are kept in `%APPDATA%\ConnectIQ STP Viewer\pdf2step-jobs`. `npm start` also offers
PDF to STEP when Python and the converter's requirements are installed.

## Publish a download (GitHub Releases)

GitHub can build the installer and put it on the repo's Releases page
(`https://github.com/NectroGT/stepviewer/releases`), so other computers can download it:

- On GitHub: **Actions** -> **Release desktop app** -> **Run workflow**, enter a new version (e.g. `1.0.1`), or
- from a terminal: `git tag v1.0.1` then `git push origin v1.0.1`.

The build takes about 5 minutes. The version you enter is used for the installer, so `package.json`
doesn't need editing. Each version number can be released once.

Links to share (they always point at the newest release):

- page: `https://github.com/NectroGT/stepviewer/releases/latest`
- direct download: `https://github.com/NectroGT/stepviewer/releases/latest/download/ConnectIQ.STP.Viewer.Setup.exe`
  (the old name `Pocket.STEP.Viewer.Setup.exe` still works: each release includes a copy under it)

The direct link works because the installer's file name has no version in it (`artifactName` in
`package.json`); the version shows in the installer and in Help > About.

## How it works

- `main.js`: creates the window, serves `../site` at `app://viewer/`, handles File > Open, double-clicked
  files and a second launch (which adds the file as a tab in the open window).
- `preload.js`: the only bridge to the page; it just delivers opened files.
- `start.js`: used by `npm start`. VS Code's terminal sets `ELECTRON_RUN_AS_NODE=1`, which stops Electron
  opening a window, so this starts it without that setting.
- The viewer page keeps its Content Security Policy and runs sandboxed, with no access to the computer
  beyond the files you open. Web links open in your normal browser.
