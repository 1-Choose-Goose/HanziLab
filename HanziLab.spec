# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


PROJECT_ROOT = Path(SPECPATH)

a = Analysis(
    [str(PROJECT_ROOT / "desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "assets" / "fonts"), "assets/fonts"),
        (str(PROJECT_ROOT / "assets" / "icons"), "assets/icons"),
        (str(PROJECT_ROOT / "assets" / "strokes"), "assets/strokes"),
        (str(PROJECT_ROOT / "assets" / "cursive"), "assets/cursive"),
        (str(PROJECT_ROOT / "data" / "hanzi-placeholder.db"), "data"),
        (str(PROJECT_ROOT / "dictionary-server.json"), "."),
        (str(PROJECT_ROOT / "dictionary-server-ca.pem"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Qt for Windows uses the ICU compatibility DLLs supplied by Windows itself.
# A third-party icuuc.dll can leak in from PATH during the build and shadow the
# system DLL, causing PySide6.QtCore to fail with "procedure not found".
a.binaries = [
    entry
    for entry in a.binaries
    if not (
        sys.platform == "win32"
        and (
        Path(entry[0]).name.lower() == "icuuc.dll"
        or (
            Path(entry[0]).name.lower().startswith("icudt")
            and Path(entry[0]).name.lower().endswith(".dll")
        ))
    )
]

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
    icon=str(
        PROJECT_ROOT
        / "assets"
        / "icons"
        / ("hanzilab.icns" if sys.platform == "darwin" else "hanzilab.ico")
    ),
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

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="HanziLab.app",
        icon=str(PROJECT_ROOT / "assets" / "icons" / "hanzilab.icns"),
        bundle_identifier="io.github.choose-goose.hanzilab",
        info_plist={
            "CFBundleDisplayName": "HanziLab",
            "CFBundleName": "HanziLab",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        },
    )
