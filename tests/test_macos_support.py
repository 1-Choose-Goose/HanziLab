import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app_paths
from app_paths import default_user_data_dir
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication


class MacOSSupportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_source_build_uses_project_data_on_macos(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(
                default_user_data_dir(
                    platform="darwin", frozen=False, source_root=root
                ),
                root / "data",
            )

    def test_frozen_macos_app_uses_adjacent_portable_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / "Applications" / "HanziLab.app" / "Contents" / "MacOS" / "HanziLab"
            self.assertEqual(
                default_user_data_dir(
                    platform="darwin", frozen=True, executable=executable
                ),
                (root / "Applications" / "data").resolve(),
            )

    def test_local_dist_build_reuses_project_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            executable = root / "dist" / "HanziLab.app" / "Contents" / "MacOS" / "HanziLab"
            self.assertEqual(
                default_user_data_dir(
                    platform="darwin", frozen=True, executable=executable
                ),
                (root / "data").resolve(),
            )

    def test_user_dictionary_precedes_bundled_dictionary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(app_paths, "USER_DATA_DIR", root / "user"),
                patch.object(app_paths, "RESOURCE_ROOT", root / "bundle"),
                patch.object(app_paths, "SOURCE_ROOT", root / "source"),
            ):
                candidates = app_paths.dictionary_candidates()
        self.assertEqual(candidates[0], (root / "user" / "hanzi.db").resolve())
        self.assertEqual(candidates[1], (root / "bundle" / "data" / "hanzi.db").resolve())

    def test_undo_uses_platform_native_standard_key(self):
        self.assertFalse(QKeySequence(QKeySequence.StandardKey.Undo).isEmpty())


if __name__ == "__main__":
    unittest.main()
