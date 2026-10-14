"""Run the test suite without triggering unstable Qt interpreter teardown."""

from __future__ import annotations

import os
import sys
import unittest


def exit_code(result: unittest.TestResult) -> int:
    return 0 if result.wasSuccessful() else 1


def main() -> None:
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    code = exit_code(result)
    sys.stdout.flush()
    sys.stderr.flush()
    # Some PySide/Qt builds terminate with a false non-zero status while the
    # interpreter finalizes after a successful suite on Windows runners.
    # Preserve unittest's real result and skip only that unstable teardown.
    os._exit(code)


if __name__ == "__main__":
    main()
