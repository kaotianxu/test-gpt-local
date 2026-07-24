"""Behavioral tests for isolated, atomic workspace change sets."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services import change_set_store as change_sets
from app.storage import database as db


def _git(repository: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return process.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[str, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Change Set Tests")
    _git(repository, "config", "user.email", "change-sets@example.invalid")
    (repository / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repository, "add", "a.txt")
    _git(repository, "commit", "-m", "initial")
    head = _git(repository, "rev-parse", "HEAD")
    db.insert_workspace(
        "ws-change-set",
        "project",
        "change-set-tests",
        str(repository),
        head,
    )
    return "ws-change-set", repository


def test_stage_validate_commit_isolated_and_preserves_index(
    workspace: tuple[str, Path],
) -> None:
    workspace_id, repository = workspace
    (repository / "staged.txt").write_text("already staged\n", encoding="utf-8")
    _git(repository, "add", "staged.txt")
    index = Path(_git(repository, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = repository / index
    index_before = index.read_bytes()

    begun = change_sets.begin(workspace_id, "atomic edit")
    assert (repository / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert index.read_bytes() == index_before

    replaced = change_sets.stage_replace(
        begun["change_set_id"],
        "a.txt",
        "hello",
        "goodbye",
        "replace greeting",
    )
    patch = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+created\n"
    staged = change_sets.stage_patch(
        begun["change_set_id"],
        patch,
        "create a file",
    )
    assert staged["ordinal"] == 2
    assert (repository / "a.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (repository / "new.txt").exists()
    assert index.read_bytes() == index_before

    validated = change_sets.validate(
        begun["change_set_id"],
        staged["revision"],
    )
    assert validated["status"] == "validated"
    assert replaced["validation_invalidated"] is False
    committed = change_sets.commit(
        begun["change_set_id"],
        validated["validated_digest"],
        validated["before_tree_hash"],
    )

    assert committed["status"] == "committed"
    assert (repository / "a.txt").read_text(encoding="utf-8") == "goodbye\n"
    assert (repository / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert index.read_bytes() == index_before
    assert change_sets.workspace_tree(repository) == validated["after_tree_hash"]


def test_workspace_change_conflicts_without_writes(
    workspace: tuple[str, Path],
) -> None:
    workspace_id, repository = workspace
    begun = change_sets.begin(workspace_id, "conflict")
    staged = change_sets.stage_replace(
        begun["change_set_id"],
        "a.txt",
        "hello",
        "staged",
        "stage",
    )
    validated = change_sets.validate(begun["change_set_id"], staged["revision"])
    (repository / "a.txt").write_text("external\n", encoding="utf-8")

    with pytest.raises(change_sets.ChangeSetError) as captured:
        change_sets.commit(
            begun["change_set_id"],
            validated["validated_digest"],
        )
    assert captured.value.code == "CHANGE_SET_CONFLICT"
    assert (repository / "a.txt").read_text(encoding="utf-8") == "external\n"
    assert change_sets.get(begun["change_set_id"])["status"] == "validated"


def test_stage_operation_replay_and_rollback(workspace: tuple[str, Path]) -> None:
    workspace_id, repository = workspace
    begun = change_sets.begin(workspace_id, "replay")
    first = change_sets.stage_replace(
        begun["change_set_id"],
        "a.txt",
        "hello",
        "changed",
        "stage",
        operation_id="op_stable",
    )
    replay = change_sets.stage_replace(
        begun["change_set_id"],
        "a.txt",
        "hello",
        "changed",
        "stage",
        operation_id="op_stable",
    )
    assert replay["ordinal"] == first["ordinal"]
    assert replay["idempotent_replay"] is True

    rolled_back = change_sets.rollback(begun["change_set_id"])
    replayed_rollback = change_sets.rollback(begun["change_set_id"])
    assert rolled_back["status"] == "rolled_back"
    assert replayed_rollback["idempotent_replay"] is True
    assert (repository / "a.txt").read_text(encoding="utf-8") == "hello\n"


def test_unchanged_submodule_is_preserved_and_cannot_be_edited(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    _git(child, "init")
    _git(child, "config", "user.name", "Change Set Tests")
    _git(child, "config", "user.email", "change-sets@example.invalid")
    (child / "child.txt").write_text("child\n", encoding="utf-8")
    _git(child, "add", "child.txt")
    _git(child, "commit", "-m", "child")

    repository = tmp_path / "parent"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Change Set Tests")
    _git(repository, "config", "user.email", "change-sets@example.invalid")
    (repository / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repository, "add", "a.txt")
    _git(repository, "commit", "-m", "parent")
    _git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child),
        "vendor/external",
    )
    _git(repository, "commit", "-am", "add submodule")
    head = _git(repository, "rev-parse", "HEAD")
    db.insert_workspace(
        "ws-change-set-submodule",
        "project",
        "change-set-submodule-tests",
        str(repository),
        head,
    )

    begun = change_sets.begin("ws-change-set-submodule", "preserve gitlink")
    forbidden_patch = (
        "--- a/vendor/external/child.txt\n"
        "+++ b/vendor/external/child.txt\n"
        "@@ -1 +1 @@\n"
        "-child\n"
        "+changed\n"
    )
    with pytest.raises(change_sets.ChangeSetError) as captured:
        change_sets.stage_patch(
            begun["change_set_id"],
            forbidden_patch,
            "must reject submodule content",
        )
    assert captured.value.code == "PATH_DENIED"

    staged = change_sets.stage_replace(
        begun["change_set_id"],
        "a.txt",
        "hello",
        "changed",
        "ordinary edit",
    )
    validated = change_sets.validate(begun["change_set_id"], staged["revision"])
    committed = change_sets.commit(
        begun["change_set_id"],
        validated["validated_digest"],
    )
    assert committed["status"] == "committed"
    assert (repository / "a.txt").read_text(encoding="utf-8") == "changed\n"
    assert (repository / "vendor" / "external" / "child.txt").read_text(
        encoding="utf-8"
    ) == "child\n"
    assert "160000 commit" in _git(
        repository,
        "ls-tree",
        validated["after_tree_hash"],
        "vendor/external",
    )
