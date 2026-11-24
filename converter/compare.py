"""compare an auto model with a reference STEP: align bounding-box corners, report volume overlap and per-z slices"""
import sys
import cadquery as cq
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp


def vol(s):
    g = GProp_GProps(); BRepGProp.VolumeProperties_s(s.wrapped if hasattr(s, "wrapped") else s, g); return g.Mass()


def compare(auto, ref, flip=False):
    a = cq.importers.importStep(auto).val(); r = cq.importers.importStep(ref).val()
    ba, br = a.BoundingBox(), r.BoundingBox()
    a = a.translate(cq.Vector(br.xmin - ba.xmin, br.ymin - ba.ymin, br.zmin - ba.zmin))
    c = BRepAlgoAPI_Common(a.wrapped, r.wrapped); c.Build()
    vc = vol(c.Shape()); va, vr = vol(a), vol(r)
    return dict(iou=round(vc / (va + vr - vc), 4), auto_cm3=round(va / 1e3, 2), ref_cm3=round(vr / 1e3, 2),
                missing_cm3=round((vr - vc) / 1e3, 2), extra_cm3=round((va - vc) / 1e3, 2),
                size_auto=[round(ba.xlen, 2), round(ba.ylen, 2), round(ba.zlen, 2)], size_ref=[round(br.xlen, 2), round(br.ylen, 2), round(br.zlen, 2)])


if __name__ == "__main__":
    print(compare(sys.argv[1], sys.argv[2]))
