"""Unit tests for the view_image tool.

Covers all acceptance criteria from the iteration plan (section 4),
including image decoding, MIME detection, scaling, security boundaries,
and error codes.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.utilities.types import Image as MCPImage

from app.tools.view_image import (
    _detect_mime,
    _scale_image,
    _view_image,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png_bytes(
    width: int = 4,
    height: int = 4,
    colour: tuple[int, int, int, int] = (255, 0, 0, 255),
) -> bytes:
    """Create a minimal valid PNG of the given size."""
    from PIL import Image as PILImage

    img = PILImage.new("RGBA", (width, height), colour)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(width: int = 4, height: int = 4) -> bytes:
    """Create a minimal valid JPEG of the given size."""
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (width, height), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes(width: int = 4, height: int = 4) -> bytes:
    """Create a minimal valid WebP of the given size."""
    from PIL import Image as PILImage

    img = PILImage.new("RGBA", (width, height), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _gif_bytes(width: int = 4, height: int = 4) -> bytes:
    """Create a minimal valid GIF."""
    from PIL import Image as PILImage

    img = PILImage.new("RGBA", (width, height), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()


def _setup_workspace(
    tmp_path: Path,
    workspace_id: str = "ws-00000001",
    worktree_name: str = "ws-00000001-test",
) -> tuple[Path, str]:
    """Create a mock workspace directory and return its path and the workspace_id."""
    worktree = tmp_path / worktree_name
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree, workspace_id


def _mock_workspace_in_db(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, workspace_id: str
) -> None:
    """Patch get_workspace to return a minimal record."""

    def fake_get_workspace(wid: str) -> dict[str, Any] | None:
        if wid == workspace_id:
            return {
                "workspace_id": wid,
                "project_id": "test-project",
                "worktree_path": str(worktree),
                "status": "active",
                "base_commit": "abc123",
            }
        return None

    import app.tools.view_image as view_image_mod

    monkeypatch.setattr(view_image_mod, "get_workspace", fake_get_workspace)


# ---------------------------------------------------------------------------
# MIME detection
# ---------------------------------------------------------------------------


class TestDetectMime:
    def test_png_from_content(self, tmp_path: Path) -> None:
        data = _png_bytes()
        path = tmp_path / "test.txt"  # wrong extension
        path.write_bytes(data)
        assert _detect_mime(path, data) == "image/png"

    def test_jpeg_from_content(self, tmp_path: Path) -> None:
        data = _jpeg_bytes()
        path = tmp_path / "test.txt"
        path.write_bytes(data)
        assert _detect_mime(path, data) == "image/jpeg"

    def test_webp_from_content(self, tmp_path: Path) -> None:
        data = _webp_bytes()
        path = tmp_path / "test.txt"
        path.write_bytes(data)
        assert _detect_mime(path, data) == "image/webp"

    def test_gif_from_content(self, tmp_path: Path) -> None:
        data = _gif_bytes()
        path = tmp_path / "test.txt"
        path.write_bytes(data)
        assert _detect_mime(path, data) == "image/gif"

    def test_plain_text_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "test.png"
        path.write_text("not an image", encoding="utf-8")
        data = path.read_bytes()
        assert _detect_mime(path, data) is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "test.png"
        path.write_bytes(b"")
        assert _detect_mime(path, b"") is None


# ---------------------------------------------------------------------------
# Image scaling
# ---------------------------------------------------------------------------


class TestScaleImage:
    def test_original_returns_unchanged(self) -> None:
        data = _png_bytes(16, 16)
        scaled, w, h = _scale_image(data, "original", {})
        assert scaled == data
        assert w == 16
        assert h == 16

    def test_high_small_image_unchanged(self) -> None:
        data = _png_bytes(100, 100)
        scaled, w, h = _scale_image(data, "high", {"max_dimension_high": 2048})
        assert scaled == data
        assert w == 100
        assert h == 100

    def test_high_large_image_scaled_down(self) -> None:
        """A 3000x3000 image should be scaled to fit within 2048."""
        data = _png_bytes(3000, 3000)
        scaled, w, h = _scale_image(data, "high", {"max_dimension_high": 2048})
        assert w <= 2048
        assert h <= 2048
        assert w == h  # aspect ratio preserved (square image)
        assert scaled != data

    def test_high_wide_image_maintains_aspect_ratio(self) -> None:
        """A 4000x2000 image should be scaled to 2048x1024."""
        data = _png_bytes(4000, 2000)
        scaled, w, h = _scale_image(data, "high", {"max_dimension_high": 2048})
        assert w <= 2048
        assert h <= 2048
        # Aspect ratio should be approximately 2:1
        assert abs(w / h - 2.0) < 0.01


# ---------------------------------------------------------------------------
# Tool: view_image — workspace validation
# ---------------------------------------------------------------------------


class TestViewImageWorkspaceValidation:
    def test_unknown_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.tools.view_image as view_image_mod

        monkeypatch.setattr(view_image_mod, "get_workspace", lambda wid: None)
        result = _view_image("ws-00000000", "test.png")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "WORKSPACE_NOT_FOUND"

    def test_invalid_detail_parameter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        result = _view_image(ws_id, "test.png", detail="zoom")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Tool: view_image — path security
# ---------------------------------------------------------------------------


class TestViewImagePathSecurity:
    def test_path_escape_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        result = _view_image(ws_id, "../etc/passwd")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "PATH_DENIED"

    def test_absolute_path_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        result = _view_image(ws_id, "/etc/passwd")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "PATH_DENIED"

    def test_nonexistent_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        result = _view_image(ws_id, "nonexistent.png")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error"]["code"] == "PATH_DENIED"


# ---------------------------------------------------------------------------
# Tool: view_image — format support
# ---------------------------------------------------------------------------


class TestViewImageFormatSupport:
    def test_png_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        img_path.write_bytes(_png_bytes(16, 16))

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, list)
        envelope, image_content = result
        assert envelope["ok"] is True
        assert envelope["result"]["mime_type"] == "image/png"
        assert envelope["result"]["width"] == 16
        assert envelope["result"]["height"] == 16
        assert isinstance(image_content, MCPImage)

    def test_jpeg_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.jpg"
        img_path.write_bytes(_jpeg_bytes(16, 16))

        result = _view_image(ws_id, "test.jpg")
        assert isinstance(result, list)
        envelope, _ = result
        assert envelope["ok"] is True
        assert envelope["result"]["mime_type"] == "image/jpeg"

    def test_webp_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.webp"
        img_path.write_bytes(_webp_bytes(16, 16))

        result = _view_image(ws_id, "test.webp")
        assert isinstance(result, list)
        envelope, _ = result
        assert envelope["ok"] is True
        assert envelope["result"]["mime_type"] == "image/webp"

    def test_gif_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """GIF is not in the default supported formats list."""
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.gif"
        img_path.write_bytes(_gif_bytes(16, 16))

        result = _view_image(ws_id, "test.gif")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    def test_svg_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.svg"
        img_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8"
        )

        result = _view_image(ws_id, "test.svg")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    def test_unsupported_format_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """e.g. BMP is not in the supported_formats list."""
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        from PIL import Image as PILImage

        img = PILImage.new("RGB", (4, 4), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="BMP")
        img_path = worktree / "test.bmp"
        img_path.write_bytes(buf.getvalue())

        result = _view_image(ws_id, "test.bmp")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


# ---------------------------------------------------------------------------
# Tool: view_image — file validation
# ---------------------------------------------------------------------------


class TestViewImageFileValidation:
    def test_text_file_renamed_to_png_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        img_path.write_text("This is not an image", encoding="utf-8")

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "IMAGE_DECODE_FAILED"

    def test_executable_renamed_to_jpg_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Create a fake 'executable' by writing ELF/MZ header bytes."""
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.jpg"
        # MZ header (DOS executable)
        img_path.write_bytes(b"MZ" + b"\x00" * 30)

        result = _view_image(ws_id, "test.jpg")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "IMAGE_DECODE_FAILED"

    def test_corrupted_image_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        # Valid PNG header but corrupted body
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "IMAGE_DECODE_FAILED"

    def test_file_too_large_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"

        # Create a fake "large file" — we override max_file_size_bytes to 1
        # by patching get_image_config.
        def tiny_limit() -> dict[str, Any]:
            return {
                "max_file_size_bytes": 1,
                "max_pixels": 50_000_000,
                "max_dimension": 10_000,
                "max_dimension_high": 2048,
                "supported_formats": ["image/png", "image/jpeg", "image/webp"],
                "deny_svg": True,
            }

        import app.tools.view_image as view_image_mod

        monkeypatch.setattr(view_image_mod, "get_image_config", tiny_limit)
        img_path.write_bytes(_png_bytes(4, 4))

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "FILE_TOO_LARGE"

    def test_dimensions_too_large_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)

        # Patch config with tiny pixel limit.
        def tiny_pixel_limit() -> dict[str, Any]:
            return {
                "max_file_size_bytes": 20_971_520,
                "max_pixels": 10,  # very small
                "max_dimension": 10_000,
                "max_dimension_high": 2048,
                "supported_formats": ["image/png", "image/jpeg", "image/webp"],
                "deny_svg": True,
            }

        import app.tools.view_image as view_image_mod

        monkeypatch.setattr(view_image_mod, "get_image_config", tiny_pixel_limit)
        img_path = worktree / "test.png"
        img_path.write_bytes(_png_bytes(100, 100))

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, dict)
        assert result["error"]["code"] == "IMAGE_DIMENSIONS_TOO_LARGE"

    def test_zero_dimension_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A PNG with zero width/height is rejected by the decoder."""
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"

        # Create a valid 1x1 PNG.
        from PIL import Image as PILImage

        img = PILImage.new("RGBA", (1, 1), (255, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()

        # Corrupt the IHDR width bytes to zero (this breaks CRC, making it
        # undecodable, which is the correct behavior for zero-dimension data).
        corrupted = raw[:16] + b"\x00\x00\x00\x00" + raw[20:]
        img_path.write_bytes(corrupted)

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, dict)
        assert result["error"]["code"] in ("IMAGE_DECODE_FAILED", "IMAGE_DIMENSIONS_TOO_LARGE")


# ---------------------------------------------------------------------------
# Tool: view_image — detail parameter
# ---------------------------------------------------------------------------


class TestViewImageDetail:
    def test_detail_original_returns_full_resolution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        data = _png_bytes(100, 100)
        img_path.write_bytes(data)

        result = _view_image(ws_id, "test.png", detail="original")
        assert isinstance(result, list)
        envelope, image_content = result
        assert envelope["result"]["width"] == 100
        assert envelope["result"]["height"] == 100
        assert envelope["result"]["original_width"] == 100
        assert envelope["result"]["original_height"] == 100

    def test_detail_high_scales_large_image(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        data = _png_bytes(3000, 3000)
        img_path.write_bytes(data)

        result = _view_image(ws_id, "test.png", detail="high")
        assert isinstance(result, list)
        envelope, _ = result
        assert envelope["result"]["width"] <= 2048
        assert envelope["result"]["height"] <= 2048
        # Original dimensions are preserved in metadata
        assert envelope["result"]["original_width"] == 3000
        assert envelope["result"]["original_height"] == 3000


# ---------------------------------------------------------------------------
# Tool: view_image — return structure
# ---------------------------------------------------------------------------


class TestViewImageReturnStructure:
    def test_success_envelope_contains_required_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        data = _png_bytes(16, 16)
        img_path.write_bytes(data)

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, list)
        envelope, _ = result
        assert envelope["ok"] is True
        assert "request_id" in envelope
        assert envelope["request_id"].startswith("req_")
        assert envelope["workspace_id"] == ws_id
        assert "result" in envelope
        assert "warnings" in envelope
        assert "truncated" in envelope

        meta = envelope["result"]
        assert meta["path"] == "test.png"
        assert meta["mime_type"] == "image/png"
        assert isinstance(meta["width"], int)
        assert isinstance(meta["height"], int)
        assert isinstance(meta["sha256"], str)
        assert len(meta["sha256"]) == 64  # SHA-256 hex

    def test_success_returns_image_content(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        data = _png_bytes(16, 16)
        img_path.write_bytes(data)

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, list)
        _, image_content = result
        assert isinstance(image_content, MCPImage)
        # Verify the image content can produce an ImageContent
        mcp_content = image_content.to_image_content()
        assert mcp_content.type == "image"
        assert mcp_content.mimeType == "image/png"

    def test_error_envelope_contains_required_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        img_path.write_text("not an image", encoding="utf-8")

        result = _view_image(ws_id, "test.png")
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert "request_id" in result
        assert result["request_id"].startswith("req_")
        assert "error" in result
        error = result["error"]
        assert "code" in error
        assert "message" in error
        assert "retryable" in error

    def test_sha256_integrity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test.png"
        data = _png_bytes(16, 16)
        img_path.write_bytes(data)

        expected_sha = hashlib.sha256(data).hexdigest()
        result = _view_image(ws_id, "test.png")
        assert isinstance(result, list)
        envelope, _ = result
        assert envelope["result"]["sha256"] == expected_sha


# ---------------------------------------------------------------------------
# Tool: view_image — EXIF orientation
# ---------------------------------------------------------------------------


class TestViewImageExif:
    def test_exif_orientation_handled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Create an image with EXIF orientation tag and confirm it's handled."""
        worktree, ws_id = _setup_workspace(tmp_path)
        _mock_workspace_in_db(monkeypatch, worktree, ws_id)
        img_path = worktree / "test_exif.jpg"

        from PIL import Image as PILImage

        # Create a 200x100 image with EXIF orientation 6 (rotate 90° CW)
        img = PILImage.new("RGB", (200, 100), (0, 128, 0))
        exif = img.getexif()
        exif[0x0112] = 6  # Orientation: rotate 90 CW
        img.save(str(img_path), exif=exif)

        result = _view_image(ws_id, "test_exif.jpg", detail="original")
        assert isinstance(result, list)
        envelope, _ = result
        # After EXIF rotation, 200x100 becomes 100x200
        # But actually, exif_transpose returns a new image, and the scaled
        # result should reflect the transposed dimensions.
        # With detail="original", the dimensions should be transposed.
        assert envelope["result"]["width"] == 100
        assert envelope["result"]["height"] == 200
