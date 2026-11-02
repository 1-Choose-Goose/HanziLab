"""Run the test suite without triggering unstable Qt interpreter teardown."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def exit_code(result: unittest.TestResult) -> int:
    return 0 if result.wasSuccessful() else 1


def recorded_exit_code(result_path: Path, child_returncode: int) -> int:
    """Prefer the completed unittest result over a later Qt teardown crash."""
    try:
        recorded = result_path.read_text(encoding="ascii").strip()
    except OSError:
        return 1
    if recorded not in {"0", "1"}:
        return 1
    return int(recorded)


def run_suite(result_path: Path) -> int:
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    code = exit_code(result)
    result_path.write_text(str(code), encoding="ascii")
    sys.stdout.flush()
    sys.stderr.flush()
    return code


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--child-result":
        raise SystemExit(run_suite(Path(sys.argv[2])))

    with tempfile.TemporaryDirectory(prefix="hanzilab-ci-") as folder:
        result_path = Path(folder) / "unittest-result.txt"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci_test_runner",
                "--child-result",
                str(result_path),
            ],
            check=False,
        )
        raise SystemExit(recorded_exit_code(result_path, completed.returncode))


if __name__ == "__main__":
    main()
