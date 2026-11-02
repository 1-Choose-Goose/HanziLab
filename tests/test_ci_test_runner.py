import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import ci_test_runner


class CiTestRunnerTests(unittest.TestCase):
    def test_exit_code_tracks_the_unittest_result(self):
        successful = Mock()
        successful.wasSuccessful.return_value = True
        failed = Mock()
        failed.wasSuccessful.return_value = False

        self.assertEqual(ci_test_runner.exit_code(successful), 0)
        self.assertEqual(ci_test_runner.exit_code(failed), 1)

    def test_recorded_success_survives_child_qt_teardown_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            result_path = Path(folder) / "result.txt"
            result_path.write_text("0", encoding="ascii")

            self.assertEqual(
                ci_test_runner.recorded_exit_code(result_path, -1073740791),
                0,
            )

    def test_missing_or_failed_result_never_masks_a_real_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            result_path = Path(folder) / "result.txt"
            self.assertEqual(ci_test_runner.recorded_exit_code(result_path, 9), 1)
            result_path.write_text("1", encoding="ascii")
            self.assertEqual(ci_test_runner.recorded_exit_code(result_path, 0), 1)


if __name__ == "__main__":
    unittest.main()
