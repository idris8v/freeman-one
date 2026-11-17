"""Offline test-runner regressions: isolation, complete coverage and failures."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts/run_tests.py"


class TestRunner(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        self.runner = self.root / "scripts/run_tests.py"
        self.runner.write_bytes(RUNNER.read_bytes())

    def source(self, name, content):
        (self.root / "tests" / name).write_text(content)

    def run_fixture(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.runner), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_modules_are_isolated_and_imported_fixtures_are_not_repeated(self):
        self.source(
            "helper.py",
            "import unittest\nclass Foreign(unittest.TestCase):\n"
            " def test_must_not_run(self): self.fail('imported fixture ran')\n",
        )
        self.source(
            "test_first.py",
            "import unittest\nfrom helper import Foreign\n"
            "class First(unittest.TestCase):\n"
            " def test_mutate(self):\n"
            "  import builtins\n  builtins.fixture_leak = True\n",
        )
        self.source(
            "test_second.py",
            "import unittest\nclass Second(unittest.TestCase):\n"
            " def test_clean_process(self):\n"
            "  import builtins,sys\n"
            "  self.assertFalse(hasattr(builtins,'fixture_leak'))\n"
            "  self.assertNotIn('test_first',sys.modules)\n",
        )
        result = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 2 tests across 2 isolated modules", result.stdout)

    def test_failure_is_not_retried_and_later_modules_still_run(self):
        self.source(
            "test_a.py",
            "import unittest\nfrom pathlib import Path\nclass Failing(unittest.TestCase):\n"
            " def test_fail(self):\n"
            "  with Path('attempts').open('a') as f: f.write('attempt\\n')\n"
            "  self.fail('confirmed failure')\n",
        )
        self.source(
            "test_b.py",
            "import unittest\nfrom pathlib import Path\nclass Later(unittest.TestCase):\n"
            " def test_later(self): Path('later').touch()\n",
        )
        result = self.run_fixture()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.root / "attempts").read_text(), "attempt\n")
        self.assertTrue((self.root / "later").exists())
        self.assertIn("failed modules: 1", result.stdout)

    def test_pattern_selects_only_requested_module(self):
        for name in ("first", "second"):
            self.source(
                f"test_{name}.py",
                "import unittest\nclass Case(unittest.TestCase):\n"
                " def test_ok(self): pass\n",
            )
        result = self.run_fixture("test_second.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 tests across 1 isolated modules", result.stdout)
        self.assertNotIn("Testing isolated module: test_first", result.stdout)

    def test_import_failure_and_empty_selection_fail(self):
        self.source("test_broken.py", "raise RuntimeError('broken import')\n")
        result = self.run_fixture()
        self.assertEqual(result.returncode, 1)
        self.assertIn("broken import", result.stderr)
        result = self.run_fixture("test_missing.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No unittest cases matched", result.stderr)

    def test_terminated_worker_fails_the_run(self):
        self.source(
            "test_exit.py",
            "import unittest\nclass Case(unittest.TestCase):\n"
            " def test_exit(self):\n  import os\n  os._exit(7)\n",
        )
        result = self.run_fixture()
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed modules: 1", result.stdout)
