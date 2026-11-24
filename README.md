# ConnectIQ STP Viewer

| Folder | What it is |
| --- | --- |
| [`site/`](site/) | The viewer: open STEP, IGES and BREP files in the browser, then measure, cut, estimate weight and compare. Published to GitHub Pages at https://nectrogt.github.io/stepviewer/. |
| [`desktop/`](desktop/) | The same viewer as a Windows program (Electron). Installers are on the [Releases](https://github.com/NectroGT/stepviewer/releases/latest) page. |
| [`converter/`](converter/) | PDF to STEP (formerly the `2dto3d` repo): builds 3D STEP models of plate-type parts from vector PDF drawings. It's a Python web app; run it with `python converter/app.py`, and its **Open in viewer** buttons open each converted part in the viewer at `/viewer/`. |

The viewer runs entirely in the browser. The converter needs Python (CadQuery, OCR), so it runs on your own
computer rather than on the website.
