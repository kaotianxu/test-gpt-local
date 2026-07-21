"""MCP tools: run_command, write_process_input, send_process_signal.

Provides interactive process execution with PTY support on Windows
(ConPTY) and stdin/signal control for running processes.

Tools
-----
- ``run_command`` — execute a command (interactive or batch, with optional PTY)
- ``write_process_input`` — write text to a running process's stdin
- ``send_process_signal`` — send interrupt, EOF, or terminate to a process
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import get_process_config
from app.services.envelope import (
    audit_event,
    elapsed_ms,
    error_result,
    generate_request_id,
    ok_result,
)
from app.services.path_guard import is_denied, resolve_within
from app.services.process_manager import ProcessManager
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

# Poll interval (seconds) when waiting for a process to finish.
_WAIT_POLL_INTERVAL = 0.5


def _git_status(worktree: Path) -> str | None:
    """Run ``git status --short --branch`` and return the stdout, or None."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _run_command(
    workspace_id: str,
    command: str,
    shell: str = "pwsh",
    working_directory: str = "",
    tty: bool = False,
    wait: bool = True,
    timeout_seconds: int = 600,
    columns: int = 80,
    rows: int = 24,
) -> dict[str, Any]:
    """Execute a command in the workspace (interactive or batch).

    See the ``run_command`` tool docstring for parameter details.
    """
    start = time.monotonic()
    request_id = generate_request_id()
    input_summary = f"shell={shell!r} tty={tty} wait={wait}"

    def fail(code: str, message: str) -> dict[str, Any]:
        audit_event(
            tool_name="run_command",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=input_summary,
            success=False,
            duration_ms=elapsed_ms(start),
            error_code=code,
        )
        return error_result(
            code, message, workspace_id=workspace_id, request_id=request_id
        )

    # ---- 1. Look up workspace ----
    record = get_workspace(workspace_id)
    if record is None:
        return fail("WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}")
    worktree = Path(record["worktree_path"])
    if not worktree.exists():
        return fail("STALE_WORKSPACE", f"worktree path missing on disk: {worktree}")

    # ---- 2. Validate inputs ----
    if (not command or not command.strip()) and shell != "python":
        return fail("INVALID_INPUT", "command must be a non-empty string")

    supported_shells = ("pwsh", "cmd", "python", "wsl-bash")
    if shell not in supported_shells:
        return fail(
            "INVALID_INPUT",
            f"shell must be one of {supported_shells}, got: {shell!r}",
        )

    timeout = min(
        max(timeout_seconds, 1),
        int(get_process_config().get("max_timeout_seconds", 3600)),
    )

    # ---- 3. Resolve working directory ----
    cwd = None
    if working_directory:
        try:
            resolved = resolve_within(worktree, working_directory, must_exist=False)
            if is_denied(resolved, worktree):
                return fail("PATH_DENIED", "working_directory is denied by policy")
            cwd = str(resolved)
        except ValueError as exc:
            return fail("PATH_DENIED", str(exc))

    # ---- 4. Spawn the process ----
    pm = ProcessManager.get_instance()

    try:
        spawn_result = pm.spawn_interactive(
            workspace_id=workspace_id,
            worktree_path=worktree,
            command=command,
            shell=shell,
            working_directory=cwd,
            timeout_seconds=timeout,
            tty=tty,
            columns=columns,
            rows=rows,
            request_id=request_id,
        )
    except RuntimeError as exc:
        return fail("RATE_LIMITED", str(exc))

    if "error" in spawn_result:
        return fail(
            str(spawn_result.get("code", "INTERNAL_ERROR")),
            str(spawn_result.get("error", "unknown spawn error")),
        )

    process_id = spawn_result["process_id"]

    # ---- 5. Wait (if requested) ----
    if wait:
        # Poll until the status changes from "running"/"queued".
        terminal_statuses = {"passed", "failed", "timed_out", "cancelled"}
        deadline = time.monotonic() + timeout + 5
        while time.monotonic() < deadline:
            result = pm.get_result(process_id)
            status = result.get("status", "")
            if status in terminal_statuses:
                result["git_status_after"] = _git_status(worktree)
                final = ok_result(
                    result,
                    workspace_id=workspace_id,
                    request_id=request_id,
                    truncated=bool(result.get("truncated")),
                )
                audit_event(
                    tool_name="run_command",
                    request_id=request_id,
                    workspace_id=workspace_id,
                    input_summary=f"shell={shell!r} tty={tty} wait={wait}",
                    success=True,
                    duration_ms=elapsed_ms(start),
                )
                return final
            time.sleep(_WAIT_POLL_INTERVAL)

        # Timed out waiting.
        pm.cancel(process_id)
        result = pm.get_result(process_id)
        result["git_status_after"] = _git_status(worktree)
        response = ok_result(
            result,
            workspace_id=workspace_id,
            request_id=request_id,
            truncated=bool(result.get("truncated")),
        )
        audit_event(
            tool_name="run_command", request_id=request_id,
            workspace_id=workspace_id, input_summary=input_summary,
            success=False, duration_ms=elapsed_ms(start), error_code="PROCESS_TIMEOUT",
        )
        return response

    # Async: return immediately with the process_id.
    result = ok_result(
        {
            "process_id": process_id,
            "status": "running",
            "workspace_id": workspace_id,
            "tty": tty,
        },
        workspace_id=workspace_id,
        request_id=request_id,
    )
    audit_event(
        tool_name="run_command",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"shell={shell!r} tty={tty} wait={wait}",
        success=True,
        duration_ms=elapsed_ms(start),
    )
    return result


