"""GitHub Releases client and launcher for the external Windows updater."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import certifi

from version import APP_VERSION

LATEST_RELEASE_API = (
    "https://api.github.com/repos/1-Choose-Goose/HanziLab/releases/latest"
)
WINDOWS_ASSET_NAME = "HanziLab-Windows-x64.zip"
UPDATER_NAME = "HanziLabUpdater.exe"
UPDATER_RELATIVE_PATH = Path("_internal") / UPDATER_NAME
UPDATE_LOCK_NAME = "update.lock"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SHA256_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class UpdateError(RuntimeError):
    """An actionable update check, download, or launch failure."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    download_url: str
    sha256: str
    size: int
    notes: str
    page_url: str


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unsupported version: {value}")
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate: str, current: str = APP_VERSION) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def updates_supported() -> bool:
    return (
        sys.platform == "win32"
        and bool(getattr(sys, "frozen", False))
        and os.environ.get("HANZILAB_DISABLE_UPDATES") != "1"
    )


def update_lock_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return root / "HanziLab" / UPDATE_LOCK_NAME


def _process_is_running(pid: int) -> bool:
    if pid <= 0 or os.name != "nt":
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _write_update_lock(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"pid": pid, "created_at": time.time()}), encoding="utf-8"
    )
    temporary.replace(path)


def is_update_in_progress() -> bool:
    """Return true only while the process recorded by the update lock is alive."""
    path = update_lock_path()
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        created_at = float(payload["created_at"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        try:
            created_at = path.stat().st_mtime
        except OSError:
            return False
        pid = 0
    age = time.time() - created_at
    # The one-file updater needs a short moment to unpack and replace the
    # launcher's PID with its own. Keep the hand-off locked during that gap.
    if 0 <= age <= 30 or (age <= 30 * 60 and _process_is_running(pid)):
        return True
    path.unlink(missing_ok=True)
    return False


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def _open(request: Request, *, timeout: int):
    return urlopen(request, timeout=timeout, context=_ssl_context())


def _request(url: str, *, accept: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": f"HanziLab/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def check_for_update(
    current_version: str = APP_VERSION,
    *,
    api_url: str = LATEST_RELEASE_API,
) -> UpdateInfo | None:
    """Return the latest compatible Windows release when it is newer."""
    try:
        with _open(
            _request(api_url, accept="application/vnd.github+json"), timeout=15
        ) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return None
        raise UpdateError(f"GitHub вернул HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise UpdateError("Не удалось проверить обновления") from error

    tag = str(payload.get("tag_name") or "")
    try:
        if not is_newer_version(tag, current_version):
            return None
    except ValueError as error:
        raise UpdateError("В последнем релизе указана неверная версия") from error

    asset = next(
        (
            item
            for item in payload.get("assets", ())
            if item.get("name") == WINDOWS_ASSET_NAME
        ),
        None,
    )
    if asset is None:
        raise UpdateError("В релизе нет Windows-сборки HanziLab")
    digest = _SHA256_RE.fullmatch(str(asset.get("digest") or ""))
    if digest is None:
        raise UpdateError("У Windows-сборки нет контрольной суммы SHA-256")
    download_url = str(asset.get("browser_download_url") or "")
    parsed = urlsplit(download_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateError("Релиз содержит небезопасную ссылку на обновление")
    return UpdateInfo(
        version=tag.removeprefix("v"),
        download_url=download_url,
        sha256=digest.group(1).lower(),
        size=max(0, int(asset.get("size") or 0)),
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url") or ""),
    )


def create_download_path(version: str) -> Path:
    folder = Path(tempfile.mkdtemp(prefix=f"HanziLab-{version}-"))
    return folder / WINDOWS_ASSET_NAME


def download_update(
    update: UpdateInfo,
    destination: Path,
    *,
    progress=None,
    cancelled=None,
) -> Path:
    """Download and verify a release asset without exposing a partial archive."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    downloaded = 0
    request = _request(update.download_url, accept="application/octet-stream")
    try:
        with _open(request, timeout=120) as response, temporary.open("wb") as stream:
            total = update.size or int(response.headers.get("Content-Length") or 0)
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Загрузка обновления отменена")
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                digest.update(block)
                downloaded += len(block)
                if progress and total:
                    progress(downloaded, total)
        if cancelled and cancelled():
            raise UpdateError("Загрузка обновления отменена")
        if update.size and downloaded != update.size:
            raise UpdateError("Архив обновления загружен не полностью")
        if digest.hexdigest() != update.sha256:
            raise UpdateError("Контрольная сумма обновления не совпала")
        temporary.replace(destination)
        return destination
    except UpdateError:
        temporary.unlink(missing_ok=True)
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise UpdateError("Не удалось скачать обновление") from error


def launch_updater(archive: Path) -> None:
    """Copy the standalone updater to temp and detach it from the running app."""
    if not updates_supported():
        raise UpdateError("Автообновление доступно только в собранной Windows-версии")
    install_dir = Path(sys.executable).resolve().parent
    if (
        install_dir.name.casefold() != "hanzilab"
        or not (install_dir / "_internal").is_dir()
        or (install_dir / ".git").exists()
    ):
        raise UpdateError("Папка установки HanziLab не прошла проверку безопасности")
    source_updater = install_dir / UPDATER_RELATIVE_PATH
    if not source_updater.is_file():
        raise UpdateError("Рядом с HanziLab.exe не найден модуль обновления")
    updater_dir = Path(tempfile.mkdtemp(prefix="HanziLab-updater-"))
    updater = updater_dir / UPDATER_NAME
    shutil.copy2(source_updater, updater)
    lock_file = update_lock_path()
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        _write_update_lock(lock_file, os.getpid())
        subprocess.Popen(
            [
                str(updater),
                "--archive",
                str(archive.resolve()),
                "--install-dir",
                str(install_dir),
                "--pid",
                str(os.getpid()),
                "--executable",
                Path(sys.executable).name,
                "--lock-file",
                str(lock_file),
            ],
            close_fds=True,
            creationflags=creation_flags,
            cwd=updater_dir.parent,
        )
    except OSError as error:
        lock_file.unlink(missing_ok=True)
        shutil.rmtree(updater_dir, ignore_errors=True)
        raise UpdateError("Не удалось запустить модуль обновления") from error

