"""Read application assets from sources or compact release archives."""

from __future__ import annotations

import atexit
import zipfile
from pathlib import Path, PurePosixPath

_OPEN_ARCHIVES: dict[Path, zipfile.ZipFile] = {}


def _open_archive(path: Path) -> zipfile.ZipFile:
    archive = _OPEN_ARCHIVES.get(path)
    if archive is None:
        archive = zipfile.ZipFile(path)
        _OPEN_ARCHIVES[path] = archive
    return archive


def close_asset_archives() -> None:
    while _OPEN_ARCHIVES:
        _path, archive = _OPEN_ARCHIVES.popitem()
        archive.close()


atexit.register(close_asset_archives)


def read_asset_bytes(directory: Path, relative_path: str) -> bytes:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Unsafe asset path")
    source = directory.joinpath(*relative.parts)
    if source.is_file():
        return source.read_bytes()
    archive = directory.with_suffix(".zip")
    return _open_archive(archive).read(relative.as_posix())


def read_asset_text(directory: Path, relative_path: str) -> str:
    return read_asset_bytes(directory, relative_path).decode("utf-8")


def iter_asset_paths(directory: Path, suffix: str | None = None) -> tuple[str, ...]:
    """List asset names from an unpacked directory or its release archive."""
    names: set[str] = set()
    if directory.is_dir():
        names.update(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
        )
    archive = directory.with_suffix(".zip")
    if archive.is_file():
        with zipfile.ZipFile(archive) as package:
            names.update(name for name in package.namelist() if not name.endswith("/"))
    return tuple(
        sorted(name for name in names if suffix is None or name.endswith(suffix))
    )
