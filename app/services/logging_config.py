"""Rotating, redacted file logging for unattended service components."""

from __future__ import annotations

import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from app.config import BASE_DIR, get_logging_config

_ASSIGNMENT_RE = re.compile(
    r"(?i)(CONTROL_PLANE_API_KEY\s*[=:]\s*)([^\s,;]+)",
)


class RedactingFormatter(logging.Formatter):
    """Formatter that removes known secrets and runtime-key assignments."""

    def __init__(self, fmt: str, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return _ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)


def configure_component_logger(
    component: str,
    *,
    secrets: Iterable[str] = (),
    logs_dir: Path | None = None,
) -> logging.Logger:
    """Create an isolated rotating logger for one service component."""
    cfg = get_logging_config()
    directory = logs_dir or BASE_DIR / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    _remove_expired_logs(directory, int(cfg.get("retention_days", 14)))

    logger = logging.getLogger(f"service.{component}")
    for existing_handler in list(logger.handlers):
        logger.removeHandler(existing_handler)
        existing_handler.close()
    logger.propagate = False
    logger.setLevel(getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO))
    handler = RotatingFileHandler(
        directory / f"{component}.log",
        maxBytes=int(cfg.get("max_file_bytes", 10_485_760)),
        backupCount=int(cfg.get("backup_count", 5)),
        encoding="utf-8",
    )
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(name)s pid=%(process)d %(message)s",
            secrets,
        )
    )
    logger.addHandler(handler)
    return logger


def _remove_expired_logs(directory: Path, retention_days: int) -> None:
    if retention_days < 0:
        return
    cutoff = time.time() - retention_days * 86400
    for path in directory.glob("*.log*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
