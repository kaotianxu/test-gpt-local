from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.process_manager import _PWSH_PREFIX, ProcessManager


def test_pwsh_redirected_output_is_utf8(tmp_path: Path) -> None:
    """Match the production stdin/file-handle launch used by ProcessManager."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is not installed")

    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_file,
        stderr_path.open("w", encoding="utf-8") as stderr_file,
    ):
        process = subprocess.Popen(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
        )
        process.communicate(_PWSH_PREFIX + "Write-Output '中文'\n", timeout=30)

    assert process.returncode == 0, stderr_path.read_text(encoding="utf-8")
    assert stdout_path.read_bytes() == "中文\r\n".encode("utf-8")


def test_python_redirected_output_is_utf8(tmp_path: Path) -> None:
    """Cover the run_command(shell='python') path from live acceptance."""
    manager = ProcessManager()
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout_file,
        stderr_path.open("w", encoding="utf-8") as stderr_file,
    ):
        process = subprocess.run(
            manager._build_command_line("python", "print('中文-section5-omega')"),
            env=manager._build_env(None),
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    assert process.returncode == 0, stderr_path.read_text(encoding="utf-8")
    assert stdout_path.read_bytes() == "中文-section5-omega\r\n".encode("utf-8")
