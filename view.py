"""Open a STEP file in OCP CAD Viewer with its assembly tree, part names and colors.

    .venv/bin/python view.py part.stp     # or no argument to pick a file
"""
import sys
from collections import Counter

import cadquery as cq
from ocp_vscode import show

from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_ColorRGBA
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.XCAFDoc import (XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_ColorTool,
                         XCAFDoc_DocumentTool)


def pick_file():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()  # hide the empty main window
    path = filedialog.askopenfilename(
        title="Select a STEP file",
        filetypes=[("STEP files", "*.stp *.step *.STP *.STEP"), ("All files", "*.*")],
    )
    root.destroy()
    return path


class StepTree:
    """Reads a STEP file's product structure (assemblies, parts, names, colors)."""

    def __init__(self, path):
        self.doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        if reader.ReadFile(path) != IFSelect_RetDone:
            raise RuntimeError(f"Could not read {path}")
        reader.Transfer(self.doc)
        self.shapes = XCAFDoc_DocumentTool.ShapeTool_s(self.doc.Main())
        self.colors = XCAFDoc_DocumentTool.ColorTool_s(self.doc.Main())
        self.used_names = Counter()

    def name(self, label, fallback="part"):
        attr = TDataStd_Name()
        if label.IsAttribute(TDataStd_Name.GetID_s()):
            label.FindAttribute(TDataStd_Name.GetID_s(), attr)
            text = str(attr.Get().ToExtString()).strip()
            if text:
                return text
        return fallback

    def unique(self, name):
        # cadquery needs every name in the tree to be unique
        self.used_names[name] += 1
        n = self.used_names[name]
        return name if n == 1 else f"{name} ({n})"

    def color(self, *labels):
        c = Quantity_ColorRGBA()
        for label in labels:
            if label is None:
                continue
            for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
                if XCAFDoc_ColorTool.GetColor_s(label, kind, c):
                    rgb = c.GetRGB()
                    return cq.Color(rgb.Red(), rgb.Green(), rgb.Blue(), c.Alpha())
            shape = self.shapes.GetShape_s(label)
            if self.colors.GetColor(shape, XCAFDoc_ColorSurf, c):
                rgb = c.GetRGB()
                return cq.Color(rgb.Red(), rgb.Green(), rgb.Blue(), c.Alpha())
        return None

    def build(self):
        free = TDF_LabelSequence()
        self.shapes.GetFreeShapes(free)
        tops = [free.Value(i) for i in range(1, free.Length() + 1)]
        if len(tops) == 1:
            return self.node(tops[0])
        root = cq.Assembly(name=self.unique("model"))
        for lbl in tops:
            root.add(self.node(lbl))
        return root

    def node(self, label, instance=None):
        """Return a cq.Assembly for a label (assembly, or a part shown as a leaf)."""
        ref = TDF_Label()
        if self.shapes.IsReference_s(label) and self.shapes.GetReferredShape_s(label, ref):
            return self.node(ref, instance=label)

        title = self.name(instance, None) if instance is not None else None
        if not title or title.startswith("=>"):  # auto-generated instance name
            title = self.name(label)
        loc = cq.Location(self.shapes.GetLocation_s(instance)) if instance is not None else cq.Location()

        if self.shapes.IsAssembly_s(label):
            asm = cq.Assembly(name=self.unique(title), loc=loc)
            comps = TDF_LabelSequence()
            self.shapes.GetComponents_s(label, comps)
            for i in range(1, comps.Length() + 1):
                asm.add(self.node(comps.Value(i)))
            return asm

        shape = self.shapes.GetShape_s(label)
        color = self.color(instance, label)
        solids = []
        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            solids.append(exp.Current())
            exp.Next()

        if len(solids) <= 1:
            # a single part
            return cq.Assembly(cq.Shape.cast(shape), name=self.unique(title), loc=loc, color=color)

        # one shape holding many bodies: list each body so it can be toggled
        asm = cq.Assembly(name=self.unique(title), loc=loc)
        for k, solid in enumerate(solids, 1):
            sub = TDF_Label()
            has_sub = self.shapes.FindSubShape(label, solid, sub)
            body_name = self.name(sub, f"body {k}") if has_sub else f"body {k}"
            body_color = self.color(sub if has_sub else None) or color
            asm.add(cq.Shape.cast(solid), name=self.unique(body_name), color=body_color)
        return asm


def load(path):
    try:
        return StepTree(path).build()
    except Exception as e:  # fall back to a flat import if the tree can't be read
        print(f"Couldn't read the assembly tree ({e}); showing it as one shape.")
        return cq.importers.importStep(path)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if not path.lower().endswith((".stp", ".step")):
            sys.exit(f"Not a STEP file: {path}\nSwitch to a .stp/.step tab first.")
        show(load(path))
    else:
        # keep offering the picker so several files can be viewed; Cancel to stop
        while True:
            path = pick_file()
            if not path:
                print("Done.")
                break
            print(f"Opening {path} ...")
            show(load(path))
