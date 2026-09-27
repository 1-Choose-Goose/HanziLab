"""Standalone atomic updater for the Windows PyInstaller onedir build."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path

PRESERVED_ITEMS = ("data", "dictionary-server.json", "dictionary-server-ca.pem")
ProgressCallback = Callable[[int, str], None]


class ApplyUpdateError(RuntimeError):
    pass


class UpdateProgressWindow:
    """Updater progress dialog styled like the main HanziLab interface."""

    def __init__(self) -> None:
        self.app = None
        self.dialog = None
        self.label = None
        self.progress = None
        if os.name != "nt":
            return
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont, QIcon
        from PySide6.QtWidgets import (
            QApplication,
            QDialog,
            QLabel,
            QProgressBar,
            QVBoxLayout,
        )

        self.app = QApplication.instance() or QApplication(["HanziLabUpdater"])
        self.app.setApplicationName("Обновление HanziLab")
        self.app.setStyle("Fusion")
        self.app.setFont(QFont("Times New Roman", 10))
        self.dialog = QDialog()
        self.dialog.setWindowTitle("Обновление HanziLab")
        self.dialog.setWindowIcon(QIcon(sys.executable))
        self.dialog.setWindowFlag(Qt.WindowType.Tool, True)
        self.dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.dialog.setFixedSize(480, 205)
        self.dialog.setObjectName("updateDialog")

        layout = QVBoxLayout(self.dialog)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)
        title = QLabel("Устанавливаем обновление")
        title.setObjectName("updateTitle")
        self.label = QLabel("Подготовка…")
        self.label.setObjectName("updateStatus")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        footer = QLabel("После установки HanziLab откроется автоматически")
        footer.setObjectName("updateFooter")
        layout.addWidget(title)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addWidget(footer)
        self.dialog.setStyleSheet(
            "QDialog#updateDialog { background: #F7F9FA; color: #182026; }"
            "QLabel#updateTitle { color: #182026; font-size: 17px; font-weight: 700; }"
            "QLabel#updateStatus { color: #344149; font-size: 13px; }"
            "QLabel#updateFooter { color: #758188; font-size: 11px; }"
            "QProgressBar { background: #E8EDEF; color: #344149; border: none; "
            "border-radius: 6px; text-align: center; }"
            "QProgressBar::chunk { background: #E05945; border-radius: 6px; }"
        )
        self.dialog.show()
        self.update(3, "Закрываем программу…")

    def update(self, value: int, text: str) -> None:
        if self.dialog is None:
            return
        self.label.setText(text)
        self.progress.setValue(max(0, min(100, value)))
        self.app.processEvents()

    def close(self) -> None:
        if self.dialog is not None:
            self.dialog.close()
            self.app.processEvents()
            self.dialog = None


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
    progress: ProgressCallback | None = None,
) -> None:
    archive = archive.resolve()
    install_dir = install_dir.resolve()
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ApplyUpdateError("Архив обновления повреждён")
    if (
        not install_dir.is_dir()
        or install_dir.name.casefold() != "hanzilab"
        or not (install_dir / executable).is_file()
        or not (install_dir / "_internal" / "HanziLabUpdater.exe").is_file()
        or not (install_dir / "_internal").is_dir()
        or (install_dir / ".git").exists()
    ):
        raise ApplyUpdateError("Не найдена текущая установка HanziLab")

    report = progress or (lambda _value, _text: None)
    report(5, "Ожидаем завершения HanziLab…")
    wait_for_process(parent_pid)
    suffix = uuid.uuid4().hex[:10]
    staging = install_dir.parent / f".HanziLab-update-{suffix}"
    backup = install_dir.parent / f".HanziLab-backup-{suffix}"
    failed_install: Path | None = None
    try:
        staging.mkdir()
        report(20, "Распаковываем новую версию…")
        _safe_extract(archive, staging)
        payload = _payload_root(staging, executable)
        if not (payload / "_internal").is_dir():
            raise ApplyUpdateError("В архиве нет зависимостей HanziLab")

        report(55, "Заменяем файлы программы…")
        install_dir.rename(backup)
        try:
            payload.rename(install_dir)
            report(78, "Сохраняем ваши данные и настройки…")
            _move_preserved_items(backup, install_dir)
            if restart:
                subprocess.Popen(
                    [str(install_dir / executable)],
                    close_fds=True,
                    cwd=install_dir,
                )
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
        report(90, "Завершаем установку…")
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
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(cleanup_script)],
            close_fds=True,
            creationflags=creation_flags,
            cwd=temporary_root,
        )
    except OSError:
        cleanup_script.unlink(missing_ok=True)
        raise


def _claim_update_lock(lock_file: Path | None) -> None:
    if lock_file is None:
        return
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"pid": os.getpid(), "created_at": time.time()}),
        encoding="utf-8",
    )
    temporary.replace(lock_file)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--executable", default="HanziLab.exe")
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    arguments: argparse.Namespace | None = None
    progress_window: UpdateProgressWindow | None = None
    try:
        arguments = parser.parse_args()
        _claim_update_lock(arguments.lock_file)
        progress_window = UpdateProgressWindow()
        # Give the main process a moment to enter its normal Qt shutdown path.
        time.sleep(0.25)
        apply_update(
            arguments.archive,
            arguments.install_dir,
            executable=arguments.executable,
            parent_pid=arguments.pid,
            restart=False,
            progress=progress_window.update,
        )
        if arguments.lock_file is not None:
            arguments.lock_file.unlink(missing_ok=True)
        if not arguments.no_restart:
            progress_window.update(100, "Готово. Запускаем HanziLab…")
            time.sleep(0.4)
            progress_window.close()
            subprocess.Popen(
                [str(arguments.install_dir / arguments.executable)],
                close_fds=True,
                cwd=arguments.install_dir,
            )
        return 0
    except Exception as error:  # noqa: BLE001 - final updater boundary
        _show_error(str(error))
        return 1
    finally:
        if progress_window is not None:
            progress_window.close()
        if arguments is not None:
            if arguments.lock_file is not None:
                arguments.lock_file.unlink(missing_ok=True)
            arguments.archive.unlink(missing_ok=True)
            try:
                arguments.archive.parent.rmdir()
            except OSError:
                pass
        _schedule_self_cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

