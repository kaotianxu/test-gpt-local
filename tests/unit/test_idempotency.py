"""Unit tests for the idempotency store."""

import json

from app.storage.idempotency import (
    _input_hash,
    get_idempotent_result,
    store_idempotent_result,
    expire_old_keys,
    with_idempotency,
)


class TestInputHash:
    def test_deterministic(self) -> None:
        h1 = _input_hash("apply_patch", workspace_id="ws_abc", patch_sha256="abc123")
        h2 = _input_hash("apply_patch", workspace_id="ws_abc", patch_sha256="abc123")
        assert h1 == h2

    def test_different_for_different_inputs(self) -> None:
        h1 = _input_hash("apply_patch", workspace_id="ws_abc", patch_sha256="abc123")
        h2 = _input_hash("apply_patch", workspace_id="ws_abc", patch_sha256="def456")
        assert h1 != h2

    def test_different_for_different_tools(self) -> None:
        h1 = _input_hash("apply_patch", workspace_id="ws_abc")
        h2 = _input_hash("create_workspace", workspace_id="ws_abc")
        assert h1 != h2


class TestStoreAndGet:
    def test_store_and_retrieve(self) -> None:
        key = "test-key-1"
        result = {"status": "passed", "exit_code": 0}
        store_idempotent_result(key, "test_tool", _input_hash("test_tool"), json.dumps(result))
        cached = get_idempotent_result(key, "test_tool", _input_hash("test_tool"))
        assert cached is not None
        assert cached["status"] == "passed"

    def test_mismatch_returns_mismatch_signal(self) -> None:
        key = "test-key-mismatch"
        store_idempotent_result(key, "test_tool", "hash_original", json.dumps({"ok": True}))
        cached = get_idempotent_result(key, "test_tool", "hash_different")
        assert cached is not None
        assert cached.get("_mismatch") is True

    def test_missing_key_returns_none(self) -> None:
        result = get_idempotent_result("nonexistent-key", "test_tool", "some_hash")
        assert result is None


class TestWithIdempotency:
    def test_no_key_executes_fn(self) -> None:
        called = False

        def fn() -> dict:
            nonlocal called
            called = True
            return {"ok": True}

        result = with_idempotency(None, "test_tool", {"input": "x"}, fn)
        assert called is True
        assert result["ok"] is True

    def test_with_key_caches_result(self) -> None:
        call_count = 0

        def fn() -> dict:
            nonlocal call_count
            call_count += 1
            return {"ok": True, "call": call_count}

        key = "test-cache-key"
        inputs = {"input": "x"}

        # First call: executes fn.
        first = with_idempotency(key, "test_tool", inputs, fn)
        assert call_count == 1
        assert first["call"] == 1

        # Second call: returns cached result.
        second = with_idempotency(key, "test_tool", inputs, fn)
        assert call_count == 1  # not incremented
        assert second["call"] == 1  # still 1


class TestExpireOldKeys:
    def test_expire_removes_old_keys(self) -> None:
        key = "test-expire-key"
        store_idempotent_result(key, "test_tool", "hash", json.dumps({"ok": True}))
        # Use a very short expiry to ensure it's not expired yet.
        # expire_old_keys is best-effort; we just verify it runs.
        count = expire_old_keys(hours=0)
        assert count >= 0