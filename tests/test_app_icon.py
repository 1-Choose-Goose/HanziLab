import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QImage
from PySide6.QtWidgets import QApplication, QLabel

import desktop
from study_database import StudyRepository


class AppIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        desktop.load_chinese_fonts()

    def test_png_and_multisize_windows_icon_are_valid(self):
        self.assertTrue(desktop.APP_ICON_PNG.is_file())
        self.assertTrue(desktop.APP_ICON_ICO.is_file())
        image = QImage(str(desktop.APP_ICON_PNG))
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (256, 256))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

        icon = QIcon(str(desktop.APP_ICON_ICO))
        self.assertFalse(icon.isNull())
        sizes = {(size.width(), size.height()) for size in icon.availableSizes()}
        self.assertIn((16, 16), sizes)
        self.assertIn((256, 256), sizes)

    def test_main_window_and_sidebar_use_hanzi_xingshu_brand(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            self.assertFalse(window.windowIcon().isNull())
            seal = window.findChild(QLabel, "seal")
            self.assertIsNotNone(seal)
            self.assertEqual(seal.text(), "汉")
            self.assertEqual(seal.font().family(), desktop.XINGSHU_FAMILY)
            window.close()

    def test_about_page_lists_developer_and_clickable_contacts(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            window.about_button.click()

            self.assertEqual(window.page_stack.currentIndex(), 3)
            developer = window.findChild(QLabel, "aboutDeveloper")
            contacts = window.findChild(QLabel, "aboutContacts")
            self.assertEqual(developer.text(), "Разработчик: Choose_Goose")
            self.assertTrue(contacts.openExternalLinks())
            self.assertEqual(
                contacts.textInteractionFlags(),
                Qt.TextInteractionFlag.TextBrowserInteraction,
            )
            for contact in (
                "https://vk.ru/kamereka",
                "https://t.me/choose_o_goose",
                "https://t.me/yi_bi_yi_hua",
            ):
                self.assertIn(contact, contacts.text())
            window.close()

    def test_build_configuration_embeds_and_bundles_the_icon(self):
        spec = (Path(__file__).parents[1] / "HanziLab.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn('"assets" / "icons"', spec)
        self.assertIn("hanzilab.ico", spec)

    def test_window_title_is_not_duplicated_by_display_name(self):
        source = (Path(__file__).parents[1] / "desktop.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("setApplicationDisplayName", source)


if __name__ == "__main__":
    unittest.main()
