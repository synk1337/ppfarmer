# PyInstaller build recipe.
#
#   pip install pyinstaller
#   pyinstaller ppfarmer.spec
#
# Produces dist/ppfarmer.exe: a single file users can run without Python.
# The page is bundled as data (paths.resource_dir finds it under sys._MEIPASS),
# and rosu_pp_py ships a compiled .pyd that PyInstaller must be told about.

from PyInstaller.utils.hooks import collect_dynamic_libs

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=collect_dynamic_libs("rosu_pp_py"),
    datas=[("ppfarmer/static", "static")],
    hiddenimports=["rosu_pp_py"],
    hookspath=[],
    runtime_hooks=[],
    # Trimmed: none of these are imported, and they add tens of megabytes.
    excludes=["tkinter", "unittest", "pydoc", "doctest", "test",
              "numpy", "PIL", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ppfarmer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Console kept on purpose: it shows the local URL and the data folder, and
    # closing it is how you stop the server.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
