"""Cross-platform subprocess launch flags."""

from __future__ import annotations

import os
import subprocess


def no_window_creationflags(*, new_process_group: bool = False) -> int:
    """Return Windows flags for a console-free non-interactive child process."""
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags
