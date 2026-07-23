"""Behavior tests for the Section 4 symbol-level code intelligence tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.tools import code_intelligence


@pytest.fixture
def indexed_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    (tmp_path / "service.py").write_text(
        """class Store:
    def save(self, value: str) -> None:
        raise NotImplementedError

class FileStore(Store):
    def save(self, value: str) -> None:
        write_file(value)

def write_file(value: str) -> None:
    print(value)

def run() -> None:
    FileStore().save('ok')
""",
        encoding="utf-8",
    )
    (tmp_path / "client.ts").write_text(
        """interface Client { request(): void }
export class HttpClient implements Client {
  request(): void { send(); }
}
function send(): void {}
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    monkeypatch.setattr(
        code_intelligence,
        "get_workspace",
        lambda workspace_id: (
            {"worktree_path": str(tmp_path)} if workspace_id == "ws-00000001" else None
        ),
    )
    return tmp_path


def test_symbols_definitions_references_and_implementations(indexed_repo: Path) -> None:
    symbols = code_intelligence.list_symbols("ws-00000001")
    assert symbols["ok"] is True
    qualified = {item["qualified_name"] for item in symbols["result"]["symbols"]}
    assert {"Store", "Store.save", "FileStore", "FileStore.save", "HttpClient.request"} <= qualified

    definitions = code_intelligence.find_definition("ws-00000001", "FileStore.save")
    assert [item["line"] for item in definitions["result"]["definitions"]] == [6]

    references = code_intelligence.find_references("ws-00000001", "write_file")
    assert references["result"]["count"] == 2

    implementations = code_intelligence.find_implementations("ws-00000001", "Store")
    assert [item["qualified_name"] for item in implementations["result"]["implementations"]] == [
        "FileStore"
    ]

    ts_implementations = code_intelligence.find_implementations("ws-00000001", "Client")
    assert [item["qualified_name"] for item in ts_implementations["result"]["implementations"]] == [
        "HttpClient"
    ]


def test_call_hierarchy_and_diagnostics(indexed_repo: Path) -> None:
    hierarchy = code_intelligence.get_call_hierarchy("ws-00000001", "write_file")
    incoming = {item["qualified_name"] for item in hierarchy["result"]["incoming"]}
    assert "FileStore.save" in incoming

    clean = code_intelligence.get_diagnostics("ws-00000001", "service.py")
    assert clean["result"]["diagnostics"] == []

    (indexed_repo / "broken.py").write_text("def nope(:\n", encoding="utf-8")
    broken = code_intelligence.get_diagnostics("ws-00000001", "broken.py")
    assert broken["result"]["count"] == 1
    assert broken["result"]["diagnostics"][0]["source"] == "fallback-parser"


def test_changed_symbols(indexed_repo: Path) -> None:
    service = indexed_repo / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8").replace("print(value)", "print(value.upper())"),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "service.py"], cwd=indexed_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "change writer"], cwd=indexed_repo, check=True)

    changed = code_intelligence.get_changed_symbols("ws-00000001")
    names = {item["qualified_name"] for item in changed["result"]["symbols"]}
    assert "write_file" in names


def test_invalid_inputs_and_workspace_errors(indexed_repo: Path) -> None:
    missing = code_intelligence.list_symbols("ws-ffffffff")
    assert missing["error"]["code"] == "WORKSPACE_NOT_FOUND"

    invalid = code_intelligence.find_definition("ws-00000001", "not valid!")
    assert invalid["error"]["code"] == "INVALID_INPUT"

    denied = code_intelligence.list_symbols("ws-00000001", "../outside")
    assert denied["error"]["code"] == "PATH_DENIED"
