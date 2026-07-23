from __future__ import annotations

from pathlib import Path

from app.storage import database as db
from app.tools.powershell import _read_process_output


def test_process_output_uses_utf8_byte_cursor(tmp_path: Path) -> None:
    db.insert_workspace(
        "ws-00000001", "project", "output", str(tmp_path), "deadbeef"
    )
    output = tmp_path / "stdout.txt"
    output.write_bytes("A中文B".encode())
    db.insert_process(
        "pr-00000001",
        "ws-00000001",
        "run_pwsh",
        stdout_path=str(output),
        stderr_path=str(tmp_path / "stderr.txt"),
    )

    first = _read_process_output("pr-00000001", max_chars=4)
    assert first["ok"] is True
    assert first["result"]["content"] == "A中"
    assert first["result"]["offset_unit"] == "bytes"
    assert first["next_cursor"] == "out1_4"

    second = _read_process_output(
        "pr-00000001", cursor=str(first["next_cursor"]), max_chars=4
    )
    assert second["result"]["content"] == "文B"
    assert second["next_cursor"] is None


def test_process_output_preserves_utf8_at_live_acceptance_boundary(
    tmp_path: Path,
) -> None:
    db.insert_workspace(
        "ws-00000001", "project", "output", str(tmp_path), "deadbeef"
    )
    output = tmp_path / "stdout.txt"
    expected = "section5-alpha\r\n中文-section5-omega\r\n"
    output.write_bytes(expected.encode("utf-8"))
    db.insert_process(
        "pr-00000001",
        "ws-00000001",
        "run_pwsh",
        stdout_path=str(output),
    )

    pages: list[str] = []
    cursor: str | None = None
    while True:
        page = _read_process_output(
            "pr-00000001", cursor=cursor, max_chars=16
        )
        pages.append(page["result"]["content"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert "".join(pages) == expected
    assert "\ufffd" not in "".join(pages)


def test_process_output_aligns_legacy_offset_to_utf8_boundary(tmp_path: Path) -> None:
    db.insert_workspace(
        "ws-00000001", "project", "output", str(tmp_path), "deadbeef"
    )
    output = tmp_path / "stdout.txt"
    output.write_bytes("A中文B".encode())
    db.insert_process(
        "pr-00000001",
        "ws-00000001",
        "run_pwsh",
        stdout_path=str(output),
    )

    result = _read_process_output("pr-00000001", offset=2, max_chars=10)
    assert result["result"]["offset"] == 4
    assert result["result"]["content"] == "文B"


def test_process_output_rejects_invalid_cursor(tmp_path: Path) -> None:
    db.insert_workspace(
        "ws-00000001", "project", "output", str(tmp_path), "deadbeef"
    )
    db.insert_process("pr-00000001", "ws-00000001", "run_pwsh")
    result = _read_process_output("pr-00000001", cursor="4")
    assert result["error"]["code"] == "INVALID_CURSOR"
