# PyInstaller recipe: packs the converter (desktop_server.py + Python + CadQuery + OCR) into
# dist/pdf2step-server/pdf2step-server.exe, which the desktop app ("+ PDF to STEP" build) starts in the background.
#   cd converter
#   pip install -r requirements.txt pyinstaller
#   pyinstaller pdf2step-server.spec --noconfirm
# It is a console program on purpose (it reports its port on stdout); the desktop app starts it hidden.
import glob, os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

here = os.path.abspath(SPECPATH)
local = [os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(here, "*.py"))
         if not f.endswith("desktop_server.py")]

a = Analysis(
    ["desktop_server.py"],
    pathex=[here],
    datas=[("web", "web")] + collect_data_files("rapidocr") + collect_data_files("cadquery"),
    binaries=collect_dynamic_libs("OCP") + collect_dynamic_libs("onnxruntime"),
    hiddenimports=local + collect_submodules("rapidocr") + ["OCP", "werkzeug.serving"],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "torch", "paddle",
              "pytesseract", "trame", "trame_vtk", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="pdf2step-server", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="pdf2step-server", upx=False)
