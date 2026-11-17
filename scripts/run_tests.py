#!/usr/bin/env python3
"""Run locally defined unittest cases once, in isolated module processes.

Keep concurrency scenarios within each module together. Isolate FastAPI route
graphs, SQLAlchemy caches and fixture cycles from later modules' startup budgets.
"""
import argparse
import importlib
import inspect
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


class ProjectLoader(unittest.TestLoader):
    def loadTestsFromModule(self, module, *, pattern=None):
        suites = []
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, unittest.TestCase) and cls.__module__ == module.__name__:
                suites.append(self.loadTestsFromTestCase(cls))
        return self.suiteClass(suites)


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pattern', nargs='?', default='test_*.py')
    parser.add_argument('--module', help=argparse.SUPPRESS)
    args = parser.parse_args()
    loader = ProjectLoader()
    if args.module:
        suite = loader.loadTestsFromModule(importlib.import_module(args.module))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    suite = loader.discover(str(ROOT / 'tests'), pattern=args.pattern)
    if loader.errors:
        print('\n'.join(loader.errors), file=sys.stderr)
        return 1
    modules = sorted({case.__class__.__module__ for case in cases(suite)})
    if not modules:
        parser.error('No unittest cases matched ' + args.pattern)
    # No retries. Nonzero exits, including terminated workers, fail the run;
    # later modules still execute so the final summary includes all regressions.
    failed = []
    for module in modules:
        print('\nTesting isolated module: ' + module, flush=True)
        result = subprocess.run([sys.executable, __file__, '--module', module], cwd=ROOT)
        if result.returncode:
            failed.append(module)
    print(f'\nRan {suite.countTestCases()} tests across {len(modules)} isolated modules; '
          f'failed modules: {len(failed)}', flush=True)
    if failed:
        print('Failed: ' + ', '.join(failed), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
