"""Persistent, failure-atomic workspace change sets.

The real worktree is captured into a Git tree through a temporary index.
Every staged edit is applied to an isolated materialisation of that tree.
Only a validated final patch may be committed back to the worktree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.config import get_change_set_config
from app.services.path_guard import is_denied, resolve_within
from app.services.subprocess_utils import no_window_creationflags
from app.storage import database as db
from app.tools import patcher

ACTIVE_STATUSES = frozenset({"open", "validated", "committing", "recovery_required"})
TERMINAL_STATUSES = frozenset({"committed", "rolled_back", "expired"})
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ChangeSetError(RuntimeError):
    """A stable change-set failure suitable for an MCP error envelope."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> Path:
    root = db._effective_db_path().parent / "change_sets"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _directory(change_set_id: str) -> Path:
    if not change_set_id.startswith("cs_") or not change_set_id[3:].isalnum():
        raise ChangeSetError("CHANGE_SET_NOT_FOUND", "change set not found")
    candidate = (_root() / change_set_id).resolve()
    try:
        candidate.relative_to(_root().resolve())
    except ValueError as exc:
        raise ChangeSetError("CHANGE_SET_NOT_FOUND", "change set not found") from exc
    return candidate


def _lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _run_git(
    repository: Path,
    args: list[str],
    *,
    work_tree: Path | None = None,
    index_file: Path | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    if work_tree is not None:
        env["GIT_WORK_TREE"] = str(work_tree.resolve())
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file.resolve())
    process = subprocess.run(
        [os.environ.get("GIT_EXECUTABLE", "git"), *args],
        cwd=repository,
        env=env,
        input=input_bytes,
        capture_output=True,
        timeout=60,
        creationflags=no_window_creationflags(),
    )
    if check and process.returncode:
        detail = (process.stderr or process.stdout).decode("utf-8", "replace").strip()
        raise ChangeSetError("CHANGE_SET_VALIDATION_FAILED", detail or "Git operation failed")
    return process


def _workspace(change_set_or_workspace_id: str, *, by_change_set: bool = False) -> dict[str, Any]:
    if by_change_set:
        record = get(change_set_or_workspace_id)
        workspace_id = str(record["workspace_id"])
    else:
        workspace_id = change_set_or_workspace_id
    workspace = db.get_workspace(workspace_id)
    if workspace is None:
        raise ChangeSetError("WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}")
    path = Path(str(workspace["worktree_path"]))
    if not path.is_dir():
        raise ChangeSetError("STALE_WORKSPACE", "workspace directory is missing")
    workspace["path"] = path
    return workspace


def _ensure_supported(repository: Path) -> None:
    git_dir = _run_git(repository, ["rev-parse", "--git-dir"]).stdout.decode().strip()
    actual_git_dir = (
        (repository / git_dir).resolve()
        if not Path(git_dir).is_absolute()
        else Path(git_dir)
    )
    for marker in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-apply",
        "rebase-merge",
    ):
        if (actual_git_dir / marker).exists():
            raise ChangeSetError(
                "CHANGE_SET_INVALID_STATE",
                f"cannot begin during unfinished Git operation ({marker})",
            )
    for root, directories, files in os.walk(repository):
        directories[:] = [name for name in directories if name != ".git"]
        for name in [*directories, *files]:
            if (Path(root) / name).is_symlink():
                raise ChangeSetError(
                    "CHANGE_SET_LIMIT_EXCEEDED",
                    "symlinks are not supported in change sets",
                )


