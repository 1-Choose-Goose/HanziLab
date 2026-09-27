"""Standalone atomic updater for the Windows PyInstaller onedir build."""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

PRESERVED_ITEMS = ("data", "dictionary-server.json", "dictionary-server-ca.pem")


class ApplyUpdateError(RuntimeError):
    pass


def wait_for_process(pid: int, timeout: float = 120.0) -> None:
    if pid <= 0 or os.name != "nt":
        return
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return
    try:
        result = ctypes.windll.kernel32.WaitForSingleObject(
            handle, max(0, round(timeout * 1000))
        )
        if result == 0x00000102:
            raise ApplyUpdateError("HanziLab не завершился вовремя")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _safe_extract(archive: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            mode = item.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ApplyUpdateError("Архив обновления содержит символическую ссылку")
            target = (destination / item.filename).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise ApplyUpdateError("Архив обновления содержит опасный путь")
        package.extractall(destination)


def _payload_root(staging: Path, executable: str) -> Path:
    if (staging / executable).is_file():
        return staging
    children = [item for item in staging.iterdir() if item.is_dir()]
    if len(children) == 1 and (children[0] / executable).is_file():
        return children[0]
    raise ApplyUpdateError("В архиве нет новой версии HanziLab.exe")


def _move_preserved_items(backup: Path, installed: Path) -> None:
    for name in PRESERVED_ITEMS:
        source = backup / name
        destination = installed / name
        if not source.exists():
            continue
        if name == "data" and destination.exists():
            shutil.rmtree(destination)
        elif destination.exists():
            continue
        shutil.move(str(source), str(destination))


def apply_update(
    archive: Path,
    install_dir: Path,
    *,
    executable: str = "HanziLab.exe",
    parent_pid: int = 0,
    restart: bool = True,
) -> None:
    archive = archive.resolve()
    install_dir = install_dir.resolve()
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ApplyUpdateError("Архив обновления повреждён")
    if not install_dir.is_dir() or not (install_dir / executable).is_file():
        raise ApplyUpdateError("Не найдена текущая установка HanziLab")

    wait_for_process(parent_pid)
    suffix = uuid.uuid4().hex[:10]
    staging = install_dir.parent / f".HanziLab-update-{suffix}"
    backup = install_dir.parent / f".HanziLab-backup-{suffix}"
    failed_install: Path | None = None
    try:
        staging.mkdir()
        _safe_extract(archive, staging)
        payload = _payload_root(staging, executable)
        if not (payload / "_internal").is_dir():
            raise ApplyUpdateError("В архиве нет зависимостей HanziLab")

        install_dir.rename(backup)
        try:
            payload.rename(install_dir)
            _move_preserved_items(backup, install_dir)
            if restart:
                subprocess.Popen([str(install_dir / executable)], close_fds=True)
        except Exception:
            if install_dir.exists():
                for name in PRESERVED_ITEMS:
                    source = install_dir / name
                    destination = backup / name
                    if source.exists() and not destination.exists():
                        shutil.move(str(source), str(destination))
                failed_install = install_dir.with_name(
                    install_dir.name + f".failed-{suffix}"
                )
                install_dir.rename(failed_install)
            backup.rename(install_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        archive.unlink(missing_ok=True)
        try:
            archive.parent.rmdir()
        except OSError:
            pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if failed_install is not None:
            shutil.rmtree(failed_install, ignore_errors=True)


def _show_error(message: str) -> None:
    log = Path(tempfile.gettempdir()) / "HanziLab-updater-error.txt"
    log.write_text(message, encoding="utf-8")
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None,
            message + f"\n\nПодробности: {log}",
            "HanziLab — ошибка обновления",
            0x10,
        )


def _schedule_self_cleanup() -> None:
    """Delete the copied one-file updater after this process releases it."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    executable = Path(sys.executable).resolve()
    folder = executable.parent
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if temporary_root not in folder.parents or not folder.name.startswith(
        "HanziLab-updater-"
    ):
        return
    # A batch helper avoids cmd.exe's fragile quoting for a compound /c command.
    # It lives outside the updater directory and removes itself when done.
    descriptor, cleanup_name = tempfile.mkstemp(
        prefix="HanziLab-cleanup-", suffix=".cmd", dir=temporary_root
    )
    cleanup_script = Path(cleanup_name)
    with os.fdopen(descriptor, "w", encoding="mbcs", newline="\r\n") as script:
        script.write(
            "@echo off\n"
            "for /l %%i in (1,1,60) do (\n"
            f'  rmdir /s /q "{folder}" 2>nul\n'
            f'  if not exist "{folder}" goto cleanup_done\n'
            "  ping 127.0.0.1 -n 2 >nul\n"
            ")\n"
            ":cleanup_done\n"
            'del /f /q "%~f0"\n'
        )
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(cleanup_script)],
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError:
        cleanup_script.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--executable", default="HanziLab.exe")
    parser.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    arguments: argparse.Namespace | None = None
    try:
        arguments = parser.parse_args()
        # Give the main process a moment to enter its normal Qt shutdown path.
        time.sleep(0.25)
        apply_update(
            arguments.archive,
            arguments.install_dir,
            executable=arguments.executable,
            parent_pid=arguments.pid,
            restart=not arguments.no_restart,
        )
        return 0
    except Exception as error:  # noqa: BLE001 - final updater boundary
        _show_error(str(error))
        return 1
    finally:
        if arguments is not None:
            arguments.archive.unlink(missing_ok=True)
            try:
                arguments.archive.parent.rmdir()
            except OSError:
                pass
        _schedule_self_cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

