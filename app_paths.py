from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "HanziLab"
SOURCE_ROOT = Path(__file__).resolve().parent


def resource_root() -> Path:
    """Return the read-only application resource directory.

    PyInstaller exposes bundle resources through ``sys._MEIPASS`` on macOS.
    Keeping this separate from writable data is essential because a signed
    ``.app`` bundle must not be modified after it has been built.
    """
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root).resolve() if bundle_root else SOURCE_ROOT


def default_user_data_dir(
    *,
    platform: str | None = None,
    home: Path | None = None,
    frozen: bool | None = None,
    executable: Path | None = None,
    source_root: Path | None = None,
) -> Path:
    platform = platform or sys.platform
    home = home or Path.home()
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = executable or Path(sys.executable)
    source_root = source_root or SOURCE_ROOT
    override = os.environ.get("HANZILAB_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if platform == "darwin":
        if frozen:
            return home / "Library" / "Application Support" / APP_NAME
        return source_root / "data"
    if platform == "win32":
        # Preserve the portable Windows layout used by existing releases.
        if frozen:
            return executable.resolve().parent / "data"
        return source_root / "data"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    return (
        Path(xdg_data_home).expanduser() / APP_NAME
        if xdg_data_home
        else home / ".local" / "share" / APP_NAME
    )


RESOURCE_ROOT = resource_root()
USER_DATA_DIR = default_user_data_dir()


def dictionary_candidates() -> tuple[Path, ...]:
    explicit = os.environ.get("HANZILAB_DICTIONARY")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        (
            USER_DATA_DIR / "hanzi.db",
            RESOURCE_ROOT / "data" / "hanzi.db",
            SOURCE_ROOT / "data" / "hanzi.db",
        )
    )
    # Keep ordering while removing duplicates caused by development layouts.
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def placeholder_dictionary_path() -> Path:
    bundled = RESOURCE_ROOT / "data" / "hanzi-placeholder.db"
    return bundled if bundled.exists() else SOURCE_ROOT / "data" / "hanzi-placeholder.db"


def select_dictionary_path() -> Path:
    return next(
        (candidate for candidate in dictionary_candidates() if candidate.is_file()),
        placeholder_dictionary_path(),
    )
