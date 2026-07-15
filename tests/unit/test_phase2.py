"""Unit tests for Phase 2: apply_patch.

Tests the internal helper functions of ``app.tools.patcher`` in isolation
without a running MCP server or a real Git worktree.
"""

import pytest

from app.tools.patcher import (
    _classify_change_kind,
    _classify_patch_error,
    _extract_changed_files,
    _extract_file_changes,
    _strip_patch_wrappers,
    _validate_patch_paths,
    _validate_unified_diff,
)

# ═══════════════════════════════════════════════════════════════════════
# _strip_patch_wrappers
# ═══════════════════════════════════════════════════════════════════════


class TestStripPatchWrappers:
    def test_passes_through_clean_diff(self) -> None:
        patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(patch) == patch

    def test_strips_begin_end_patch_markers(self) -> None:
        raw = (
            "*** Begin Patch ***\n--- a/foo.py\n+++ b/foo.py\n"
            "@@ -1 +1 @@\n-old\n+new\n*** End Patch ***\n"
        )
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_strips_dashed_begin_end_patch_markers(self) -> None:
        raw = (
            "--- Begin Patch ---\n--- a/foo.py\n+++ b/foo.py\n"
            "@@ -1 +1 @@\n-old\n+new\n--- End Patch ---\n"
        )
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_strips_leading_text_before_diff(self) -> None:
        raw = (
            "Here is a patch I generated:\n\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_strips_trailing_text_after_diff(self) -> None:
        raw = (
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n\n"
            "This patch adds a new feature.\n"
        )
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_strips_both_leading_and_trailing_text(self) -> None:
        raw = (
            "GPT generated this patch:\n\n--- a/foo.py\n+++ b/foo.py\n"
            "@@ -1 +1 @@\n-old\n+new\n\nPlease review carefully.\n"
        )
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_strips_begin_end_markers_with_leading_text(self) -> None:
        raw = (
            "I'll apply the following patch:\n*** Begin Patch ***\n"
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n*** End Patch ***\n"
        )
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_returns_empty_string(self) -> None:
        assert _strip_patch_wrappers("") == ""

    def test_strips_leading_text_with_diff_git_header(self) -> None:
        raw = (
            "Here is a diff:\ndiff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        expected = (
            "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        assert _strip_patch_wrappers(raw) == expected

    def test_normalises_crlf_to_lf(self) -> None:
        raw = "--- a/foo.py\r\n+++ b/foo.py\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
        expected = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        assert _strip_patch_wrappers(raw) == expected

    def test_returns_raw_text_when_no_diff_found(self) -> None:
        raw = "This is just a comment with no diff content."
        assert _strip_patch_wrappers(raw) == raw


# ═══════════════════════════════════════════════════════════════════════
# _validate_patch_paths
# ═══════════════════════════════════════════════════════════════════════


class TestValidatePatchPaths:
    def test_accepts_standard_unified_diff(self) -> None:
        patch = (
            "--- a/foo/bar.py\n+++ b/foo/bar.py\n@@ -1,3 +1,4 @@\n line1\n-old line\n+new line\n"
        )
        _validate_patch_paths(patch)

    def test_accepts_new_file_dev_null(self) -> None:
        patch = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1 @@\n+new content\n"
        _validate_patch_paths(patch)

    def test_accepts_deleted_file_dev_null(self) -> None:
        patch = "--- a/old_file.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old content\n"
        _validate_patch_paths(patch)

    def test_accepts_paths_with_spaces(self) -> None:
        patch = (
            '--- "a/path with spaces/file.py"\n'
            '+++ "b/path with spaces/file.py"\n'
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        _validate_patch_paths(patch)

    def test_rejects_absolute_unix_path(self) -> None:
        patch = "--- a/file.py\n+++ /etc/passwd\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(ValueError, match="absolute path"):
            _validate_patch_paths(patch)

    def test_rejects_absolute_windows_path(self) -> None:
        patch = "--- a/file.py\n+++ C:\\Windows\\system32\\config\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(ValueError, match="absolute path"):
            _validate_patch_paths(patch)

    def test_rejects_absolute_windows_path_forward_slash(self) -> None:
        patch = "--- a/file.py\n+++ D:/Users/evil/config\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(ValueError, match="absolute path"):
            _validate_patch_paths(patch)

    def test_rejects_traversal(self) -> None:
        patch = "--- a/../../etc/passwd\n+++ b/../../etc/shadow\n@@ -1 +1 @@\n-old\n+new\n"
        with pytest.raises(ValueError, match="path traversal"):
            _validate_patch_paths(patch)

    def test_rejects_traversal_windows(self) -> None:
        patch = (
            "--- a/..\\..\\windows\\system32\\config\n"
            "+++ b/..\\..\\windows\\system32\\sam\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        with pytest.raises(ValueError, match="path traversal"):
            _validate_patch_paths(patch)

    def test_accepts_only_context_lines(self) -> None:
        patch = "just a random line\n"
        _validate_patch_paths(patch)

    def test_accepts_empty_patch(self) -> None:
        _validate_patch_paths("")


# ═══════════════════════════════════════════════════════════════════════
# _extract_changed_files
# ═══════════════════════════════════════════════════════════════════════


class TestExtractChangedFiles:
    def test_normal(self) -> None:
        patch = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "--- a/bar.py\n"
            "+++ b/bar.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        assert _extract_changed_files(patch) == ["foo.py", "bar.py"]

    def test_new_file(self) -> None:
        patch = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1 @@\n+new content\n"
        assert _extract_changed_files(patch) == ["new_file.py"]

    def test_deleted_file(self) -> None:
        patch = "--- a/old_file.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old content\n"
        assert _extract_changed_files(patch) == ["old_file.py"]
        assert _extract_file_changes(patch) == [{"path": "old_file.py", "status": "deleted"}]

    def test_reports_added_modified_and_renamed_statuses(self) -> None:
        patch = (
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new\n"
            "--- a/existing.py\n+++ b/existing.py\n@@ -1 +1 @@\n-old\n+new\n"
            "--- a/old.py\n+++ b/new_name.py\n@@ -1 +1 @@\n-old\n+new\n"
        )
        assert _extract_file_changes(patch) == [
            {"path": "new.py", "status": "added"},
            {"path": "existing.py", "status": "modified"},
            {"path": "new_name.py", "old_path": "old.py", "status": "renamed"},
        ]

    def test_deduplicates(self) -> None:
        patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n@@ -10 +10 @@\n-old2\n+new2\n"
        assert _extract_changed_files(patch) == ["foo.py"]

    def test_empty(self) -> None:
        assert _extract_changed_files("") == []


class TestValidateUnifiedDiff:
    def test_accepts_matching_hunk_counts(self) -> None:
        patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n context\n-old\n+new\n"
        assert _validate_unified_diff(patch) is None

    def test_reports_precise_hunk_count_mismatch(self) -> None:
        patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,3 @@\n-old\n+new\n"
        diagnostic = _validate_unified_diff(patch)
        assert diagnostic is not None
        assert diagnostic["line"] == 3
        assert diagnostic["line_content"] == "@@ -1,2 +1,3 @@"
        assert "expected old/new 2/3" in diagnostic["parser_message"]

    def test_rejects_missing_hunks(self) -> None:
        diagnostic = _validate_unified_diff("--- a/foo.py\n+++ b/foo.py\n")
        assert diagnostic is not None
        assert diagnostic["parser_message"] == "patch contains no unified-diff hunks"


# ═══════════════════════════════════════════════════════════════════════
# _classify_change_kind
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyChangeKind:
    def test_code(self) -> None:
        assert _classify_change_kind("src/main.py | 5 +++++") == "code"

    def test_docs(self) -> None:
        assert _classify_change_kind("README.md | 2 +-") == "docs"

    def test_config(self) -> None:
        assert _classify_change_kind("config/settings.yaml | 3 +++") == "config"

    def test_mixed(self) -> None:
        stat = "src/main.py | 5 +++++\nREADME.md | 2 +-"
        assert _classify_change_kind(stat) == "mixed"

    def test_unknown_on_empty(self) -> None:
        assert _classify_change_kind("") == "unknown"

    def test_unknown_on_no_diff_stat_lines(self) -> None:
        assert _classify_change_kind("nothing to see here") == "unknown"


# ═══════════════════════════════════════════════════════════════════════
# _classify_patch_error
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyPatchError:
    def test_format_error_corrupt_patch(self) -> None:
        assert _classify_patch_error("error: corrupt patch at line 3") == "format_error"

    def test_format_error_malformed(self) -> None:
        assert _classify_patch_error("malformed patch: invalid hunk header") == "format_error"

    def test_format_error_invalid_hunk(self) -> None:
        assert _classify_patch_error("error: invalid hunk header") == "format_error"

    def test_conflict_does_not_match(self) -> None:
        assert _classify_patch_error("error: patch does not match") == "conflict"

    def test_conflict_patch_failed(self) -> None:
        assert _classify_patch_error("error: patch failed: file.py:1") == "conflict"

    def test_file_not_found(self) -> None:
        assert _classify_patch_error("error: no such file: foo.py") == "file_not_found"

    def test_file_not_found_does_not_exist(self) -> None:
        assert _classify_patch_error("foo.py does not exist in the index") == "file_not_found"

    def test_unknown_on_empty(self) -> None:
        assert _classify_patch_error("") == "unknown"

    def test_unknown_on_unrecognised(self) -> None:
        assert _classify_patch_error("something unexpected happened") == "unknown"
