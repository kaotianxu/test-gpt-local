import hashlib
from pathlib import Path

import pytest

from app.services.path_guard import is_denied, resolve_within
from app.tools.reader import _read_item


def test_denied_paths_cover_nested_sensitive_entries(tmp_path: Path) -> None:
    assert is_denied(tmp_path / ".git" / "HEAD", tmp_path)
    assert is_denied(tmp_path / ".env" / "local", tmp_path)
    assert not is_denied(tmp_path / "app" / "server.py", tmp_path)


def test_resolve_within_rejects_escape_and_absolute_paths(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("print('ok')\n", encoding="utf-8")

    assert resolve_within(tmp_path, "app.py") == source.resolve()
    with pytest.raises(ValueError):
        resolve_within(tmp_path, "..")
    with pytest.raises(ValueError):
        resolve_within(tmp_path, str(source))


def test_read_item_returns_content_version_hash(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    raw = b"print('ok')\n"
    source.write_bytes(raw)

    result = _read_item(
        tmp_path,
        {"path": "app.py", "start_line": 1, "end_line": 1},
        remaining=100,
    )

    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "git_blob" in result
