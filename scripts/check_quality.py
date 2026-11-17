#!/usr/bin/env python3
"""Repeatable project checks; --server-tests needs a separate test PostgreSQL.

Does not deploy, run application lifespan, contact business providers or read
server secrets. Browser checks serve local files with mocked HTTP APIs.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
FORMATTED = [
    "app/web/routes.py",
    "app/web/cabinet",
    "scripts/check_quality.py",
    "tests/test_cabinet_api.py",
]


def run(command, env=None):
    print("\nChecking: " + " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--server-tests", action="store_true")
    args = parser.parse_args()

    # Reject a mistaken production URL before any test or subprocess is started.
    if args.server_tests:
        url = urlsplit(os.environ.get("DATABASE_URL", ""))
        database = unquote(url.path).lstrip("/")
        if (
            os.environ.get("AI_TEST_POSTGRES") != "1"
            or url.scheme not in {"postgresql", "postgresql+asyncpg"}
            or not (
                database.startswith("test_") or database.endswith(("_test", "_tests"))
            )
        ):
            parser.error(
                "Use AI_TEST_POSTGRES=1 and a separate PostgreSQL database named "
                "test_* or *_test/*_tests. Production databases are not test targets."
            )

    python = sys.executable
    run([python, "scripts/check_assets.py", "--git-only"])
    run([python, "-m", "ruff", "check", "app", "scripts", "tests"])
    run([python, "-m", "ruff", "check", "--select", "F,I", *FORMATTED])
    run([python, "-m", "ruff", "format", "--check", *FORMATTED])
    run(
        [
            python,
            "scripts/audit_project.py",
            "--node",
            args.node,
            "--output",
            "artifacts/quality/source-manifest.json",
        ]
    )
    run([args.node, "scripts/build_styles.cjs", "--check"])
    run([args.node, "--test", "tests/style_policy.test.cjs"])

    if args.server_tests:
        run([python, "scripts/run_tests.py"])
        run(
            [
                python,
                "-m",
                "pytest",
                "tests/regression",
                "-q",
                "-o",
                "asyncio_mode=auto",
            ]
        )

    if args.ui:
        env = dict(os.environ)
        env.setdefault(
            "FREEMAN_PLAYWRIGHT_MODULE", str(ROOT / "node_modules/playwright")
        )
        for test in sorted((ROOT / "tests").glob("ui_*.cjs")):
            run([args.node, str(test.relative_to(ROOT))], env=env)

    print("\nAll requested checks passed.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
