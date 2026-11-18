# Pocket STEP Viewer: desktop app

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

The installer is written to `dist/Pocket STEP Viewer Setup <version>.exe` (about 120 MB). It installs for
the current user, adds Start-menu and desktop shortcuts, and registers the viewer for STEP, IGES and BREP
files. To release a new version, raise `version` in `package.json` first.

The installer isn't code-signed, so Windows SmartScreen shows "Windows protected your PC" the first time:
click **More info**, then **Run anyway**. Signing needs a paid code-signing certificate.

## Publish a download (GitHub Releases)

GitHub can build the installer and put it on the repo's Releases page
(`https://github.com/NectroGT/stepviewer/releases`), so other computers can download it:

- On GitHub: **Actions** -> **Release desktop app** -> **Run workflow**, enter a new version (e.g. `1.0.1`), or
- from a terminal: `git tag v1.0.1` then `git push origin v1.0.1`.

The build takes about 5 minutes. The version you enter is used for the installer, so `package.json`
doesn't need editing. Each version number can be released once.

## How it works

- `main.js`: creates the window, serves `../site` at `app://viewer/`, handles File > Open, double-clicked
  files and a second launch (which adds the file as a tab in the open window).
- `preload.js`: the only bridge to the page; it just delivers opened files.
- `start.js`: used by `npm start`. VS Code's terminal sets `ELECTRON_RUN_AS_NODE=1`, which stops Electron
  opening a window, so this starts it without that setting.
- The viewer page keeps its Content Security Policy and runs sandboxed, with no access to the computer
  beyond the files you open. Web links open in your normal browser.
