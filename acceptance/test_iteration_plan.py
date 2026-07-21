"""Executable acceptance contracts for iteration-plan Sections 2 through 9.

These tests intentionally live outside ``tests/`` so roadmap contracts do not
break the normal unit/integration suite before their section is implemented.
Run them through ``scripts/accept-iteration.py`` or the configured MCP checks.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import re
import tomllib
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _module(name: str, requirement: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"{requirement}: missing module {name}", pytrace=False)


def _require_names(module: ModuleType, names: set[str], requirement: str) -> None:
    missing = sorted(name for name in names if not hasattr(module, name))
    assert not missing, f"{requirement}: missing {', '.join(missing)}"


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _package_version() -> str:
    """Resolve installed metadata or the same source declaration in a checkout."""
    try:
        return importlib.metadata.version("gpt-local-code-operator")
    except importlib.metadata.PackageNotFoundError:
        pyproject = tomllib.loads(_text("pyproject.toml"))
        return str(pyproject["project"]["version"])


def test_section_2_central_tool_contract_and_middleware() -> None:
    module = _module("app.services.tool_registry", "central tool execution contract")
    _require_names(
        module,
        {"ToolSpec", "Effect", "IdempotencyPolicy", "RetryPolicy", "execute_tool"},
        "ToolSpec and middleware pipeline",
    )
    tool_spec = module.ToolSpec
    assert is_dataclass(tool_spec), "ToolSpec must be a dataclass"
    actual_fields = {field.name for field in fields(tool_spec)}
    required_fields = {
        "name",
        "effects",
        "concurrency_key",
        "idempotency",
        "interrupt_behavior",
        "max_result_chars",
        "permission_profile",
        "retry_policy",
    }
    assert required_fields <= actual_fields


def test_section_2_errors_version_and_idempotency_are_consistent() -> None:
    envelope = _text("app/services/envelope.py")
    assert "PROCESS_NOT_RUNNING" in envelope
    assert "PTY_NOT_ACTIVE" in envelope

    capabilities = importlib.import_module("app.tools.capabilities")
    assert capabilities.SERVER_VERSION == _package_version()

    powershell = _text("app/tools/powershell.py")
    assert re.search(
        r"with_idempotency[\s\S]{0,1500}working_directory", powershell
    ), "run_pwsh idempotency input must include working_directory"


def test_section_3_scheduler_fairness_limits_and_recovery() -> None:
    scheduler = _module("app.services.process_scheduler", "process scheduling")
    _require_names(
        scheduler,
        {"ProcessScheduler", "ConcurrencyKey", "ResourceLimits", "QueuePolicy"},
        "fair, keyed process scheduler",
    )
    recovery = _module("app.services.process_recovery", "service restart recovery")
    _require_names(recovery, {"recover_processes"}, "process identity recovery")
    signature = inspect.signature(recovery.recover_processes)
    assert "database" in signature.parameters

    migrations = "\n".join(path.read_text(encoding="utf-8") for path in _migration_files())
    for column in ("process_creation_identity", "heartbeat", "last_output_offset"):
        assert column in migrations, f"process recovery schema lacks {column}"


def test_section_4_symbol_level_code_intelligence() -> None:
    module = _module("app.tools.code_intelligence", "symbol-level code intelligence")
    _require_names(
        module,
        {
            "list_symbols",
            "find_definition",
            "find_references",
            "find_implementations",
            "get_call_hierarchy",
            "get_diagnostics",
            "get_changed_symbols",
        },
        "code-intelligence tool surface",
    )


def test_section_5_append_only_event_stream() -> None:
    module = _module("app.tools.events", "append-only event stream")
    _require_names(module, {"get_events", "subscribe_process"}, "event cursor API")
    migrations = "\n".join(path.read_text(encoding="utf-8") for path in _migration_files())
    assert "CREATE TABLE" in migrations and "events" in migrations
    for column in ("event_id", "sequence", "event_type"):
        assert column in migrations, f"event schema lacks {column}"


def test_section_6_atomic_change_sets() -> None:
    module = _module("app.tools.change_sets", "atomic multi-file change sets")
    _require_names(
        module,
        {
            "begin_change_set",
            "stage_patch",
            "stage_replace",
            "validate_change_set",
            "commit_change_set",
            "rollback_change_set",
        },
        "change-set lifecycle",
    )


def test_section_7_structured_plan_evidence_and_check_dag() -> None:
    plan = _module("app.services.workspace_plan", "structured plan evidence")
    _require_names(plan, {"StepKind", "EvidenceRequirement"}, "typed plan steps")
    runner = _module("app.services.check_runner", "check DAG execution")
    _require_names(runner, {"CheckGraph", "CheckParser", "run_check_graph"}, "check DAG")

    projects = yaml.safe_load(_text("config/projects.yaml"))["projects"]
    configured_checks = [
        check
        for project in projects.values()
        for check in project["checks"].values()
    ]
    assert any("depends_on" in check for check in configured_checks)
    assert any("parser" in check for check in configured_checks)


def test_section_8_large_file_and_cursor_contracts() -> None:
    reader = importlib.import_module("app.tools.reader")
    _require_names(reader, {"_hash_file_streaming", "_read_text_range"}, "streaming file reads")

    powershell = importlib.import_module("app.tools.powershell")
    _require_names(powershell, {"_read_output_range"}, "seek-based process output")

    search = importlib.import_module("app.tools.search")
    search_signature = inspect.signature(search._search)
    assert "cursor" in search_signature.parameters
    assert "timeout_seconds" in search_signature.parameters

    registry = importlib.import_module("app.services.artifact_registry")
    _require_names(registry, {"_hash_file_streaming", "ArtifactHashCache"}, "artifact hash cache")


def test_section_9_local_config_migrations_and_dependency_lock() -> None:
    example = ROOT / "config/projects.example.yaml"
    assert example.is_file(), "commit config/projects.example.yaml"
    assert "projects.local.yaml" in _text(".gitignore")

    tracked_config = (
        _text("config/projects.yaml")
        if (ROOT / "config/projects.yaml").exists()
        else ""
    )
    assert not re.search(r"(?i)([A-Z]:[/\\]|/Users/|/home/)", tracked_config), (
        "tracked project config contains a personal absolute path"
    )

    migrations = _migration_files()
    assert migrations, "add versioned SQL migrations"
    assert "schema_migrations" in "\n".join(
        path.read_text(encoding="utf-8") for path in migrations
    )
    assert any((ROOT / name).is_file() for name in ("uv.lock", "poetry.lock", "pdm.lock")), (
        "add a reproducible dependency lock file"
    )


def _migration_files() -> list[Path]:
    migration_dir = ROOT / "app/storage/migrations"
    return sorted(migration_dir.glob("[0-9][0-9][0-9]_*.sql")) if migration_dir.is_dir() else []
