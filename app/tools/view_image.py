"""MCP tool: view_image.

Reads an image from a workspace and returns metadata plus the image data
as MCP ImageContent so the model can inspect it directly.

Supported formats: PNG, JPEG, WebP.  GIF is limited to the first frame.
SVG is not supported (returns ``UNSUPPORTED_FILE_TYPE``).

Security
--------
- Paths are validated against the workspace boundary (``resolve_within``).
- MIME type is detected from file content, never from the extension alone.
- File size, pixel count, and dimensions are capped.
- Corrupted or non-image files are rejected with a structured error.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from app.config import get_image_config
from app.services.envelope import (
    audit_event,
    elapsed_ms,
    error_result,
    generate_request_id,
    ok_result,
)
from app.services.path_guard import is_denied, resolve_within
from app.services.workspace_manager import get_workspace

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MIME type helpers
# ---------------------------------------------------------------------------

_EXTENSION_MIME_FALLBACK: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}

# PIL.Image.OPEN —> MIME type mapping for the formats we accept.
_PIL_FORMAT_TO_MIME: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}

# ---------------------------------------------------------------------------
# Image detail scaling
# ---------------------------------------------------------------------------

HIGH_DETAIL_MAX_DIMENSION = 2048  # default, overridden by config


def _scale_image(
    image_bytes: bytes,
    detail: str,
    img_cfg: dict[str, Any],
) -> tuple[bytes, int, int]:
    """Scale *image_bytes* according to *detail*.

    Returns (scaled_bytes, width, height).  When ``detail="original"`` the
    bytes are returned as-is (caller must have already checked size limits).
    """
    from PIL import Image as PILImage

    buf = io.BytesIO(image_bytes)
    with PILImage.open(buf) as img:
        image: PILImage.Image = img
        # Honour EXIF orientation so width/height are correct.
        try:
            from PIL import ImageOps

            image = ImageOps.exif_transpose(image)
        except Exception:
            pass

        original_w, original_h = image.size

        if detail == "original":
            return image_bytes, original_w, original_h

        # "high" — scale down to fit within max_dimension_high on the
        # longest side, maintaining aspect ratio.
        max_dim = int(img_cfg.get("max_dimension_high", HIGH_DETAIL_MAX_DIMENSION))
        if original_w <= max_dim and original_h <= max_dim:
            return image_bytes, original_w, original_h

        ratio = min(max_dim / original_w, max_dim / original_h)
        new_w = max(1, int(original_w * ratio))
        new_h = max(1, int(original_h * ratio))

        resampling = getattr(PILImage, "Resampling", PILImage)
        resized = image.resize((new_w, new_h), resampling.LANCZOS)

        # Convert RGBA to RGB for JPEG output if needed, but prefer PNG
        # for transparency support.
        out_fmt = "PNG"
        if image.mode in ("RGBA", "LA", "P"):
            out_fmt = "PNG"
        elif image.mode == "1":
            out_fmt = "PNG"

        out_buf = io.BytesIO()
        resized.save(out_buf, format=out_fmt)
        scaled_bytes = out_buf.getvalue()

        # Re-read to get accurate dimensions.
        with PILImage.open(io.BytesIO(scaled_bytes)) as final:
            return scaled_bytes, final.width, final.height


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

_SUPPORTED_MIME_PREFIXES = ("image/",)


def _detect_mime(path: Path, image_bytes: bytes) -> str | None:
    """Detect MIME type from file content using Pillow.

    Returns ``None`` when the content is not a recognised image format.
    """
    from PIL import Image as PILImage

    try:
        buf = io.BytesIO(image_bytes)
        with PILImage.open(buf) as img:
            pil_format: str = img.format or ""
            return _PIL_FORMAT_TO_MIME.get(pil_format.upper())
    except Exception:
        return None


def _view_image(
    workspace_id: str,
    path: str,
    detail: str = "high",
) -> dict[str, Any] | list[Any]:
    """Core implementation of the view_image tool."""
    start = time.monotonic()
    request_id = generate_request_id()

    def fail(
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> dict[str, Any]:
        result = error_result(
            code,
            message,
            retryable=retryable,
            workspace_id=workspace_id,
            request_id=request_id,
        )
        audit_event(
            tool_name="view_image",
            request_id=request_id,
            workspace_id=workspace_id,
            input_summary=f"path={path!r} detail={detail!r}",
            success=False,
            duration_ms=elapsed_ms(start),
            error_code=code,
        )
        return result

    # ---- Validate workspace ----
    record = get_workspace(workspace_id)
    if record is None:
        return fail("WORKSPACE_NOT_FOUND", f"workspace not found: {workspace_id}")
    worktree = Path(record["worktree_path"])
    if not worktree.is_dir():
        return fail("STALE_WORKSPACE", f"worktree path missing on disk: {worktree}")

    # ---- Validate detail parameter ----
    if detail not in ("high", "original"):
        return fail("INVALID_INPUT", f"detail must be 'high' or 'original', got {detail!r}")

    # ---- Resolve path ----
    try:
        absolute = resolve_within(worktree, path)
    except ValueError as exc:
        return fail("PATH_DENIED", str(exc))
    if is_denied(absolute, worktree):
        return fail("PATH_DENIED", f"path is denied by policy: {path!r}")

    if not absolute.is_file():
        return fail("PATH_DENIED", f"path is not a regular file: {path!r}")

    # ---- Check file size ----
    img_cfg = get_image_config()
    max_size = int(img_cfg.get("max_file_size_bytes", 20_971_520))
    try:
        file_size = absolute.stat().st_size
    except OSError as exc:
        return fail("INTERNAL_ERROR", f"failed to stat image: {exc}", retryable=True)
    if file_size > max_size:
        return fail(
            "FILE_TOO_LARGE",
            f"file size {file_size} bytes exceeds limit of {max_size} bytes",
        )

    # ---- Read raw bytes ----
    try:
        raw_bytes = absolute.read_bytes()
    except OSError as exc:
        return fail("INTERNAL_ERROR", f"failed to read file: {exc}", retryable=True)

    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    # ---- Detect MIME type from content ----
    detected_mime = _detect_mime(absolute, raw_bytes)

    # ---- SVG handling (check extension before content detection) ----
    if path.lower().endswith(".svg"):
        return fail(
            "UNSUPPORTED_FILE_TYPE",
            "SVG images are not supported in this release. "
            "Use a raster format such as PNG or JPEG.",
        )

    if detected_mime is None:
        return fail(
            "IMAGE_DECODE_FAILED",
            f"file content is not a recognised image format: {path!r}",
        )

    # ---- Check supported formats ----
    supported = set(img_cfg.get("supported_formats", ["image/png", "image/jpeg", "image/webp"]))
    if detected_mime not in supported:
        return fail(
            "UNSUPPORTED_FILE_TYPE",
            f"unsupported image format: {detected_mime}. Supported: {', '.join(sorted(supported))}",
        )

    # ---- Validate image dimensions ----
    from PIL import Image as PILImage

    try:
        buf = io.BytesIO(raw_bytes)
        with PILImage.open(buf) as img:
            image: PILImage.Image = img
            # Honour EXIF orientation.
            try:
                from PIL import ImageOps

                image = ImageOps.exif_transpose(image)
            except Exception:
                pass
            orig_w, orig_h = image.size
    except Exception as exc:
        return fail("IMAGE_DECODE_FAILED", f"failed to decode image: {exc}")

    # Zero dimensions check.
    if orig_w == 0 or orig_h == 0:
        return fail("IMAGE_DIMENSIONS_TOO_LARGE", "image has zero width or height")

    # Pixel count check.
    max_pixels = int(img_cfg.get("max_pixels", 50_000_000))
    if orig_w * orig_h > max_pixels:
        return fail(
            "IMAGE_DIMENSIONS_TOO_LARGE",
            f"image dimensions {orig_w}x{orig_h} ({orig_w * orig_h} pixels) "
            f"exceed limit of {max_pixels} pixels",
        )

    # Max dimension check.
    max_dim = int(img_cfg.get("max_dimension", 10_000))
    if orig_w > max_dim or orig_h > max_dim:
        return fail(
            "IMAGE_DIMENSIONS_TOO_LARGE",
            f"image dimension {orig_w}x{orig_h} exceeds limit of {max_dim}px on one axis",
        )

    # ---- Scale image per detail ----
    try:
        scaled_bytes, display_w, display_h = _scale_image(raw_bytes, detail, img_cfg)
    except Exception as exc:
        return fail("IMAGE_DECODE_FAILED", f"image scaling failed: {exc}")

    # ---- Determine output format for ImageContent ----
    out_format = "png"
    if detected_mime == "image/jpeg":
        out_format = "jpeg"
    elif detected_mime == "image/webp":
        out_format = "webp"

    # ---- Build result ----
    result_meta = {
        "path": path,
        "mime_type": detected_mime,
        "width": display_w,
        "height": display_h,
        "original_width": orig_w,
        "original_height": orig_h,
        "file_size_bytes": file_size,
        "sha256": sha256,
    }

    envelope = ok_result(result_meta, workspace_id=workspace_id, request_id=request_id)

    # For the MCP response, we return a list: [TextContent (envelope), ImageContent].
    # FastMCP converts this to proper MCP content items.
    # We need to return the envelope as a dict so FastMCP serialises it to JSON.
    image_content = Image(data=scaled_bytes, format=out_format)

    log.info(
        "view_image workspace_id=%s path=%s mime=%s %dx%d elapsed=%dms",
        workspace_id,
        path,
        detected_mime,
        display_w,
        display_h,
        elapsed_ms(start),
    )

    # Return as a list of content items: the envelope dict (TextContent) and Image.
    result: list[Any] = [envelope, image_content]
    audit_event(
        tool_name="view_image",
        request_id=request_id,
        workspace_id=workspace_id,
        input_summary=f"path={path!r} detail={detail!r}",
        success=True,
        duration_ms=elapsed_ms(start),
    )
    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tools(mcp: FastMCP) -> None:
    """Register the view_image tool on the FastMCP instance."""

    @mcp.tool(
        name="view_image",
        description=(
            "Read an image from a workspace and return its metadata together "
            "with the image data so the model can inspect it visually. "
            "Supported formats: PNG, JPEG, WebP. "
            "GIF returns the first frame only. "
            "SVG is not supported. "
            "Use detail='high' (default) for a scaled version suitable for "
            "model viewing, or detail='original' for full resolution."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def view_image(
        workspace_id: str,
        path: str,
        detail: str = "high",
    ) -> dict[str, object] | list[object]:
        """View an image from the workspace.

        Args:
            workspace_id: The workspace to read from.
            path: Workspace-relative path to the image file.
            detail: ``"high"`` (default) scales the image to fit within
                    ``max_dimension_high`` pixels on the longest side.
                    ``"original"`` returns the image at its native resolution,
                    subject to pixel and dimension limits.
        """
        log.info("view_image workspace_id=%s path=%s detail=%s", workspace_id, path, detail)
        return _view_image(workspace_id, path, detail)
