"""Check the converter against reference models.

    python check_refs.py FOLDER            # FOLDER holds <name>-drawing.pdf (or <name>.pdf) + <name>.step pairs
    python check_refs.py FOLDER --keep out # also keep the converted STEP files in out/

Each drawing is converted (page 1, scale read from the title block) and the result is compared with its
reference model. The reference may be drawn in any orientation, so all 24 quarter-turn orientations are tried
and the best overlap is kept. Reports, per part:
    overlap   volume in both / volume in either (1.0 = identical)
    missing   reference material the converted model lacks (mm3)
    extra     material the converted model has that the reference does not (mm3)
    faces     face count, converted / reference: many more faces means curves were built from flat facets
Exit code 1 if any part fails to convert or its overlap is below --min (default 0.995).
"""
import argparse, glob, itertools, os, sys, tempfile
import numpy as np
import cadquery as cq
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.gp import gp_Trsf


def vol(s):
    g = GProp_GProps(); BRepGProp.VolumeProperties_s(s.wrapped if hasattr(s, "wrapped") else s, g); return g.Mass()


def _rotations():
    for p in itertools.permutations(range(3)):
        for sg in itertools.product((1, -1), repeat=3):
            m = np.zeros((3, 3))
            for i in range(3):
                m[i, p[i]] = sg[i]
            if np.linalg.det(m) > 0:
                yield m


def compare(auto_path, ref_path):
    a0 = cq.importers.importStep(auto_path).val()
    r = cq.importers.importStep(ref_path).val()
    br, vr = r.BoundingBox(), vol(r)
    best = None
    for m in _rotations():
        t = gp_Trsf()
        t.SetValues(*[float(v) for v in m[0]], 0.0, *[float(v) for v in m[1]], 0.0, *[float(v) for v in m[2]], 0.0)
        a = cq.Shape.cast(BRepBuilderAPI_Transform(a0.wrapped, t, True).Shape())
        ba = a.BoundingBox()
        if max(abs(ba.xlen - br.xlen), abs(ba.ylen - br.ylen), abs(ba.zlen - br.zlen)) > 1.0:
            continue
        a = a.translate(cq.Vector(br.xmin - ba.xmin, br.ymin - ba.ymin, br.zmin - ba.zmin))
        c = BRepAlgoAPI_Common(a.wrapped, r.wrapped); c.Build()
        vc, va = vol(c.Shape()), vol(a)
        iou = vc / (va + vr - vc)
        if best is None or iou > best["overlap"]:
            best = dict(overlap=iou, missing=vr - vc, extra=va - vc)
    if best is None:
        ba = a0.BoundingBox()
        return dict(overlap=0.0, missing=vr, extra=0.0,
                    note=f"size {ba.xlen:.1f}x{ba.ylen:.1f}x{ba.zlen:.1f} vs {br.xlen:.1f}x{br.ylen:.1f}x{br.zlen:.1f}")
    best["faces"] = (len(a0.Faces()), len(r.Faces()))
    return best


def find_pairs(folder):
    """[(name, pdf, ref)]: each <name>-drawing.pdf with a <name>.step beside it, or <name>.pdf with a <name>.step
    of the same name (.stp works too)"""
    pairs = []
    for pdf in sorted(glob.glob(os.path.join(folder, "*.pdf"))):
        stem = os.path.splitext(os.path.basename(pdf))[0]
        names = [stem[: -len("-drawing")], stem] if stem.endswith("-drawing") else [stem]
        for name in names:
            ref = next((os.path.join(folder, name + e) for e in (".step", ".stp", ".STEP", ".STP")
                        if os.path.exists(os.path.join(folder, name + e))), None)
            if ref:
                pairs.append((name, pdf, ref))
                break
    return pairs


def check_one(pdf, ref, out):
    """convert pdf (page 1) into out/ and compare it with ref. Returns compare()'s dict plus "model" (the
    converted STEP), or {"error": ...}. Runs in a worker process in the web app."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pdf2step import run
    try:
        res = run(pdf, 1, None, out)
        built = [r for r in res if "size_mm" in r]
        if not built:
            raise RuntimeError(next((r.get("error") or r.get("note") for r in res), None) or "nothing built")
        model = os.path.join(out, built[0]["name"] + ".step")
        c = compare(model, ref)
        c["model"] = model
        return c
    except BaseException as e:            # SystemExit too: run() exits when it cannot read the scale
        return dict(error=f"{type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--keep", help="folder to keep the converted models in")
    ap.add_argument("--min", type=float, default=0.995, help="lowest overlap that passes")
    args = ap.parse_args()
    out_root = args.keep or tempfile.mkdtemp(prefix="check_refs_")
    pairs = find_pairs(args.folder)
    if not pairs:
        raise SystemExit(f"no <name>-drawing.pdf + <name>.step pairs in {args.folder}")

    failed = 0
    print(f"{'part':28s} {'overlap':>8s} {'missing':>9s} {'extra':>9s} {'faces':>11s}")
    for name, pdf, ref in pairs:
        c = check_one(pdf, ref, os.path.join(out_root, name))
        if "error" in c:
            failed += 1
            print(f"{name:28s} FAILED: {c['error']}")
            continue
        ok = c["overlap"] >= args.min
        failed += not ok
        faces = f"{c['faces'][0]}/{c['faces'][1]}" if "faces" in c else "-"
        print(f"{name:28s} {c['overlap']:8.4f} {c['missing']:9.1f} {c['extra']:9.1f} {faces:>11s}"
              + ("" if ok else "  << below --min") + (f"  ({c['note']})" if "note" in c else ""))
    if args.keep is None:
        print(f"(converted models in {out_root})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
