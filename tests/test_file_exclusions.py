"""Read-only Git exclusion regressions; Docker uses the same fixture matrix."""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "tests/fixtures/file-exclusions.json").read_text())


def git(*args, **kwargs):
    return subprocess.run(
        ["git", "-c", "core.excludesfile=/dev/null", *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
        **kwargs,
    )


def ignored(paths):
    result = git(
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        input="\0".join(paths).encode() + b"\0",
    )
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr.decode())
    return {path.decode() for path in result.stdout.split(b"\0") if path}


class FileExclusions(unittest.TestCase):
    def test_private_runtime_paths_are_ignored_without_reading_files(self):
        self.assertEqual(ignored(MATRIX["private"]), set(MATRIX["private"]))

    def test_safe_examples_sources_and_sql_migrations_remain_included(self):
        self.assertEqual(ignored(MATRIX["required"]), set())

    def test_no_tracked_or_staged_runtime_files_are_hidden_by_ignore_rules(self):
        # check-ignore normally skips tracked paths. --no-index is intentional:
        # exclusions cannot remove a file already present in the Git index.
        result = git("ls-files", "-c", "-z")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        paths = [path.decode() for path in result.stdout.split(b"\0") if path]
        self.assertEqual(ignored(paths), set())


if __name__ == "__main__":
    unittest.main()
