"""Run executable acceptance gates for iteration-plan Sections 1 through 9."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_TESTS = "acceptance/test_iteration_plan.py"


def _command_for(section: int, workers: int) -> list[str]:
    if section == 1:
        return [
            sys.executable,
            "scripts/accept-section1.py",
            "--workers",
            str(workers),
        ]
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        CONTRACT_TESTS,
        "-k",
        f"section_{section}_",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--section",
        default="all",
        choices=["all", *(str(section) for section in range(1, 10))],
    )
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    sections = range(1, 10) if args.section == "all" else [int(args.section)]
    failures: list[int] = []
    for section in sections:
        print(f"\n[iteration] Section {section} acceptance", flush=True)
        result = subprocess.run(_command_for(section, args.workers), cwd=PROJECT_ROOT)
        if result.returncode:
            failures.append(section)

    if failures:
        joined = ", ".join(str(section) for section in failures)
        print(f"\n[iteration] FAIL: Sections {joined}", file=sys.stderr)
        return 1
    if args.section == "all":
        message = "Sections 1-9 satisfy their acceptance contracts"
    else:
        message = f"Section {args.section} satisfies its acceptance contract"
    print(f"\n[iteration] PASS: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
