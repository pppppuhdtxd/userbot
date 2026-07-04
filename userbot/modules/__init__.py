"""
userbot/__init__.py
════════════════════════════════════════════════════════════════
Package initializer — reads version from VERSION file.
════════════════════════════════════════════════════════════════
"""
from pathlib import Path

_version_file = Path(__file__).resolve().parent.parent / "VERSION"

try:
    __version__ = _version_file.read_text(encoding="utf-8").strip()
except FileNotFoundError:
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]