"""Unit tests for the artifact registry service and tools.

Covers the acceptance criteria from the iteration plan (section 6),
including artifact registration, listings, text reading, image viewing,
staleness detection, and auto-discovery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services import artifact_registry as registry
from app.storage import database as db
from app.tools.artifacts import _list_artifacts, _read_artifact

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _artifact_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give each test a fresh database with the artifacts table."""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        db._local.conn = None
    # Use a subdirectory so git-using tests in the same tmp_path aren't
    # affected by the database file.
    db_dir = tmp_path / ".artifact_db"
    db_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "_DB_PATH", db_dir / "operator.db")
    db.init_db()
    yield
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_workspace_db(workspace_id: str, worktree: Path) -> None:
    """Insert a workspace record into the test database."""
    from app.storage.database import _now_iso

    conn = db._get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO workspaces
           (workspace_id, project_id, task_name, worktree_path, base_commit,
            status, created_at, last_accessed_at, revision, current_head)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 1, ?)""",
        (
            workspace_id,
            "test-project",
            "artifact-test",
            str(worktree),
            "abc123",
            _now_iso(),
            _now_iso(),
            "abc123",
        ),
    )
    conn.commit()


def _create_text_file(path: Path, content: str = "hello world") -> Path:
    """Create a text file and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _create_image_file(path: Path) -> Path:
    """Create a minimal PNG file and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image as PILImage

    img = PILImage.new("RGBA", (4, 4), (255, 0, 0, 255))
    img.save(str(path), format="PNG")
    return path


# ---------------------------------------------------------------------------
# Artifact Registration
# ---------------------------------------------------------------------------


class TestRegisterArtifact:
    def test_register_and_retrieve(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000001", tmp_path)
        file = _create_text_file(tmp_path / "report.txt", "test content")

        record = registry.register_artifact(
            workspace_id="ws-00000001",
            path=str(file),
            mime_type="text/plain",
            kind="text",
            source_type="scan",
        )

        assert record["workspace_id"] == "ws-00000001"
        assert record["kind"] == "text"
        assert record["mime_type"] == "text/plain"
        assert record["path"] == str(file)
        assert record["size_bytes"] > 0
        assert record["sha256"] is not None
        assert record["artifact_id"].startswith("artifact_")

        # Retrieve by ID.
        retrieved = registry.get_artifact(record["artifact_id"])
        assert retrieved is not None
        assert retrieved["artifact_id"] == record["artifact_id"]

    def test_register_without_size_auto_detects(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000002", tmp_path)
        file = _create_text_file(tmp_path / "data.txt", "x" * 1000)

        record = registry.register_artifact(
            workspace_id="ws-00000002",
            path=str(file),
            mime_type="text/plain",
        )

        # Size should be auto-detected from the file.
        assert record["size_bytes"] == 1000

    def test_register_with_metadata(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000003", tmp_path)
        file = _create_text_file(tmp_path / "meta.txt")

        record = registry.register_artifact(
            workspace_id="ws-00000003",
            path=str(file),
            kind="text",
            metadata={"source": "test", "version": 1},
        )

        retrieved = registry.get_artifact(record["artifact_id"])
        assert retrieved is not None
        # metadata is stored as JSON string.
        if retrieved.get("metadata"):
            meta = json.loads(retrieved["metadata"])
            assert meta["source"] == "test"


# ---------------------------------------------------------------------------
# Artifact Listing
# ---------------------------------------------------------------------------


class TestListArtifacts:
    def test_list_empty(self) -> None:
        artifacts = registry.list_artifacts("ws-00000000")
        assert artifacts == []

    def test_list_by_kind(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000010", tmp_path)
        t1 = _create_text_file(tmp_path / "a.txt")
        t2 = _create_text_file(tmp_path / "b.txt")
        img = _create_image_file(tmp_path / "img.png")

        registry.register_artifact("ws-00000010", str(t1), kind="text")
        registry.register_artifact("ws-00000010", str(t2), kind="text")
        registry.register_artifact("ws-00000010", str(img), kind="image")

        all_artifacts = registry.list_artifacts("ws-00000010")
        assert len(all_artifacts) == 3

        text_artifacts = registry.list_artifacts("ws-00000010", kind="text")
        assert len(text_artifacts) == 2

        image_artifacts = registry.list_artifacts("ws-00000010", kind="image")
        assert len(image_artifacts) == 1

    def test_list_by_path_prefix(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000011", tmp_path)
        r1 = _create_text_file(tmp_path / "reports" / "a.html")
        r2 = _create_text_file(tmp_path / "reports" / "b.html")
        o = _create_text_file(tmp_path / "other.txt")

        registry.register_artifact("ws-00000011", str(r1), kind="html")
        registry.register_artifact("ws-00000011", str(r2), kind="html")
        registry.register_artifact("ws-00000011", str(o), kind="text")

        reports = registry.list_artifacts("ws-00000011", path_prefix=str(tmp_path / "reports"))
        assert len(reports) == 2

    def test_list_artifact_status(self, tmp_path: Path) -> None:
        """Artifacts should report stale when the file is deleted."""
        _create_workspace_db("ws-00000012", tmp_path)
        file = _create_text_file(tmp_path / "temp.txt")
        registry.register_artifact("ws-00000012", str(file), kind="text")

        # File exists -> available.
        artifacts = registry.list_artifacts("ws-00000012")
        assert artifacts[0]["status"] == "available"

        # Delete file -> stale.
        file.unlink()
        artifacts = registry.list_artifacts("ws-00000012")
        assert artifacts[0]["status"] == "stale"


# ---------------------------------------------------------------------------
# Artifact Deletion
# ---------------------------------------------------------------------------


class TestDeleteArtifact:
    def test_delete_single(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000020", tmp_path)
        file = _create_text_file(tmp_path / "del.txt")
        record = registry.register_artifact("ws-00000020", str(file), kind="text")

        assert registry.get_artifact(record["artifact_id"]) is not None
        assert registry.delete_artifact(record["artifact_id"]) is True
        assert registry.get_artifact(record["artifact_id"]) is None

    def test_delete_for_workspace(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000021", tmp_path)
        t1 = _create_text_file(tmp_path / "a.txt")
        t2 = _create_text_file(tmp_path / "b.txt")
        registry.register_artifact("ws-00000021", str(t1), kind="text")
        registry.register_artifact("ws-00000021", str(t2), kind="text")

        assert registry.delete_artifacts_for_workspace("ws-00000021") == 2
        assert registry.list_artifacts("ws-00000021") == []


# ---------------------------------------------------------------------------
# Artifact Counting
# ---------------------------------------------------------------------------


class TestCountArtifacts:
    def test_count(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000030", tmp_path)
        t1 = _create_text_file(tmp_path / "a.txt")
        t2 = _create_text_file(tmp_path / "b.txt")
        img = _create_image_file(tmp_path / "img.png")

        registry.register_artifact("ws-00000030", str(t1), kind="text")
        registry.register_artifact("ws-00000030", str(t2), kind="text")
        registry.register_artifact("ws-00000030", str(img), kind="image")

        count = registry.count_artifacts("ws-00000030")
        assert count["count"] == 3
        assert count["kinds"]["text"] == 2
        assert count["kinds"]["image"] == 1


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


class TestDiscoverArtifacts:
    def test_discover_png(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000040", tmp_path)
        _create_image_file(tmp_path / "screenshot.png")
        _create_text_file(tmp_path / "readme.md")

        discovered = registry.discover_artifacts(tmp_path, "ws-00000040")
        # Should find at least the PNG.
        pngs = [a for a in discovered if a["kind"] == "image"]
        assert len(pngs) >= 1

    def test_discover_skips_excluded_dirs(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000041", tmp_path)
        _create_image_file(tmp_path / ".git" / "secret.png")
        _create_image_file(tmp_path / "node_modules" / "dep.png")
        _create_image_file(tmp_path / "legit.png")

        discovered = registry.discover_artifacts(tmp_path, "ws-00000041")
        # Should find only legit.png, not the ones in excluded dirs.
        assert len(discovered) == 1

    def test_discover_skips_already_registered(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000042", tmp_path)
        file = _create_image_file(tmp_path / "already.png")

        # Manually register it with a relative path (as discovery would).
        registry.register_artifact(
            "ws-00000042",
            path="already.png",
            kind="image",
            size_bytes=file.stat().st_size,
            sha256=registry._compute_sha256(file),
        )

        # Discovery should not re-register it.
        discovered = registry.discover_artifacts(tmp_path, "ws-00000042")
        already = [a for a in discovered if "already.png" in a["path"]]
        assert len(already) == 0


# ---------------------------------------------------------------------------
# list_artifacts tool
# ---------------------------------------------------------------------------


class TestListArtifactsTool:
    def test_unknown_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.tools.artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "get_workspace", lambda wid: None)
        result = _list_artifacts("ws-00000000")
        assert result["ok"] is False
        assert result["error"]["code"] == "WORKSPACE_NOT_FOUND"

    def test_invalid_kind(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def fake_gw(wid: str) -> dict[str, Any] | None:
            return {
                "workspace_id": wid,
                "project_id": "p",
                "worktree_path": str(tmp_path),
                "status": "active",
            }

        import app.tools.artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "get_workspace", fake_gw)
        result = _list_artifacts("ws-00000001", kind="invalid")
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# read_artifact tool
# ---------------------------------------------------------------------------


class TestReadArtifact:
    def test_read_text_artifact(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000050", tmp_path)
        file = _create_text_file(tmp_path / "data.txt", "hello world\nline 2\nline 3")
        record = registry.register_artifact("ws-00000050", str(file), kind="text")

        result = _read_artifact(record["artifact_id"])
        assert result["ok"] is True
        assert result["result"]["content"] == "hello world\nline 2\nline 3"
        assert result["result"]["total_chars"] == 25

    def test_read_with_offset(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000051", tmp_path)
        file = _create_text_file(tmp_path / "data.txt", "hello world\nline 2\nline 3")
        record = registry.register_artifact("ws-00000051", str(file), kind="text")

        result = _read_artifact(record["artifact_id"], offset=12)
        assert result["result"]["content"] == "line 2\nline 3"
        assert result["result"]["offset"] == 12

    def test_read_nonexistent_artifact(self) -> None:
        result = _read_artifact("artifact_nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "ARTIFACT_NOT_FOUND"

    def test_read_image_artifact_returns_error(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000052", tmp_path)
        file = _create_image_file(tmp_path / "img.png")
        record = registry.register_artifact("ws-00000052", str(file), kind="image")

        result = _read_artifact(record["artifact_id"])
        assert result["ok"] is False
        assert result["error"]["code"] == "ARTIFACT_IS_IMAGE"

    def test_read_stale_artifact(self, tmp_path: Path) -> None:
        _create_workspace_db("ws-00000053", tmp_path)
        file = _create_text_file(tmp_path / "temp.txt", "hello")
        record = registry.register_artifact("ws-00000053", str(file), kind="text")
        file.unlink()

        result = _read_artifact(record["artifact_id"])
        assert result["ok"] is False
        assert result["error"]["code"] == "ARTIFACT_STALE"


# ---------------------------------------------------------------------------
# Kind detection
# ---------------------------------------------------------------------------


class TestDetectKind:
    def test_from_mime_type(self) -> None:
        assert registry._detect_kind("image/png", "file.xyz") == "image"
        assert registry._detect_kind("text/html", "file.xyz") == "html"
        assert registry._detect_kind("application/json", "file.xyz") == "json"
        assert registry._detect_kind("text/plain", "file.xyz") == "text"

    def test_from_extension(self) -> None:
        assert registry._detect_kind(None, "file.png") == "image"
        assert registry._detect_kind(None, "file.html") == "html"
        assert registry._detect_kind(None, "file.json") == "json"
        assert registry._detect_kind(None, "file.txt") == "text"
        assert registry._detect_kind(None, "file.unknown") == "unknown"
