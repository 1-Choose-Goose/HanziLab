import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMainWindow

from desktop import WindowCenteringFilter


class WindowCenteringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.centering_filter = WindowCenteringFilter(cls.app)
        cls.app.installEventFilter(cls.centering_filter)

    def assert_centers_on_show(self, window):
        window.resize(320, 200)
        window.move(0, 0)
        window.show()
        self.app.processEvents()
        self.app.processEvents()

        self.assertEqual(
            window.frameGeometry().center(),
            window.screen().availableGeometry().center(),
        )
        window.close()

    def test_main_window_is_centered(self):
        self.assert_centers_on_show(QMainWindow())

    def test_dialog_is_centered(self):
        self.assert_centers_on_show(QDialog())


if __name__ == "__main__":
    unittest.main()
