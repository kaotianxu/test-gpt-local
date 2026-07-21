"""Cross-suite isolation fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.storage import database as db


@pytest.fixture(autouse=True)
def isolated_default_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Isolate legacy module-level database consumers for every test."""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    db._local.conn = None
    db._local.db_path = None

    database_dir: Path = tmp_path_factory.mktemp("default-database")
    monkeypatch.setattr(db, "_DB_PATH", database_dir / "operator.db")
    db.init_db()
    yield

    connection = getattr(db._local, "conn", None)
    if connection is not None:
        connection.close()
    db._local.conn = None
    db._local.db_path = None
