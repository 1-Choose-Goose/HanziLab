# -*- mode: python ; coding: utf-8 -*-

import re
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(SPECPATH)
VERSION_SOURCE = (PROJECT_ROOT / "version.py").read_text(encoding="utf-8")
APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', VERSION_SOURCE).group(1)
DATA_FILES = [
    (str(PROJECT_ROOT / "assets" / "fonts"), "assets/fonts"),
    (str(PROJECT_ROOT / "assets" / "icons"), "assets/icons"),
    (str(PROJECT_ROOT / "data" / "hanzi-placeholder.db"), "data"),
]
PACKED_ASSET_ROOT = PROJECT_ROOT / "build" / "packed-assets"
PACKED_ASSET_ROOT.mkdir(parents=True, exist_ok=True)
for asset_name in ("strokes", "cursive"):
    source = PROJECT_ROOT / "assets" / asset_name
    archive = PACKED_ASSET_ROOT / f"{asset_name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(source).as_posix())
    DATA_FILES.append((str(archive), "assets"))
for optional_file in ("dictionary-server.json", "dictionary-server-ca.pem"):
    path = PROJECT_ROOT / optional_file
    if path.is_file():
        DATA_FILES.append((str(path), "."))

a = Analysis(
    [str(PROJECT_ROOT / "desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=DATA_FILES,
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
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
        },
    )