def _write_process_input(
    process_id: str,
    text: str,
    append_newline: bool = True,
) -> dict[str, Any]:
    """Write text to a running process's stdin.

    See the ``write_process_input`` tool docstring for parameter details.
    """
    start = time.monotonic()
    request_id = generate_request_id()

    # Reject an intrinsically invalid request before consulting storage.
    if not text and not append_newline:
        return error_result(
            "INVALID_INPUT",
            "text must be non-empty when append_newline is False",
            request_id=request_id,
            extra={"process_id": process_id},
        )

    pm = ProcessManager.get_instance()
    record = pm.get_record(process_id)
    workspace_id = str(record["workspace_id"]) if record else None

    def fail(code: str, message: str) -> dict[str, Any]:
        audit_event(
            tool_name="write_process_input", request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"process_id={process_id!r} text_len={len(text)}",
            success=False, duration_ms=elapsed_ms(start), error_code=code,
        )
        return error_result(code, message, request_id=request_id, extra={"process_id": process_id})

    result = pm.write_input(process_id, text, append_newline=append_newline)

    if "error" in result:
        code = "PROCESS_NOT_FOUND"
        err = result.get("error", "")
        if "not running" in err or "has already exited" in err:
            code = "PROCESS_NOT_RUNNING"
        return fail(code, str(err))

    response = ok_result(result, request_id=request_id)
    audit_event(
        tool_name="write_process_input",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=(
            f"process_id={process_id!r} text_len={len(text)} "
            f"append_newline={append_newline}"
        ),
        success=True,
        duration_ms=elapsed_ms(start),
    )
    return response


def _send_process_signal(
    process_id: str,
    signal: str,
) -> dict[str, Any]:
    """Send a signal to a running process.

    See the ``send_process_signal`` tool docstring for parameter details.
    """
    start = time.monotonic()
    request_id = generate_request_id()

    supported_signals = ("interrupt", "eof", "terminate")
    if signal not in supported_signals:
        return error_result(
            "INVALID_INPUT",
            f"signal must be one of {supported_signals}, got: {signal!r}",
            request_id=request_id,
            extra={"process_id": process_id},
        )

    pm = ProcessManager.get_instance()
    record = pm.get_record(process_id)
    workspace_id = str(record["workspace_id"]) if record else None

    def fail(code: str, message: str) -> dict[str, Any]:
        audit_event(
            tool_name="send_process_signal", request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"process_id={process_id!r} signal={signal!r}",
            success=False, duration_ms=elapsed_ms(start), error_code=code,
        )
        return error_result(code, message, request_id=request_id, extra={"process_id": process_id})

    result = pm.send_signal(process_id, signal)

    if "error" in result:
        code = "PROCESS_NOT_FOUND"
        err = result.get("error", "")
        if "not running" in err:
            code = "PROCESS_NOT_RUNNING"
        return fail(code, str(err))

    response = ok_result(result, request_id=request_id)
    audit_event(
        tool_name="send_process_signal",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"process_id={process_id!r} signal={signal!r}",
        success=True,
        duration_ms=elapsed_ms(start),
    )
    return response


