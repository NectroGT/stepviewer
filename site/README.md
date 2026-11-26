# ConnectIQ STP Viewer

A phone-friendly web page for opening STEP (also IGES/BREP) files, browsing the part tree, and
measuring part sizes, hole diameters and distances.

It also opens meshes: STL (binary and text), OBJ, 3MF and glTF / GLB (`mesh.js`). A mesh is only triangles, so
its faces are worked out from them: triangles meeting at less than 30° form one face, which makes flat faces and
hole walls pickable and measurable, but sizes are as exact as the mesh is fine. STL and OBJ have no units and are
read as millimetres; 3MF states its unit; glTF is in metres (a model over 10 units across is taken as millimetres,
as several CAD exporters write it) and is turned from Y up to Z up. Compressed glTF (Draco, meshopt) and glTF
with a separate .bin file are not read: save as .glb.

## Privacy

- Files you open are read and processed inside your browser. Nothing is uploaded.
- Every script, font and file the page uses is served from this site. No CDNs, analytics or trackers.
- The page sets a Content Security Policy (`connect-src 'self'`, `img-src 'self' data: blob:`), so the
  browser refuses any attempt to send data to another server, even from tampered code.
- Files are read in a background worker so the page stays responsive. The worker is started in a way
  that makes it follow the same policy, so it can't connect anywhere else either.
- The only things stored are your display settings (mm/in, light/dark, dimension lines on/off,
  details panel open/closed, cut direction, and the default material/density used for weight estimates), in
  your browser's local storage. No file names or file contents are stored. Materials chosen for single parts
  are kept only until the page is reloaded.

## Third-party code

- `vendor/three.min.js`, `vendor/OrbitControls.js`: [three.js](https://threejs.org) r147, MIT (`vendor/three.LICENSE`).
- `occt-import-js.js` / `.wasm`: [occt-import-js](https://github.com/kovacsv/occt-import-js) (Open CASCADE),
  LGPL-2.1 (`license.occt-import-js.txt`, `license.occt.txt`).
- `fonts/`: IBM Plex, SIL Open Font License 1.1 (`fonts/OFL.txt`).
