import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import updater
import updates
from desktop import UpdateDownloadTask


class Response(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.headers = {"Content-Length": str(len(value))}

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        self.close()


class UpdateClientTests(unittest.TestCase):
    def test_semantic_version_comparison(self):
        self.assertTrue(updates.is_newer_version("v1.2.0", "1.1.9"))
        self.assertFalse(updates.is_newer_version("1.2.0", "1.2.0"))
        self.assertFalse(updates.is_newer_version("1.1.9", "1.2.0"))
        with self.assertRaises(ValueError):
            updates.version_tuple("latest")

    def test_latest_release_selects_verified_windows_asset(self):
        digest = "a" * 64
        payload = {
            "tag_name": "v2.0.0",
            "body": "Important fixes",
            "html_url": "https://github.com/1-Choose-Goose/HanziLab/releases/tag/v2.0.0",
            "assets": [
                {
                    "name": updates.WINDOWS_ASSET_NAME,
                    "browser_download_url": "https://github.com/1-Choose-Goose/HanziLab/releases/download/v2.0.0/HanziLab-Windows-x64.zip",
                    "digest": f"sha256:{digest}",
                    "size": 123,
                }
            ],
        }
        with patch.object(
            updates, "_open", return_value=Response(json.dumps(payload).encode())
        ):
            result = updates.check_for_update("1.0.0")
        self.assertEqual(result.version, "2.0.0")
        self.assertEqual(result.sha256, digest)
        self.assertEqual(result.size, 123)

    def test_release_without_digest_is_rejected(self):
        payload = {
            "tag_name": "v2.0.0",
            "assets": [
                {
                    "name": updates.WINDOWS_ASSET_NAME,
                    "browser_download_url": "https://github.com/example/update.zip",
                }
            ],
        }
        with patch.object(
            updates, "_open", return_value=Response(json.dumps(payload).encode())
        ), self.assertRaisesRegex(updates.UpdateError, "SHA-256"):
            updates.check_for_update("1.0.0")

    def test_download_is_atomic_and_checksum_is_verified(self):
        content = b"verified update archive"
        information = updates.UpdateInfo(
            "2.0.0",
            "https://github.com/example/update.zip",
            hashlib.sha256(content).hexdigest(),
            len(content),
            "",
            "",
        )
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "update.zip"
            progress = []
            with patch.object(updates, "_open", return_value=Response(content)):
                updates.download_update(
                    information,
                    destination,
                    progress=lambda downloaded, total: progress.append(
                        (downloaded, total)
                    ),
                )
            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse(destination.with_suffix(".zip.part").exists())
            self.assertEqual(progress[-1], (len(content), len(content)))

            bad = updates.UpdateInfo(
                "2.0.0",
                information.download_url,
                "0" * 64,
                len(content),
                "",
                "",
            )
            with patch.object(
                updates, "_open", return_value=Response(content)
            ), self.assertRaisesRegex(updates.UpdateError, "сумма"):
                updates.download_update(bad, destination)
            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse(destination.with_suffix(".zip.part").exists())

    def test_cancelled_download_task_removes_its_temporary_directory(self):
        information = updates.UpdateInfo(
            "2.0.0",
            "https://github.com/example/update.zip",
            "0" * 64,
            1,
            "",
            "",
        )
        with tempfile.TemporaryDirectory() as folder:
            download_folder = Path(folder) / "download"
            download_folder.mkdir()
            destination = download_folder / "update.zip"
            task = UpdateDownloadTask(information, destination)
            with patch.object(
                updates,
                "download_update",
                side_effect=updates.UpdateError("Загрузка обновления отменена"),
            ):
                task.run()
            self.assertFalse(download_folder.exists())

    def test_updater_is_launched_outside_the_installation_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            install = root / "HanziLab"
            (install / "_internal").mkdir(parents=True)
            executable = install / "HanziLab.exe"
            executable.write_bytes(b"application")
            (install / "HanziLabUpdater.exe").write_bytes(b"updater")
            archive = root / "update.zip"
            archive.write_bytes(b"archive")
            updater_folder = root / "temporary-updater"
            updater_folder.mkdir()

            with (
                patch.object(updates.sys, "platform", "win32"),
                patch.object(updates.sys, "executable", str(executable)),
                patch.object(updates.sys, "frozen", True, create=True),
                patch.object(
                    updates.tempfile,
                    "mkdtemp",
                    return_value=str(updater_folder),
                ),
                patch.object(updates.subprocess, "Popen") as launch,
            ):
                updates.launch_updater(archive)

            self.assertEqual(launch.call_args.kwargs["cwd"], updater_folder.parent)


class ApplyUpdateTests(unittest.TestCase):
    @staticmethod
    def make_archive(path: Path) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("HanziLab.exe", b"new executable")
            package.writestr("HanziLabUpdater.exe", b"new updater")
            package.writestr("_internal/runtime.dll", b"new runtime")
            package.writestr("data/", b"")

    def test_update_replaces_program_preserves_data_and_removes_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            install = root / "HanziLab"
            (install / "_internal").mkdir(parents=True)
            (install / "data").mkdir()
            (install / "HanziLab.exe").write_bytes(b"old executable")
            (install / "HanziLabUpdater.exe").write_bytes(b"old updater")
            (install / "_internal" / "runtime.dll").write_bytes(b"old runtime")
            (install / "data" / "study.db").write_bytes(b"user progress")
            (install / "dictionary-server.json").write_text(
                '{"url":"https://example.test"}', encoding="utf-8"
            )
            download_folder = root / "download"
            download_folder.mkdir()
            archive = download_folder / "update.zip"
            self.make_archive(archive)

            updater.apply_update(archive, install, restart=False)

            self.assertEqual((install / "HanziLab.exe").read_bytes(), b"new executable")
            self.assertEqual(
                (install / "_internal" / "runtime.dll").read_bytes(),
                b"new runtime",
            )
            self.assertEqual(
                (install / "data" / "study.db").read_bytes(), b"user progress"
            )
            self.assertTrue((install / "dictionary-server.json").is_file())
            self.assertFalse(download_folder.exists())
            self.assertFalse(list(root.glob(".HanziLab-*")))

    def test_archive_cannot_write_outside_staging_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", b"unsafe")
            with self.assertRaisesRegex(updater.ApplyUpdateError, "опасный путь"):
                updater._safe_extract(archive, root / "staging")
            self.assertFalse((root / "outside.txt").exists())

    def test_failed_restart_rolls_back_program_and_user_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            install = root / "HanziLab"
            (install / "_internal").mkdir(parents=True)
            (install / "data").mkdir()
            (install / "HanziLab.exe").write_bytes(b"old executable")
            (install / "HanziLabUpdater.exe").write_bytes(b"old updater")
            (install / "_internal" / "runtime.dll").write_bytes(b"old runtime")
            (install / "data" / "study.db").write_bytes(b"user progress")
            archive = root / "update.zip"
            self.make_archive(archive)

            with patch.object(
                updater.subprocess, "Popen", side_effect=OSError("cannot start")
            ), self.assertRaisesRegex(OSError, "cannot start"):
                updater.apply_update(archive, install)

            self.assertEqual(
                (install / "HanziLab.exe").read_bytes(), b"old executable"
            )
            self.assertEqual(
                (install / "data" / "study.db").read_bytes(), b"user progress"
            )
            self.assertFalse(list(root.glob(".HanziLab-*")))

    def test_refuses_to_replace_a_source_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            install = Path(folder) / "HanziLab"
            (install / "_internal").mkdir(parents=True)
            (install / ".git").mkdir()
            (install / "HanziLab.exe").write_bytes(b"executable")
            (install / "HanziLabUpdater.exe").write_bytes(b"updater")
            archive = Path(folder) / "update.zip"
            self.make_archive(archive)

            with self.assertRaisesRegex(
                updater.ApplyUpdateError, "текущая установка"
            ):
                updater.apply_update(archive, install, restart=False)

            self.assertTrue((install / ".git").is_dir())
            self.assertEqual((install / "HanziLab.exe").read_bytes(), b"executable")


if __name__ == "__main__":
    unittest.main()

