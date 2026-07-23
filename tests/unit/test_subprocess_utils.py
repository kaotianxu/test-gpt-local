"""Tests for console-free subprocess creation flags."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

from app.services.subprocess_utils import no_window_creationflags


def test_no_window_creationflags_hides_windows_console() -> None:
    flags = no_window_creationflags()
    if os.name == "nt":
        assert flags & subprocess.CREATE_NO_WINDOW
    else:
        assert flags == 0


def test_no_window_creationflags_can_preserve_process_group() -> None:
    flags = no_window_creationflags(new_process_group=True)
    if os.name == "nt":
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert flags == 0


def test_all_noninteractive_app_subprocesses_specify_creationflags() -> None:
    app_dir = Path(__file__).parents[2] / "app"
    missing: list[str] = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if (
                isinstance(owner, ast.Name)
                and owner.id == "subprocess"
                and node.func.attr in {"run", "Popen"}
                and not any(keyword.arg == "creationflags" for keyword in node.keywords)
            ):
                missing.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")
    assert missing == []
