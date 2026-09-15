# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH)
DICTIONARY_PATH = PROJECT_ROOT / "data" / "hanzi.db"
if not DICTIONARY_PATH.exists():
    DICTIONARY_PATH = PROJECT_ROOT / "data" / "hanzi-placeholder.db"

a = Analysis(
    [str(PROJECT_ROOT / "desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "assets" / "fonts"), "assets/fonts"),
        (str(PROJECT_ROOT / "assets" / "icons"), "assets/icons"),
        (str(PROJECT_ROOT / "assets" / "strokes"), "assets/strokes"),
        (str(PROJECT_ROOT / "assets" / "cursive"), "assets/cursive"),
        (str(DICTIONARY_PATH), "data"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HanziLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "icons" / "hanzilab.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HanziLab",
)
