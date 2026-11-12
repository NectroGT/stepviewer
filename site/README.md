# Pocket STEP Viewer

A phone-friendly web page for opening STEP (also IGES/BREP) files, browsing the part tree, and
measuring part sizes, hole diameters and distances.

## Privacy

- Files you open are read and processed inside your browser. Nothing is uploaded.
- Every script, font and file the page uses is served from this site. No CDNs, analytics or trackers.
- The page sets a Content Security Policy (`connect-src 'self'`, `img-src 'self' data: blob:`), so the
  browser refuses any attempt to send data to another server, even from tampered code.
- The only thing stored is your mm/in choice, in your browser's local storage.

## Third-party code

- `vendor/three.min.js`, `vendor/OrbitControls.js`: [three.js](https://threejs.org) r147, MIT (`vendor/three.LICENSE`).
- `occt-import-js.js` / `.wasm`: [occt-import-js](https://github.com/kovacsv/occt-import-js) (Open CASCADE),
  LGPL-2.1 (`license.occt-import-js.txt`, `license.occt.txt`).
- `fonts/`: IBM Plex, SIL Open Font License 1.1 (`fonts/OFL.txt`).
- `sample.stp`: the public AS1 test assembly.
