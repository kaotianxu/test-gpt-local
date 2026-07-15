"""Unit tests for Phase 3: process_manager and powershell tools.

Tests the internal helper functions in isolation without a running
MCP server or a real subprocess.
"""

from pathlib import Path

import pytest

from app.services.process_manager import _PWSH_PREFIX, ProcessManager

# ═══════════════════════════════════════════════════════════════════════
# ProcessManager — _resolve_cwd
# ═══════════════════════════════════════════════════════════════════════


class TestResolveCwd:
    def test_defaults_to_worktree_root(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        assert pm._resolve_cwd(tmp_path, None) == tmp_path

    def test_accepts_subdirectory(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        sub = tmp_path / "sub"
        sub.mkdir()
        result = pm._resolve_cwd(tmp_path, "sub")
        assert result == sub.resolve()

    def test_rejects_escape(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        outside = tmp_path / ".." / "outside"
        with pytest.raises(ValueError, match="escapes the worktree"):
            pm._resolve_cwd(tmp_path, str(outside))


# ═══════════════════════════════════════════════════════════════════════
# ProcessManager — _build_env
# ═══════════════════════════════════════════════════════════════════════


class TestBuildEnv:
    def test_contains_proxy_vars(self) -> None:
        """When proxy is enabled, the env should contain HTTP_PROXY etc."""
        pm = ProcessManager()
        env = pm._build_env(None)
        # The proxy config sets url to http://127.0.0.1:7897 by default.
        assert "HTTP_PROXY" in env
        assert "HTTPS_PROXY" in env
        assert "NO_PROXY" in env

    def test_merges_custom_env(self) -> None:
        pm = ProcessManager()
        env = pm._build_env({"MY_VAR": "hello"})
        assert env.get("MY_VAR") == "hello"

    def test_check_can_suppress_python_bytecode(self) -> None:
        pm = ProcessManager()
        env = pm._build_env({"PYTHONDONTWRITEBYTECODE": "1"})
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_does_not_override_existing_env(self) -> None:
        """If the env already has HTTP_PROXY set, _build_env should not override it."""
        pm = ProcessManager()
        env = pm._build_env({"HTTP_PROXY": "http://custom:8888"})
        # setdefault means it won't override if already set.
        assert env.get("HTTP_PROXY") == "http://custom:8888"


# ═══════════════════════════════════════════════════════════════════════
# ProcessManager — _read_tail
# ═══════════════════════════════════════════════════════════════════════


class TestReadTail:
    def test_returns_content_when_smaller_than_max(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        f = tmp_path / "out.txt"
        f.write_text("hello world", encoding="utf-8")
        content, truncated = pm._read_tail(f, 100)
        assert content == "hello world"
        assert not truncated

    def test_truncates_when_larger_than_max(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        f = tmp_path / "out.txt"
        f.write_text("abcdefghij", encoding="utf-8")
        content, truncated = pm._read_tail(f, 5)
        assert content == "fghij"
        assert truncated

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        pm = ProcessManager()
        content, truncated = pm._read_tail(tmp_path / "nonexistent.txt", 100)
        assert content == ""
        assert not truncated


# ═══════════════════════════════════════════════════════════════════════
# ProcessManager — _kill_tree (test logic only, no actual kill)
# ═══════════════════════════════════════════════════════════════════════


class TestKillTree:
    def test_handles_invalid_pid_gracefully(self) -> None:
        pm = ProcessManager()
        # A negative PID should not raise an exception.
        result = pm._kill_tree(-1)
        # On Windows, taskkill may return 0 even for invalid PIDs;
        # just verify it ran without crashing.
        assert isinstance(result, bool)


# ═══════════════════════════════════════════════════════════════════════
# PowerShell prefix
# ═══════════════════════════════════════════════════════════════════════


class TestPwshPrefix:
    def test_prefix_suppresses_progress(self) -> None:
        assert "SilentlyContinue" in _PWSH_PREFIX

    def test_prefix_sets_error_action_preference(self) -> None:
        assert "PSNativeCommandUseErrorActionPreference" in _PWSH_PREFIX
