"""Unit tests for the capabilities tool."""

from app.tools.capabilities import SCHEMA_VERSION, SERVER_VERSION, _build_capabilities


class TestCapabilities:
    def test_returns_all_required_fields(self) -> None:
        caps = _build_capabilities()
        assert caps["schema_version"] == SCHEMA_VERSION
        assert caps["server_version"] == SERVER_VERSION
        assert caps["server_name"] == "gpt-local-code-operator"
        assert "capabilities" in caps
        assert "limits" in caps

    def test_expected_capabilities_present(self) -> None:
        caps = _build_capabilities()["capabilities"]
        assert caps["supports_async_process"] is True
        assert caps["supports_expected_hash"] is True
        assert caps["supports_idempotency"] is True
        assert caps["supports_artifacts"] is True
        assert caps["supports_multi_query_search"] is True
        assert caps["supports_diff_context_lines"] is True
        assert caps["supports_diff_stat_only"] is True
        assert caps["supports_project_manifest"] is True
        assert caps["supports_read_process_output"] is True
        assert caps["supports_view_image"] is True
        assert caps["supports_pty"] is True
        assert caps["supports_process_input"] is True
        assert caps["supports_process_signal"] is True
        assert caps["supports_artifact_registry"] is True
        assert caps["supports_artifact_discovery"] is True
        assert caps["supports_workspace_plan"] is True

    def test_limits_are_positive_integers(self) -> None:
        limits = _build_capabilities()["limits"]
        for key, value in limits.items():
            assert isinstance(value, int), f"{key} should be int, got {type(value)}"
            assert value > 0, f"{key} should be positive, got {value}"