def _resize_terminal(process_id: str, columns: int, rows: int) -> dict[str, Any]:
    """Resize a running PTY terminal."""
    request_id = generate_request_id()
    result = ProcessManager.get_instance().resize_terminal(process_id, columns, rows)
    if "error" in result:
        message = str(result["error"])
        code = "PROCESS_NOT_FOUND" if "not found" in message else "PTY_NOT_ACTIVE"
        return error_result(code, message, request_id=request_id, extra={"process_id": process_id})
    return ok_result(result, request_id=request_id)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_tools(mcp: FastMCP) -> None:
    """Register PTY/interactive process tools on the FastMCP instance."""

    @mcp.tool(
        name="run_command",
        description=(
            "Execute a command (shell, Python, REPL, etc.) in the workspace. "
            "Supports both batch and interactive modes.\n\n"
            "**Shells:**\n"
            '- ``pwsh`` — PowerShell 7 (default)\n'
            '- ``cmd`` — Windows Command Prompt (cmd.exe)\n'
            '- ``python`` — Python interpreter (``python -c <command>``)\n'
            '- ``wsl-bash`` — WSL Bash (requires WSL installed)\n\n'
            "**TTY mode (``tty=True``):**\n"
            "Uses Windows ConPTY for proper terminal emulation. "
            "Required for interactive programs like REPLs, debuggers, "
            "and programs that prompt for confirmation.\n\n"
            "**Wait mode:**\n"
            "- ``wait=True`` (default): block until the process finishes "
            "and return the full result.\n"
            "- ``wait=False``: return immediately with a ``process_id``. "
            "Use ``write_process_input`` to send input, "
            "``send_process_signal`` to send signals, and "
            "``get_process_result`` to poll for completion.\n\n"
            "**Output:** stdout and stderr are captured to files. "
            "The response includes the tail of both streams."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def run_command(
        workspace_id: str,
        command: str,
        shell: str = "pwsh",
        working_directory: str = "",
        tty: bool = False,
        wait: bool = True,
        timeout_seconds: int = 600,
        columns: int = 80,
        rows: int = 24,
    ) -> dict[str, object]:
        """Execute a command in the workspace.

        Args:
            workspace_id: The target workspace.
            command: The command to execute (multi-line allowed).
            shell: The shell to use (``"pwsh"``, ``"cmd"``, ``"python"``,
                   ``"wsl-bash"``).  Defaults to ``"pwsh"``.
            working_directory: Relative path inside the worktree.
            tty: When ``True``, use a pseudo-terminal (ConPTY on Windows)
                 for interactive programs.  Defaults to ``False``.
            wait: When ``True`` (default), block until the process finishes.
            timeout_seconds: Maximum execution time (1-3600, default 600).
        """
        log.info(
            "run_command workspace_id=%s shell=%s tty=%s wait=%s timeout=%s "
            "command_len=%d",
            workspace_id,
            shell,
            tty,
            wait,
            timeout_seconds,
            len(command),
        )
        return await asyncio.to_thread(
            _run_command,
            workspace_id=workspace_id,
            command=command,
            shell=shell,
            working_directory=working_directory,
            tty=tty,
            wait=wait,
            timeout_seconds=timeout_seconds,
            columns=columns,
            rows=rows,
        )

    @mcp.tool(
        name="write_process_input",
        description=(
            "Write text to a running process's stdin.  Use this to send "
            "input to interactive programs (REPLs, debuggers, prompts).\n\n"
            "By default a newline is appended to the text (like pressing "
            "Enter).  Set ``append_newline=False`` to send raw text.\n\n"
            "Returns ``PROCESS_NOT_RUNNING`` if the process has already "
            "exited, or ``PROCESS_NOT_FOUND`` if the process_id is unknown."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def write_process_input(
        process_id: str,
        text: str,
        append_newline: bool = True,
    ) -> dict[str, object]:
        """Write text to a running process.

        Args:
            process_id: The process ID returned by ``run_command``.
            text: The text to write to the process's stdin.
            append_newline: When ``True`` (default), append a newline
                to the text if it doesn't already end with one.
        """
        log.info(
            "write_process_input process_id=%s text_len=%d append_newline=%s",
            process_id,
            len(text),
            append_newline,
        )
        return _write_process_input(process_id, text, append_newline)

    @mcp.tool(
        name="send_process_signal",
        description=(
            "Send a signal to a running process.\n\n"
            "Signals:\n"
            '- ``"interrupt"`` — Ctrl+C (SIGINT).  Stops the currently '
            "running command in the terminal.\n"
            '- ``"eof"`` — Close stdin (EOF).  Gracefully ends programs '
            "that read from stdin.\n"
            "- ``\"terminate\"`` — Force-kill the entire process tree.\n\n"
            "Use ``terminate`` as a last resort when ``interrupt`` does "
            "not stop the process within a reasonable time."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def send_process_signal(
        process_id: str,
        signal: str,
    ) -> dict[str, object]:
        """Send a signal to a running process.

        Args:
            process_id: The process ID returned by ``run_command``.
            signal: One of ``"interrupt"``, ``"eof"``, or ``"terminate"``.
        """
        log.info(
            "send_process_signal process_id=%s signal=%s",
            process_id,
            signal,
        )
        return _send_process_signal(process_id, signal)

    @mcp.tool(
        name="resize_terminal",
        description=(
            "Resize an active PTY terminal. Columns must be 20-500 and rows "
            "must be 5-200. Returns PTY_NOT_ACTIVE for pipe-based processes."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def resize_terminal(
        process_id: str,
        columns: int = 80,
        rows: int = 24,
    ) -> dict[str, object]:
        """Resize an active PTY process."""
        return _resize_terminal(process_id, columns, rows)
