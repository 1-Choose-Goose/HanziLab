import unittest
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


if __name__ == "__main__":
    unittest.main()