def _temporary_index(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"index-{uuid.uuid4().hex}"


def workspace_tree(repository: Path) -> str:
    """Hash current non-ignored worktree content without touching the real index."""
    index = _temporary_index(_root() / ".indexes")
    try:
        _run_git(repository, ["read-tree", "HEAD"], index_file=index)
        _run_git(
            repository,
            ["add", "-A", "--", "."],
            work_tree=repository,
            index_file=index,
        )
        return _run_git(repository, ["write-tree"], index_file=index).stdout.decode().strip()
    finally:
        index.unlink(missing_ok=True)


def _tree_from_directory(repository: Path, source: Path, base_tree: str, owner: Path) -> str:
    index = _temporary_index(owner)
    try:
        _run_git(repository, ["read-tree", base_tree], index_file=index)
        _run_git(repository, ["add", "-A", "--", "."], work_tree=source, index_file=index)
        # Git archives do not materialize submodule contents, and symlinks are
        # intentionally omitted from the staging filesystem. Preserve these
        # immutable entries from the captured before tree after ``git add -A``.
        for path, (mode, object_id) in _tree_special_entries(repository, base_tree).items():
            _run_git(
                repository,
                ["update-index", "--add", "--cacheinfo", f"{mode},{object_id},{path}"],
                index_file=index,
            )
        return _run_git(repository, ["write-tree"], index_file=index).stdout.decode().strip()
    finally:
        index.unlink(missing_ok=True)


def _materialize(repository: Path, tree: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path, (mode, object_id) in _tree_entries(repository, tree).items():
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ChangeSetError("CHANGE_SET_VALIDATION_FAILED", "unsafe Git tree entry")
        target = resolve_within(destination, path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_run_git(repository, ["cat-file", "blob", object_id]).stdout)
        if mode == "100755":
            os.chmod(target, target.stat().st_mode | 0o111)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload_hash(operation_type: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"operation_type": operation_type, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(canonical)


def _query_one(sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = db.connect().execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def get(change_set_id: str) -> dict[str, Any]:
    record = _query_one(
        "SELECT * FROM change_sets WHERE change_set_id = ?",
        (change_set_id,),
    )
    if record is None:
        raise ChangeSetError("CHANGE_SET_NOT_FOUND", f"change set not found: {change_set_id}")
    return record


def workspace_id_for(change_set_id: str) -> str | None:
    record = _query_one(
        "SELECT workspace_id FROM change_sets WHERE change_set_id = ?",
        (change_set_id,),
    )
    return str(record["workspace_id"]) if record else None


def _expire_if_needed(record: dict[str, Any]) -> dict[str, Any]:
    if record["status"] not in {"open", "validated"}:
        return record
    expires = datetime.fromisoformat(str(record["expires_at"]))
    if expires > datetime.now(timezone.utc):
        return record
    now = _now()
    connection = db.connect()
    connection.execute(
        """UPDATE change_sets SET status='expired', updated_at=?, closed_at=?
           WHERE change_set_id=? AND status IN ('open','validated')""",
        (now, now, record["change_set_id"]),
    )
    connection.commit()
    shutil.rmtree(_directory(str(record["change_set_id"])) / "staging", ignore_errors=True)
    record["status"] = "expired"
    raise ChangeSetError("CHANGE_SET_EXPIRED", "change set has expired")


def _active(record: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    record = _expire_if_needed(record)
    if record["status"] not in allowed:
        raise ChangeSetError(
            "CHANGE_SET_INVALID_STATE",
            f"change set is {record['status']}; expected one of {sorted(allowed)}",
            status=record["status"],
        )
    return record


def begin(workspace_id: str, explanation: str) -> dict[str, Any]:
    cfg = get_change_set_config()
    if not cfg["enabled"]:
        raise ChangeSetError("CHANGE_SET_INVALID_STATE", "change sets are disabled")
    if not explanation.strip():
        raise ChangeSetError("INVALID_INPUT", "explanation must be non-empty")
    workspace = _workspace(workspace_id)
    repository = Path(workspace["path"])
    with _lock(f"workspace:{workspace_id}"):
        _ensure_supported(repository)
        base_head = _run_git(repository, ["rev-parse", "HEAD"]).stdout.decode().strip()
        before_tree = workspace_tree(repository)
        entries = _tree_entries(repository, before_tree)
        object_ids = [object_id for _, object_id in entries.values()]
        total_bytes = 0
        if object_ids:
            sizes = _run_git(
                repository,
                ["cat-file", "--batch-check=%(objectsize)"],
                input_bytes=("\n".join(object_ids) + "\n").encode(),
            ).stdout
            total_bytes = sum(int(value) for value in sizes.splitlines())
        if total_bytes > int(cfg["max_staging_bytes"]):
            raise ChangeSetError(
                "CHANGE_SET_LIMIT_EXCEEDED",
                "workspace snapshot exceeds max_staging_bytes",
            )
        change_set_id = f"cs_{uuid.uuid4().hex}"
        directory = _directory(change_set_id)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            _materialize(repository, before_tree, directory / "staging")
            size = _directory_size(directory / "staging")
            if size > int(cfg["max_staging_bytes"]):
                raise ChangeSetError(
                    "CHANGE_SET_LIMIT_EXCEEDED",
                    "workspace snapshot exceeds max_staging_bytes",
                )
            now = _now()
            expires = (
                datetime.now(timezone.utc) + timedelta(hours=int(cfg["ttl_hours"]))
            ).isoformat()
            connection = db.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO change_sets
                       (change_set_id, workspace_id, explanation, status, revision,
                        base_head, before_tree_hash, created_at, updated_at, expires_at)
                       VALUES (?, ?, ?, 'open', 1, ?, ?, ?, ?, ?)""",
                    (
                        change_set_id,
                        workspace_id,
                        explanation,
                        base_head,
                        before_tree,
                        now,
                        now,
                        expires,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ChangeSetError(
                    "CHANGE_SET_ALREADY_ACTIVE",
                    "workspace already has an active change set",
                ) from exc
            manifest = {
                "change_set_id": change_set_id,
                "workspace_id": workspace_id,
                "base_head": base_head,
                "before_tree_hash": before_tree,
                "file_count": _file_count(directory / "staging"),
                "staging_bytes": size,
                "created_at": now,
            }
            _write_json(directory / "manifest.json", manifest)
            _write_json(directory / "journal.json", {"phase": "open", "updated_at": now})
            return get_summary(change_set_id)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def _tree_entries(repository: Path, tree: str) -> dict[str, tuple[str, str]]:
    raw = _run_git(repository, ["ls-tree", "-r", "-z", tree]).stdout
    entries: dict[str, tuple[str, str]] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        if kind == "commit" and mode == "160000":
            continue
        if kind == "blob" and mode == "120000":
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ChangeSetError(
                "CHANGE_SET_VALIDATION_FAILED",
                "change sets support regular files only",
            )
        entries[name.decode("utf-8")] = (mode, object_id)
    return entries


def _tree_special_entries(repository: Path, tree: str) -> dict[str, tuple[str, str]]:
    """Return immutable symlink and Gitlink entries from a captured tree."""
    raw = _run_git(repository, ["ls-tree", "-r", "-z", tree]).stdout
    entries: dict[str, tuple[str, str]] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        if (kind, mode) in {("blob", "120000"), ("commit", "160000")}:
            entries[name.decode("utf-8")] = (mode, object_id)
    return entries


def _reject_special_paths(change_set_id: str, paths: list[str]) -> None:
    """Reject an operation that addresses an immutable special-tree entry."""
    record = get(change_set_id)
    workspace = _workspace(change_set_id, by_change_set=True)
    special = _tree_special_entries(
        Path(workspace["path"]),
        str(record["before_tree_hash"]),
    )
    for raw_path in paths:
        normalized = PurePosixPath(raw_path.replace("\\", "/")).as_posix()
        for special_path in special:
            if normalized == special_path or normalized.startswith(f"{special_path}/"):
                raise ChangeSetError(
                    "PATH_DENIED",
                    f"change sets cannot edit symlink or submodule path: {raw_path}",
                )


def _blob_sha256(repository: Path, object_id: str) -> str:
    return _sha256(_run_git(repository, ["cat-file", "blob", object_id]).stdout)


def _manifest(repository: Path, before: str, after: str) -> list[dict[str, Any]]:
    old = _tree_entries(repository, before)
    new = _tree_entries(repository, after)
    files: list[dict[str, Any]] = []
    for path in sorted(set(old) | set(new)):
        if old.get(path) == new.get(path):
            continue
        before_entry = old.get(path)
        after_entry = new.get(path)
        change_type = "modified"
        if before_entry is None:
            change_type = "added"
        elif after_entry is None:
            change_type = "deleted"
        files.append(
            {
                "path": path,
                "change_type": change_type,
                "before_exists": before_entry is not None,
                "before_sha256": (
                    _blob_sha256(repository, before_entry[1]) if before_entry else None
                ),
                "before_mode": int(before_entry[0], 8) if before_entry else None,
                "after_exists": after_entry is not None,
                "after_sha256": (
                    _blob_sha256(repository, after_entry[1]) if after_entry else None
                ),
                "after_mode": int(after_entry[0], 8) if after_entry else None,
            }
        )
    return files


def _diff(repository: Path, before: str, after: str) -> bytes:
    patch = _run_git(repository, ["diff", "--binary", "--no-color", before, after, "--"]).stdout
    if b"GIT binary patch" in patch or b"\0" in patch:
        raise ChangeSetError(
            "CHANGE_SET_VALIDATION_FAILED",
            "binary files are not supported in change sets",
        )
    return patch


def _save_manifest(change_set_id: str, files: list[dict[str, Any]]) -> None:
    connection = db.connect()
    connection.execute("DELETE FROM change_set_files WHERE change_set_id=?", (change_set_id,))
    connection.executemany(
        """INSERT INTO change_set_files
           (change_set_id, path, change_type, before_exists, before_sha256,
            before_mode, after_exists, after_sha256, after_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                change_set_id,
                item["path"],
                item["change_type"],
                int(item["before_exists"]),
                item["before_sha256"],
                item["before_mode"],
                int(item["after_exists"]),
                item["after_sha256"],
                item["after_mode"],
            )
            for item in files
        ],
    )


def _validate_stored_operations(change_set_id: str, directory: Path) -> None:
    rows = db.connect().execute(
        """SELECT operation_type, input_sha256, payload_ref
           FROM change_set_operations WHERE change_set_id=? ORDER BY ordinal""",
        (change_set_id,),
    ).fetchall()
    for row in rows:
        payload_path = (directory / str(row["payload_ref"])).resolve()
        try:
            payload_path.relative_to(directory.resolve())
        except ValueError as exc:
            raise ChangeSetError(
                "CHANGE_SET_VALIDATION_FAILED",
                "operation payload path escapes its change-set directory",
            ) from exc
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChangeSetError(
                "CHANGE_SET_VALIDATION_FAILED",
                "operation payload is missing or unreadable",
            ) from exc
        if not isinstance(payload, dict) or _payload_hash(
            str(row["operation_type"]), payload
        ) != str(row["input_sha256"]):
            raise ChangeSetError(
                "CHANGE_SET_VALIDATION_FAILED",
                "operation payload hash does not match the database",
            )


def _assert_manifest_matches(change_set_id: str, files: list[dict[str, Any]]) -> None:
    stored = [
        {
            key: row[key]
            for key in (
                "path",
                "change_type",
                "before_exists",
                "before_sha256",
                "before_mode",
                "after_exists",
                "after_sha256",
                "after_mode",
            )
        }
        for row in db.connect()
        .execute(
            "SELECT * FROM change_set_files WHERE change_set_id=? ORDER BY path",
            (change_set_id,),
        )
        .fetchall()
    ]
    expected = []
    for item in files:
        normalized = dict(item)
        normalized["before_exists"] = int(normalized["before_exists"])
        normalized["after_exists"] = int(normalized["after_exists"])
        expected.append(normalized)
    if stored != expected:
        raise ChangeSetError(
            "CHANGE_SET_VALIDATION_FAILED",
            "staging manifest does not match the database",
        )


def _operation_replay(
    change_set_id: str, operation_id: str, input_sha256: str
) -> dict[str, Any] | None:
    record = _query_one(
        """SELECT input_sha256, result_json FROM change_set_operations
           WHERE change_set_id=? AND operation_id=?""",
        (change_set_id, operation_id),
    )
    if record is None:
        return None
    if record["input_sha256"] != input_sha256:
        raise ChangeSetError(
            "IDEMPOTENCY_CONFLICT",
            "operation_id was already used with different inputs",
        )
    parsed = json.loads(str(record["result_json"]))
    if not isinstance(parsed, dict):
        raise ChangeSetError("CHANGE_SET_VALIDATION_FAILED", "stored operation result is invalid")
    return dict(parsed)


def _stage(
    change_set_id: str,
    operation_type: str,
    payload: dict[str, Any],
    operation_id: str | None,
    apply: Any,
) -> dict[str, Any]:
    cfg = get_change_set_config()
    operation_id = operation_id or f"op_{uuid.uuid4().hex}"
    input_sha = _payload_hash(operation_type, payload)
    with _lock(f"change_set:{change_set_id}"):
        replay = _operation_replay(change_set_id, operation_id, input_sha)
        if replay is not None:
            replay["idempotent_replay"] = True
            return replay
        record = _active(get(change_set_id), {"open", "validated"})
        connection = db.connect()
        count = connection.execute(
            "SELECT COUNT(*) FROM change_set_operations WHERE change_set_id=?",
            (change_set_id,),
        ).fetchone()[0]
        if int(count) >= int(cfg["max_operations"]):
            raise ChangeSetError("CHANGE_SET_LIMIT_EXCEEDED", "max_operations exceeded")

        directory = _directory(change_set_id)
        staging = directory / "staging"
        checkpoint = directory / f".checkpoint-{uuid.uuid4().hex}"
        shutil.copytree(staging, checkpoint)
        was_validated = record["status"] == "validated"
        try:
            apply(staging)
            size = _directory_size(staging)
            if size > int(cfg["max_staging_bytes"]):
                raise ChangeSetError("CHANGE_SET_LIMIT_EXCEEDED", "max_staging_bytes exceeded")
            workspace = _workspace(change_set_id, by_change_set=True)
            after_tree = _tree_from_directory(
                Path(workspace["path"]),
                staging,
                str(record["before_tree_hash"]),
                directory,
            )
            files = _manifest(
                Path(workspace["path"]),
                str(record["before_tree_hash"]),
                after_tree,
            )
            if len(files) > int(cfg["max_changed_files"]):
                raise ChangeSetError("CHANGE_SET_LIMIT_EXCEEDED", "max_changed_files exceeded")
            staged_digest = _sha256(
                (
                    str(record["before_tree_hash"])
                    + after_tree
                    + input_sha
                    + str(int(record["revision"]) + 1)
                ).encode()
            )
            payload_path = directory / "operations" / f"{int(count) + 1:04d}.{operation_type}.json"
            _write_json(payload_path, payload)
            payload_ref = str(payload_path.relative_to(directory)).replace("\\", "/")
            now = _now()
            connection.execute("BEGIN IMMEDIATE")
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM change_set_operations "
                "WHERE change_set_id=?",
                (change_set_id,),
            ).fetchone()
            ordinal = int(ordinal_row[0])
            result = {
                "change_set_id": change_set_id,
                "workspace_id": record["workspace_id"],
                "operation_id": operation_id,
                "ordinal": ordinal,
                "operation_type": operation_type,
                "revision": int(record["revision"]) + 1,
                "changed_files": files,
                "staged_digest": staged_digest,
                "validation_invalidated": was_validated,
                "staging_bytes": size,
            }
            connection.execute(
                """INSERT INTO change_set_operations
                   (change_set_id, operation_id, ordinal, operation_type, input_sha256,
                    payload_ref, created_at, result_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    change_set_id,
                    operation_id,
                    ordinal,
                    operation_type,
                    input_sha,
                    payload_ref,
                    now,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            _save_manifest(change_set_id, files)
            connection.execute(
                """UPDATE change_sets
                   SET status='open', revision=revision+1, staged_digest=?,
                       validated_digest=NULL, after_tree_hash=NULL, validated_at=NULL,
                       updated_at=?, error_code=NULL, error_message=NULL
                   WHERE change_set_id=? AND status IN ('open','validated')""",
                (staged_digest, now, change_set_id),
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            shutil.rmtree(staging, ignore_errors=True)
            os.replace(checkpoint, staging)
            raise
        finally:
            shutil.rmtree(checkpoint, ignore_errors=True)


def stage_patch(
    change_set_id: str,
    patch: str,
    explanation: str,
    expected_sha256: dict[str, str] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    cfg = get_change_set_config()
    if not patch.strip():
        raise ChangeSetError("INVALID_INPUT", "patch must be non-empty")
    if len(patch) > int(cfg["max_patch_chars"]):
        raise ChangeSetError("CHANGE_SET_LIMIT_EXCEEDED", "max_patch_chars exceeded")
    stripped = patcher._strip_patch_wrappers(patch)
    diagnostic = patcher._validate_unified_diff(stripped)
    if diagnostic:
        raise ChangeSetError("INVALID_INPUT", "patch is not a valid unified diff", **diagnostic)
    try:
        patcher._validate_patch_paths(stripped)
    except ValueError as exc:
        raise ChangeSetError("PATH_DENIED", str(exc)) from exc
    changes = patcher._extract_file_changes(stripped)
    paths = [
        path
        for change in changes
        for path in (change.get("old_path"), change["path"])
        if path is not None
    ]
    _reject_special_paths(change_set_id, paths)
    if len(paths) != len(set(paths)):
        raise ChangeSetError("INVALID_INPUT", "patch contains a duplicate target path")
    payload = {
        "patch": stripped,
        "explanation": explanation,
        "expected_sha256": expected_sha256,
    }

    def apply(staging: Path) -> None:
        for path in paths:
            target = resolve_within(staging, path, must_exist=False)
            if is_denied(target, staging):
                raise ChangeSetError("PATH_DENIED", f"path is denied: {path}")
        for path, expected in (expected_sha256 or {}).items():
            target = resolve_within(staging, path, must_exist=False)
            actual = _sha256(target.read_bytes()) if target.is_file() else None
            if actual != expected:
                raise ChangeSetError(
                    "CHANGE_SET_CONFLICT",
                    f"staged file changed: {path}",
                    path=path,
                    expected_sha256=expected,
                    actual_sha256=actual,
                )
        check = _run_git(
            staging,
            ["apply", "--check", "--"],
            input_bytes=stripped.encode(),
            check=False,
        )
        if check.returncode:
            detail = (check.stderr or check.stdout).decode("utf-8", "replace").strip()
            raise ChangeSetError("CHANGE_SET_CONFLICT", detail or "patch does not apply")
        result = _run_git(staging, ["apply", "--"], input_bytes=stripped.encode(), check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            raise ChangeSetError("CHANGE_SET_CONFLICT", detail or "patch could not be staged")

    return _stage(change_set_id, "patch", payload, operation_id, apply)


def stage_replace(
    change_set_id: str,
    path: str,
    old_text: str,
    new_text: str,
    explanation: str,
    expected_sha256: str | None = None,
    replace_all: bool = False,
    operation_id: str | None = None,
) -> dict[str, Any]:
    if not old_text:
        raise ChangeSetError("INVALID_INPUT", "old_text must be non-empty")
    _reject_special_paths(change_set_id, [path])
    payload = {
        "path": path,
        "old_text": old_text,
        "new_text": new_text,
        "explanation": explanation,
        "expected_sha256": expected_sha256,
        "replace_all": replace_all,
    }

    def apply(staging: Path) -> None:
        try:
            target = resolve_within(staging, path)
        except ValueError as exc:
            raise ChangeSetError("PATH_DENIED", str(exc)) from exc
        if is_denied(target, staging):
            raise ChangeSetError("PATH_DENIED", "path is denied by policy")
        if not target.is_file() or target.is_symlink():
            raise ChangeSetError("INVALID_INPUT", "path is not a regular file")
        raw = target.read_bytes()
        actual = _sha256(raw)
        if expected_sha256 is not None and actual != expected_sha256:
            raise ChangeSetError(
                "CHANGE_SET_CONFLICT",
                f"staged file changed: {path}",
                expected_sha256=expected_sha256,
                actual_sha256=actual,
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChangeSetError("INVALID_INPUT", "file is not valid UTF-8") from exc
        occurrences = content.count(old_text)
        if not occurrences:
            raise ChangeSetError("CHANGE_SET_CONFLICT", "old_text was not found")
        if occurrences > 1 and not replace_all:
            raise ChangeSetError(
                "CHANGE_SET_CONFLICT",
                "old_text is ambiguous; set replace_all=true",
                occurrences=occurrences,
            )
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(updated, encoding="utf-8", newline="")
            os.chmod(temporary, target.stat().st_mode)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    return _stage(change_set_id, "replace", payload, operation_id, apply)


def validate(
    change_set_id: str,
    expected_revision: int,
    validation_profile: str = "default",
) -> dict[str, Any]:
    with _lock(f"change_set:{change_set_id}"):
        record = _active(get(change_set_id), {"open", "validated"})
        if int(record["revision"]) != expected_revision:
            raise ChangeSetError(
                "CHANGE_SET_CONFLICT",
                "change set revision changed",
                expected_revision=expected_revision,
                actual_revision=record["revision"],
            )
        if validation_profile != "default":
            raise ChangeSetError(
                "CHANGE_SET_VALIDATION_FAILED",
                "only the default validation profile is supported",
            )
        directory = _directory(change_set_id)
        workspace = _workspace(change_set_id, by_change_set=True)
        repository = Path(workspace["path"])
        try:
            _validate_stored_operations(change_set_id, directory)
            staging_bytes = _directory_size(directory / "staging")
            if staging_bytes > int(get_change_set_config()["max_staging_bytes"]):
                raise ChangeSetError(
                    "CHANGE_SET_LIMIT_EXCEEDED",
                    "max_staging_bytes exceeded",
                )
            after_tree = _tree_from_directory(
                repository,
                directory / "staging",
                str(record["before_tree_hash"]),
                directory,
            )
            files = _manifest(repository, str(record["before_tree_hash"]), after_tree)
            _assert_manifest_matches(change_set_id, files)
            cfg = get_change_set_config()
            if len(files) > int(cfg["max_changed_files"]):
                raise ChangeSetError("CHANGE_SET_LIMIT_EXCEEDED", "max_changed_files exceeded")
            final_patch = _diff(repository, str(record["before_tree_hash"]), after_tree)
            clean_before = directory / f".validation-{uuid.uuid4().hex}"
            try:
                _materialize(repository, str(record["before_tree_hash"]), clean_before)
                check = _run_git(
                    clean_before,
                    ["apply", "--check", "--"],
                    input_bytes=final_patch,
                    check=False,
                )
                if check.returncode:
                    detail = (check.stderr or check.stdout).decode("utf-8", "replace").strip()
                    raise ChangeSetError(
                        "CHANGE_SET_VALIDATION_FAILED",
                        detail or "final patch validation failed",
                    )
            finally:
                shutil.rmtree(clean_before, ignore_errors=True)
            operations = db.connect().execute(
                """SELECT input_sha256 FROM change_set_operations
                   WHERE change_set_id=? ORDER BY ordinal""",
                (change_set_id,),
            ).fetchall()
            digest_input = "".join(
                [
                    change_set_id,
                    str(record["revision"]),
                    str(record["before_tree_hash"]),
                    after_tree,
                    *(str(item["input_sha256"]) for item in operations),
                    "default:v1",
                ]
            )
            validated_digest = _sha256(digest_input.encode())
            (directory / "final.patch").write_bytes(final_patch)
            now = _now()
            connection = db.connect()
            connection.execute("BEGIN IMMEDIATE")
            _save_manifest(change_set_id, files)
            connection.execute(
                """UPDATE change_sets
                   SET status='validated', validated_digest=?, after_tree_hash=?,
                       validated_at=?, updated_at=?, error_code=NULL, error_message=NULL
                   WHERE change_set_id=? AND revision=?""",
                (
                    validated_digest,
                    after_tree,
                    now,
                    now,
                    change_set_id,
                    expected_revision,
                ),
            )
            connection.commit()
            warning = workspace_tree(repository) != record["before_tree_hash"]
            preview = final_patch.decode("utf-8", "replace")
            return {
                "change_set_id": change_set_id,
                "workspace_id": record["workspace_id"],
                "status": "validated",
                "revision": record["revision"],
                "before_tree_hash": record["before_tree_hash"],
                "after_tree_hash": after_tree,
                "validated_digest": validated_digest,
                "changed_files": files,
                "validators": [
                    {"name": "structure", "status": "passed"},
                    {"name": "git_apply_check", "status": "passed"},
                ],
                "diff_stat": _diff_stat(files),
                "diff_preview": preview[:20_000],
                "diff_truncated": len(preview) > 20_000,
                "warnings": (
                    ["workspace changed since this change set began"] if warning else []
                ),
            }
        except ChangeSetError as exc:
            connection = db.connect()
            connection.execute(
                """UPDATE change_sets SET status='open', validated_digest=NULL,
                   after_tree_hash=NULL, validated_at=NULL, updated_at=?,
                   error_code=?, error_message=? WHERE change_set_id=?""",
                (_now(), exc.code, str(exc)[:1000], change_set_id),
            )
            connection.commit()
            raise


def _diff_stat(files: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "files": len(files),
        "added": sum(item["change_type"] == "added" for item in files),
        "modified": sum(item["change_type"] == "modified" for item in files),
        "deleted": sum(item["change_type"] == "deleted" for item in files),
    }


def _journal(change_set_id: str, phase: str, **extra: Any) -> None:
    _write_json(
        _directory(change_set_id) / "journal.json",
        {"phase": phase, "updated_at": _now(), **extra},
    )
    connection = db.connect()
    connection.execute(
        "UPDATE change_sets SET commit_phase=?, updated_at=? WHERE change_set_id=?",
        (phase, _now(), change_set_id),
    )
    connection.commit()


def _snapshot_rollback(workspace: Path, directory: Path, files: list[dict[str, Any]]) -> None:
    rollback = directory / "rollback"
    rollback.mkdir(exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for item in files:
        target = resolve_within(workspace, item["path"], must_exist=False)
        entry = {
            "path": item["path"],
            "exists": target.is_file(),
            "mode": target.stat().st_mode if target.is_file() else None,
        }
        if target.is_file():
            destination = resolve_within(rollback, item["path"], must_exist=False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, destination)
            entry["sha256"] = _sha256(destination.read_bytes())
        manifest.append(entry)
    _write_json(rollback / "manifest.json", manifest)


def _restore(workspace: Path, directory: Path) -> None:
    rollback = directory / "rollback"
    manifest = json.loads((rollback / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        target = resolve_within(workspace, item["path"], must_exist=False)
        if is_denied(target, workspace):
            raise ChangeSetError(
                "CHANGE_SET_RECOVERY_REQUIRED",
                "rollback manifest contains a denied path",
            )
        if item["exists"]:
            source = resolve_within(rollback, item["path"])
            if _sha256(source.read_bytes()) != item["sha256"]:
                raise ChangeSetError(
                    "CHANGE_SET_RECOVERY_REQUIRED",
                    "rollback snapshot hash mismatch",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            os.chmod(target, int(item["mode"]))
        elif target.exists():
            if not target.is_file() or target.is_symlink():
                raise ChangeSetError(
                    "CHANGE_SET_RECOVERY_REQUIRED",
                    "rollback target is not a regular file",
                )
            target.unlink()


def commit(
    change_set_id: str,
    validated_digest: str,
    expected_workspace_tree: str | None = None,
) -> dict[str, Any]:
    initial = get(change_set_id)
    workspace_id = str(initial["workspace_id"])
    with _lock(f"workspace:{workspace_id}"):
        record = get(change_set_id)
        if record["status"] == "committed":
            if record["validated_digest"] != validated_digest:
                raise ChangeSetError("IDEMPOTENCY_CONFLICT", "validated digest differs")
            return get_summary(change_set_id) | {"idempotent_replay": True}
        if record["status"] != "validated":
            code = (
                "CHANGE_SET_NOT_VALIDATED"
                if record["status"] == "open"
                else "CHANGE_SET_INVALID_STATE"
            )
            raise ChangeSetError(code, f"change set is {record['status']}")
        if record["validated_digest"] != validated_digest or not record["after_tree_hash"]:
            raise ChangeSetError("CHANGE_SET_NOT_VALIDATED", "validated digest is stale or invalid")
        if expected_workspace_tree and expected_workspace_tree != record["before_tree_hash"]:
            raise ChangeSetError(
                "CHANGE_SET_CONFLICT",
                "expected_workspace_tree does not match the captured before tree",
            )
        workspace = _workspace(workspace_id)
        repository = Path(workspace["path"])
        actual_tree = workspace_tree(repository)
        if actual_tree != record["before_tree_hash"]:
            raise ChangeSetError(
                "CHANGE_SET_CONFLICT",
                "workspace changed since this change set began",
                expected_tree=record["before_tree_hash"],
                actual_tree=actual_tree,
            )
        directory = _directory(change_set_id)
        files = [
            dict(row)
            for row in db.connect()
            .execute(
                "SELECT * FROM change_set_files WHERE change_set_id=? ORDER BY path",
                (change_set_id,),
            )
            .fetchall()
        ]
        _snapshot_rollback(repository, directory, files)
        connection = db.connect()
        connection.execute(
            "UPDATE change_sets SET status='committing', updated_at=? WHERE change_set_id=?",
            (_now(), change_set_id),
        )
        connection.commit()
        _journal(change_set_id, "commit_intent")
        patch = (directory / "final.patch").read_bytes()
        try:
            check = _run_git(
                repository,
                ["apply", "--check", "--"],
                input_bytes=patch,
                check=False,
            )
            if check.returncode:
                detail = (check.stderr or check.stdout).decode("utf-8", "replace").strip()
                raise ChangeSetError(
                    "CHANGE_SET_CONFLICT",
                    detail or "final patch no longer applies",
                )
            _journal(change_set_id, "applying")
            applied = _run_git(
                repository,
                ["apply", "--"],
                input_bytes=patch,
                check=False,
            )
            if applied.returncode:
                detail = (applied.stderr or applied.stdout).decode("utf-8", "replace").strip()
                raise ChangeSetError("CHANGE_SET_CONFLICT", detail or "final patch failed")
            actual_after = workspace_tree(repository)
            if actual_after != record["after_tree_hash"]:
                raise ChangeSetError(
                    "CHANGE_SET_VALIDATION_FAILED",
                    "workspace after tree does not match validated tree",
                )
            _journal(change_set_id, "files_applied")
            now = _now()
            connection = db.connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE change_sets SET status='committed', commit_phase='complete',
                   committed_at=?, closed_at=?, updated_at=?
                   WHERE change_set_id=? AND status='committing'""",
                (now, now, now, change_set_id),
            )
            connection.execute(
                "UPDATE workspaces SET revision=revision+1, last_patch_at=? WHERE workspace_id=?",
                (now, workspace_id),
            )
            connection.commit()
            _journal(change_set_id, "complete")
            return get_summary(change_set_id)
        except Exception as original:
            try:
                _restore(repository, directory)
                if workspace_tree(repository) != record["before_tree_hash"]:
                    raise ChangeSetError(
                        "CHANGE_SET_RECOVERY_REQUIRED",
                        "rollback did not restore the before tree",
                    )
            except Exception as recovery:
                connection = db.connect()
                connection.execute(
                    """UPDATE change_sets SET status='recovery_required',
                       error_code='CHANGE_SET_RECOVERY_REQUIRED', error_message=?,
                       updated_at=? WHERE change_set_id=?""",
                    (str(recovery)[:1000], _now(), change_set_id),
                )
                connection.commit()
                _journal(change_set_id, "recovery_required")
                raise ChangeSetError(
                    "CHANGE_SET_RECOVERY_REQUIRED",
                    "commit failed and rollback could not be proven",
                ) from recovery
            now = _now()
            connection = db.connect()
            connection.execute(
                """UPDATE change_sets SET status='rolled_back', closed_at=?,
                   updated_at=?, error_code=?, error_message=?
                   WHERE change_set_id=?""",
                (
                    now,
                    now,
                    getattr(original, "code", "INTERNAL_ERROR"),
                    str(original)[:1000],
                    change_set_id,
                ),
            )
            connection.commit()
            _journal(change_set_id, "rolled_back")
            if isinstance(original, ChangeSetError):
                original.details["rollback_performed"] = True
                raise original
            raise ChangeSetError(
                "INTERNAL_ERROR",
                str(original),
                rollback_performed=True,
            ) from original


def rollback(change_set_id: str) -> dict[str, Any]:
    with _lock(f"change_set:{change_set_id}"):
        record = get(change_set_id)
        if record["status"] == "rolled_back":
            return get_summary(change_set_id) | {"idempotent_replay": True}
        if record["status"] == "committed":
            raise ChangeSetError(
                "CHANGE_SET_ALREADY_COMMITTED",
                "a committed change set cannot be rolled back; create an inverse change set",
            )
        if record["status"] in {"committing", "recovery_required"}:
            raise ChangeSetError(
                "CHANGE_SET_RECOVERY_REQUIRED",
                "change set requires recovery reconciliation",
            )
        if record["status"] == "expired":
            raise ChangeSetError("CHANGE_SET_EXPIRED", "change set has expired")
        now = _now()
        connection = db.connect()
        connection.execute(
            """UPDATE change_sets SET status='rolled_back', closed_at=?, updated_at=?,
               validated_digest=NULL, after_tree_hash=NULL WHERE change_set_id=?""",
            (now, now, change_set_id),
        )
        connection.commit()
        shutil.rmtree(_directory(change_set_id) / "staging", ignore_errors=True)
        _journal(change_set_id, "rolled_back")
        return get_summary(change_set_id)


def get_summary(change_set_id: str) -> dict[str, Any]:
    record = get(change_set_id)
    operations = [
        {
            "operation_id": row["operation_id"],
            "ordinal": row["ordinal"],
            "operation_type": row["operation_type"],
            "input_sha256": row["input_sha256"],
            "created_at": row["created_at"],
        }
        for row in db.connect()
        .execute(
            """SELECT operation_id, ordinal, operation_type, input_sha256, created_at
               FROM change_set_operations WHERE change_set_id=? ORDER BY ordinal""",
            (change_set_id,),
        )
        .fetchall()
    ]
    files = [
        dict(row)
        for row in db.connect()
        .execute(
            "SELECT * FROM change_set_files WHERE change_set_id=? ORDER BY path",
            (change_set_id,),
        )
        .fetchall()
    ]
    for item in files:
        item.pop("change_set_id", None)
        item["before_exists"] = bool(item["before_exists"])
        item["after_exists"] = bool(item["after_exists"])
    preview = ""
    final_patch = _directory(change_set_id) / "final.patch"
    if final_patch.is_file():
        preview = final_patch.read_text(encoding="utf-8", errors="replace")[:20_000]
    public = {
        key: record[key]
        for key in (
            "change_set_id",
            "workspace_id",
            "explanation",
            "status",
            "revision",
            "base_head",
            "before_tree_hash",
            "staged_digest",
            "validated_digest",
            "after_tree_hash",
            "commit_phase",
            "error_code",
            "error_message",
            "created_at",
            "updated_at",
            "validated_at",
            "committed_at",
            "closed_at",
            "expires_at",
        )
    }
    public.update(
        {
            "operations": operations,
            "changed_files": files,
            "diff_stat": _diff_stat(files),
            "diff_preview": preview,
            "diff_truncated": final_patch.is_file() and final_patch.stat().st_size > 20_000,
        }
    )
    return public


def reconcile_incomplete() -> dict[str, int]:
    """Reconcile crash-interrupted commits without ever replaying a final patch."""
    counts = {"committed": 0, "rolled_back": 0, "recovery_required": 0}
    rows = db.connect().execute(
        "SELECT change_set_id, workspace_id, before_tree_hash, after_tree_hash "
        "FROM change_sets WHERE status='committing'"
    ).fetchall()
    for row in rows:
        change_set_id = str(row["change_set_id"])
        with _lock(f"workspace:{row['workspace_id']}"):
            try:
                workspace = _workspace(str(row["workspace_id"]))
                current = workspace_tree(Path(workspace["path"]))
                if current == row["after_tree_hash"]:
                    status = "committed"
                elif current == row["before_tree_hash"]:
                    status = "rolled_back"
                else:
                    try:
                        _restore(Path(workspace["path"]), _directory(change_set_id))
                        if workspace_tree(Path(workspace["path"])) != row["before_tree_hash"]:
                            raise RuntimeError("before tree was not restored")
                        status = "rolled_back"
                    except Exception:
                        status = "recovery_required"
                now = _now()
                db.connect().execute(
                    """UPDATE change_sets SET status=?, commit_phase=?, updated_at=?,
                       closed_at=CASE WHEN ? != 'recovery_required' THEN ? ELSE closed_at END
                       WHERE change_set_id=?""",
                    (status, status, now, status, now, change_set_id),
                )
                db.connect().commit()
                _journal(change_set_id, status)
                counts[status] += 1
            except Exception:
                db.connect().execute(
                    """UPDATE change_sets SET status='recovery_required',
                       commit_phase='recovery_required', updated_at=? WHERE change_set_id=?""",
                    (_now(), change_set_id),
                )
                db.connect().commit()
                counts["recovery_required"] += 1
    return counts


def cleanup_expired() -> dict[str, int]:
    """Expire abandoned staging trees and prune old terminal payloads."""
    cfg = get_change_set_config()
    now = datetime.now(timezone.utc)
    expired = 0
    cleaned = 0
    connection = db.connect()
    rows = connection.execute(
        """SELECT change_set_id, status, expires_at, closed_at
           FROM change_sets
           WHERE status IN ('open','validated','committed','rolled_back','expired')"""
    ).fetchall()
    terminal_cutoff = now - timedelta(hours=int(cfg["retain_terminal_hours"]))
    for row in rows:
        change_set_id = str(row["change_set_id"])
        status = str(row["status"])
        if status in {"open", "validated"}:
            if datetime.fromisoformat(str(row["expires_at"])) > now:
                continue
            timestamp = _now()
            connection.execute(
                """UPDATE change_sets SET status='expired', closed_at=?, updated_at=?
                   WHERE change_set_id=? AND status IN ('open','validated')""",
                (timestamp, timestamp, change_set_id),
            )
            connection.commit()
            shutil.rmtree(_directory(change_set_id) / "staging", ignore_errors=True)
            expired += 1
            continue
        closed_at = row["closed_at"]
        if closed_at and datetime.fromisoformat(str(closed_at)) <= terminal_cutoff:
            shutil.rmtree(_directory(change_set_id), ignore_errors=True)
            cleaned += 1
    return {"expired": expired, "cleaned": cleaned}


def assert_workspace_discardable(workspace_id: str) -> None:
    """Reject workspace deletion while a recoverable transaction is active."""
    row = db.connect().execute(
        """SELECT change_set_id, status FROM change_sets
           WHERE workspace_id=? AND status IN
             ('open','validated','committing','recovery_required')
           LIMIT 1""",
        (workspace_id,),
    ).fetchone()
    if row is not None:
        raise ChangeSetError(
            "CHANGE_SET_INVALID_STATE",
            f"workspace has an active change set ({row['status']}): {row['change_set_id']}",
        )


def delete_terminal_data(workspace_id: str) -> None:
    """Remove terminal payload directories before deleting workspace DB rows."""
    rows = db.connect().execute(
        """SELECT change_set_id FROM change_sets
           WHERE workspace_id=? AND status IN ('committed','rolled_back','expired')""",
        (workspace_id,),
    ).fetchall()
    for row in rows:
        shutil.rmtree(_directory(str(row["change_set_id"])), ignore_errors=True)
