# 2dto3d (pdf2step)

Builds 3D STEP models of **plate-type parts** (flat plates, blocks, brackets with holes and pockets) from a
**vector PDF** drawing exported from CAD (NX, SolidWorks, Creo, Inventor, Fusion ...).

```
python pdf2step.py drawing.pdf --page 13 -o out/                # scale read from the title block
python pdf2step.py drawing.pdf --page 13 --scale 3 -o out/      # or give it (3 = drawing scale 1:3)
```

`pip install -r requirements.txt` installs everything, including the OCR used to read the sheet's text
(RapidOCR, which runs locally). If RapidOCR is not installed, Tesseract is used when it is on the PATH; with neither,
the tool works from the geometry only and `--scale` is required.

## Web app
```
python app.py                          # then open http://127.0.0.1:5000
python app.py --host 0.0.0.0           # also reachable from other computers on the network
python app.py --host 0.0.0.0 --port 8080
```
With `--host 0.0.0.0` the app prints the address to use from other computers. There is no login, so only open it on
a network you trust; Windows may ask to allow Python through the firewall the first time.
Upload a PDF and every page is scanned for parts that can be built (a plan view with a lined-up side view).
Pages that cannot be used say why (assembly drawing, scanned image, only 3D views, no side view lined up), and
usable pages that look like an assembly are flagged for a check. Convert one page or all usable pages, turn each
part around in a 3D preview (beside it, the 3D view drawn on the sheet, if there is one, to compare by eye -
it is only shown, never used to build the model), and download the STEP files one by one or as a zip. Pages are scanned and converted
side by side in up to 4 worker processes; the text read by the scan is kept, so converting does not run OCR again.
Uploads and results are kept in `web_jobs/`.

**Check:** each converted part (pages with one part) is checked against the drawing - its views' outlines and the
lines inside them (see "Checking a model against its drawing" below) - and marked **matches drawing** or **differs
from drawing**; click the mark to see the model's views over the drawing's. It runs on this computer.

**Open in viewer:** the app also serves the ConnectIQ STP Viewer from this repo (`../site`) at
`http://127.0.0.1:5000/viewer/`. Each converted part has an **Open in viewer** button that opens its STEP file
there in a new tab, with measuring, the cut plane, weight estimates and compare. The file goes from this
app to the viewer on the same server; nothing is sent anywhere else.

The same scan from the command line:
```
python scan.py drawing.pdf              # one line per page: usable, or why not
python scan.py drawing.pdf --no-text    # geometry only (faster, no assembly check)
```

## Checking against reference models
Put drawings and the models they were drawn from side by side as `<name>-drawing.pdf` (or `<name>.pdf`) +
`<name>.step`, then
```
python check_refs.py path/to/folder                 # one line per part
python check_refs.py path/to/folder --keep out/     # also keep the converted models
```
Each drawing is converted and compared with its model in whichever of the 24 quarter-turn orientations fits best:
**overlap** (1.0 = identical), **missing** / **extra** material in mm3, and **faces** (converted / reference; many
more faces means curves came out as flat facets). It exits with an error when a part fails or its overlap is below
`--min` (0.995), so run it after every change to the converter.

## Checking a model against its drawing (check.py)
```
python check.py drawing.pdf model.step              # matched, outline overlap and line share per view
python check.py drawing.pdf model.step --png out.png
```
The drawing's views are put into one frame in millimetres (x along the part, y from the front face backwards, z up;
top view = (x, y), front = (x, z), end = (y, z)); the model is turned into that frame (whichever quarter turn fits
the drawing's size and agrees best) and projected into each view. Per view: the outer outline of the model's view
against the drawing's (overlap, 1.0 = same), and its visible edges against the drawing's visible lines - the share
of the drawing's line length lying within 0.2 mm of a model edge. Outlines alone miss what is inside them (a channel,
a pocket, a step); the edges show it. A model matches when the mean overlap is 0.998 or more and every view's line
share is 0.93 or more (exact models score 0.94-1.0; a base missing its channel, with matching outlines, scored
0.35-0.56).

## Converting with Claude (vlm.py)
A second route, from the command line only: Claude reads the sheet and writes the part as CadQuery code, using the
converter's exact measurements, and corrects it until its views match the drawing (the check above). It runs
through Claude Code on your Claude subscription (no API key, no per-use charge; it counts toward your plan's usage
limits).
```
python vlm.py drawing.pdf -o out/                     # rules first, Claude only if needed; out/model.step in mm
python vlm.py drawing.pdf -o out/ --claude-only       # straight to Claude
python vlm.py drawing.pdf -o out/ --rounds 4 --effort xhigh
python vlm.py drawing.pdf -o out/ --backend api       # the Claude API instead (pip install anthropic, billed per token)
```
1. The free rules converter (`pdf2step.py`) goes first. Its model is turned into the drawing's frame and checked; if
   it matches, that is the result and Claude is not called.
