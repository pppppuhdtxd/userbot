"""
userbot/__init__.py
════════════════════════════════════════════════════════════════
Multi-Account Telegram Userbot

A professional, async, hot-reload-capable Telegram account management
system built with Python 3.11+ and Telethon.

Version is read from the VERSION file in the project root.
════════════════════════════════════════════════════════════════
"""
from pathlib import Path

_version_file = Path(__file__).parent.parent / "VERSION"

# Fallback used only if VERSION cannot be read at all. Kept in sync with the
# actual VERSION file content so a missing/corrupted VERSION file doesn't
# silently report a stale, multiple-major-versions-old number — which would
# mislead anyone debugging via `.version` or log headers.
_FALLBACK_VERSION = "2.0.0"

try:
    __version__ = _version_file.read_text(encoding="utf-8").strip() or _FALLBACK_VERSION
except OSError:
    # Catches FileNotFoundError, PermissionError, and any other I/O failure
    # reading VERSION — not just the missing-file case. A narrower except
    # clause here would let an unreadable-but-present VERSION file crash
    # the import of the entire userbot package.
    __version__ = _FALLBACK_VERSION

__all__ = ["__version__"]