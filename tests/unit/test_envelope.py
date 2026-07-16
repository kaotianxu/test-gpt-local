"""Unit tests for the response envelope helper."""

import time

from app.services.envelope import elapsed_ms, error_result, generate_request_id, ok_result


class TestOkResult:
    def test_basic_ok_result(self) -> None:
        result = ok_result({"key": "value"})
        assert result["ok"] is True
        assert result["result"] == {"key": "value"}
        assert result["warnings"] == []
        assert result["truncated"] is False
        assert result["next_cursor"] is None
        assert result["request_id"].startswith("req_")

    def test_ok_result_with_workspace_id(self) -> None:
        result = ok_result("data", workspace_id="ws_abc123")
        assert result["workspace_id"] == "ws_abc123"

    def test_ok_result_with_revision(self) -> None:
        result = ok_result("data", revision=5)
        assert result["revision"] == 5

    def test_ok_result_with_warnings(self) -> None:
        result = ok_result("data", warnings=["disk space low"])
        assert result["warnings"] == ["disk space low"]

    def test_ok_result_with_truncated(self) -> None:
        result = ok_result("data", truncated=True)
        assert result["truncated"] is True

    def test_ok_result_with_cursor(self) -> None:
        result = ok_result("data", next_cursor="cursor_abc")
        assert result["next_cursor"] == "cursor_abc"


class TestErrorResult:
    def test_basic_error(self) -> None:
        result = error_result("WORKSPACE_NOT_FOUND", "workspace not found: ws_xxx")
        assert result["ok"] is False
        assert result["error"]["code"] == "WORKSPACE_NOT_FOUND"
        assert result["error"]["message"] == "workspace not found: ws_xxx"
        assert result["error"]["retryable"] is False

    def test_retryable_error(self) -> None:
        result = error_result("PROCESS_TIMEOUT", "timed out", retryable=True)
        assert result["error"]["retryable"] is True

    def test_error_with_suggested_tool(self) -> None:
        result = error_result("PATCH_CONFLICT", "conflict", suggested_next_tool="read_files")
        assert result["error"]["suggested_next_tool"] == "read_files"

    def test_error_with_extra_fields(self) -> None:
        result = error_result(
            "FILE_CHANGED", "hash mismatch", extra={"expected": "abc", "actual": "def"}
        )
        assert result["error"]["expected"] == "abc"
        assert result["error"]["actual"] == "def"

    def test_error_with_workspace_id(self) -> None:
        result = error_result("STALE_WORKSPACE", "gone", workspace_id="ws_abc")
        assert result["workspace_id"] == "ws_abc"


class TestHelpers:
    def test_generate_request_id_format(self) -> None:
        rid = generate_request_id()
        assert rid.startswith("req_")
        assert len(rid) > 4

    def test_elapsed_ms(self) -> None:
        start = time.monotonic()
        time.sleep(0.01)
        ms = elapsed_ms(start)
        assert ms >= 10
        assert ms < 1000