2. Otherwise Claude gets the sheet image plus each view's outline, edges, circles and the text read from it (model
   frame, mm) and writes a `build()` function. Each round is one `claude -p` call resuming the same session; Claude
   gets only the Read tool (to look at the images), so it cannot edit files or run commands.
3. The code runs in a separate Python process (3 minutes at most); its model is checked, and the scores, where the
   drawing's lines lack a model edge, and a picture of its views over the drawing go back to Claude.
4. It stops when the outlines and edges match and Claude, in one review round, answers MATCHES; when two attempts
   in a row do not improve (after the first, Claude is sent the best attempt's code to start from); when the code
   does not change; or at `--rounds` (default 3).
A page that is only a picture (a scan or photo saved as PDF) has nothing to measure: Claude gets the sheet, its four
quarters enlarged (for the small text) and the text OCR found, takes every size from the dimension text (inch
drawings converted to mm) and compares its model's views with the sheet by eye.
`out/` keeps every attempt (`attempt_N.py/.step/.png`), `model.step` and `report.json` (route, scores, why it
stopped, time). Needs Claude Code signed in (`claude` on the PATH, else the copy in the VS Code extension; or set
`CLAUDE_CLI`). The drawing image and the text read from it are sent to Claude, so do not use it for drawings that
must not leave this computer; the code Claude writes runs on this computer, in its own process with a time limit.

## How it works
1. **Reads the lines, not a picture.** The PDF keeps the drawing as true lines, arcs and circles. Thick
   strokes are visible edges; thin strokes are hidden / centre / dimension lines; filled shapes (text, arrow
   heads) are ignored. Sizes come straight from the geometry and the sheet scale, so they are exact (rounded to 0.0005 in, or to 0.01 mm when the title block says the dimensions are in millimetres).
2. **Finds the views**: connected groups of visible edges; the sheet border, title block, tables and isometric
   views are dropped. Views drawn turned on the sheet (e.g. a part lying at 33 deg) are straightened first.
3. **Pairs views** (third-angle): the plan view, the thin side / section views lined up with it, and a bottom
   view (a mirrored twin directly below) if there is one.
4. **Recognises features**
   - outline: outer boundary of the plan view (arcs rebuilt as true arcs; edge-break double lines skipped)
   - thickness: from the side view
   - holes: concentric circles from the top view (and the bottom view, mirrored); each step's depth and side
     from the lines at its edges in the side / section views (drill, counterbore, blind hole)
   - cut-outs and pockets: inner boundaries; depth from the pocket floor line in the side / section view
   - counterbores on the far face: dashed circles in the plan, or a drill that stops short in the side view
   - stepped thickness: a plan area whose width matches a higher top edge in the side view is raised to it
   - holes in the edges: circles in a side view (depth from the plan), or two hidden lines running in from an
     edge one hole-diameter apart (placed at mid-thickness, flagged in the report)
5. **Reads the text** (OCR on the lettering, which CAD exports usually draw as outlines): the title block scale and
   the hole notes. A note is matched to its hole by following its leader line; "6X #10-24 UNC ↧.50", "26X Ø.21 THRU",
   "⌴Ø.38 ↧.205" set the drill size (tap drill for threads), depth, through or counterbore of every hole of that
   kind. Threads found are listed in the report. Notes that point into a detail view ("DETAIL AG  SCALE 1:1") or at an
   edge hole in a side view are traced back to the hole they mean.
6. **Section views** (hatched views, e.g. "SECTION G-G"): the hatched area is the true cross-section along the
   cutting line on the plan. Wherever the model has material the section shows as empty, it is cut away - swept
   around the centre of the plan's arcs when the empty area sits at their radii (ring seats, lips, grooves round a
   bore), otherwise cut straight across, as wide as the nearest pair of plan lines either side of the cut.
7. **Turned (round) parts**: an end view of concentric circles as wide as the plan makes the outline spin into a
   round part; inner circles that are not outline steps are axial bores. On a stepped shaft the end view (or a
   section through it) is only as wide as the step it faces; it is paired by its centre line. A slot seen in the
   plan is a keyway, cut down to the flat drawn across it in the end view. When the plan view is nothing but
   concentric circles (bushing, washer, flange), it is the end view and the side view beside it is the profile spun. A part with its own "SCALE: 1 / 1" label
   uses that scale.
8. **Builds** the solid with CadQuery and exports STEP.

## Limits
- Threads are built at the drawn hole size; slanted features are not built.
- Stepped parts: only raised areas whose edges show in a side view; pockets inside raised areas are not handled.
- Sections: vertical cutting lines only; the profile found at the cut is used all the way round (pads or features
  that exist only at some angles are not separated).
- Parts that need sections to be understood (rings, lips, undercuts) are listed as "unused views".
- Detail views are located on their parent view (so notes pointing into them work) but their geometry is not yet
  used to shape the part.

`compare.py auto.step reference.step` reports the volume overlap between an auto model and a reference.
