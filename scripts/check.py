"""Run the complete local quality gate.

Usage::

    uv run python scripts/check.py

Runs formatting, lint, type and test checks, reports each result, and exits
non-zero if any of them failed.
"""

import subprocess
import sys

STEPS: list[tuple[str, list[str]]] = [
    ("format", ["ruff", "format", "--check", "."]),
    ("lint", ["ruff", "check", "."]),
    ("types", ["pyright"]),
    ("tests", ["pytest"]),
]


def main() -> int:
    failed: list[str] = []
    for name, args in STEPS:
        print(f"\n==> {name}: {' '.join(args)}", flush=True)
        result = subprocess.run([sys.executable, "-m", *args], check=False)
        if result.returncode != 0:
            failed.append(name)

    print()
    if failed:
        print(f"Quality gate FAILED: {', '.join(failed)}")
        return 1
    print("Quality gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
