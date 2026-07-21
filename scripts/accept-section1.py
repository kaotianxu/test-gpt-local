"""Acceptance check for iteration-plan Section 1 isolation guarantees."""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOTS = ("tests/unit", "tests/integration")
DEFAULT_SEED = 20260721


def _run(command: list[str], *, label: str) -> None:
    """Run one acceptance command and fail with its captured output."""
    print(f"[section1] {label}", flush=True)
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _collect_tests() -> list[str]:
    """Collect stable pytest node IDs without including live smoke tests."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *TEST_ROOTS],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(result.returncode)

    node_ids = [line.strip() for line in result.stdout.splitlines() if "::" in line]
    if not node_ids:
        print("[section1] pytest collected no unit/integration tests", file=sys.stderr)
        raise SystemExit(2)
    return node_ids


def _run_parallel_shard(
    index: int, node_ids: list[str], temp_root: Path
) -> tuple[int, str]:
    """Run one process-isolated pytest shard and return its output."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(temp_root / f"worker-{index}"),
            *node_ids,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1")

    _run([sys.executable, "-m", "ruff", "check", "."], label="Ruff")
    _run([sys.executable, "-m", "mypy", "app"], label="mypy strict")

    node_ids = _collect_tests()
    random.Random(args.seed).shuffle(node_ids)
    print(
        f"[section1] randomized suite: {len(node_ids)} tests (seed={args.seed})",
        flush=True,
    )
    _run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *node_ids],
        label="randomized execution",
    )

    worker_count = min(args.workers, len(node_ids))
    shards = [node_ids[index::worker_count] for index in range(worker_count)]
    print(f"[section1] parallel execution: {worker_count} isolated workers", flush=True)
    with tempfile.TemporaryDirectory(prefix="gpt-local-section1-") as temp_name:
        temp_root = Path(temp_name)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(
                executor.map(
                    lambda item: _run_parallel_shard(*item, temp_root),
                    enumerate(shards),
                )
            )

    failed = False
    for index, (return_code, output) in enumerate(results):
        print(f"[section1] worker {index + 1}/{worker_count}")
        print(output, end="" if output.endswith("\n") else "\n")
        failed = failed or return_code != 0
    if failed:
        return 1

    print(
        f"[section1] PASS: {len(node_ids)} tests passed in randomized and parallel modes",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
