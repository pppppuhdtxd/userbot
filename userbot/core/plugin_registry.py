"""
core/plugin_registry.py
════════════════════════════════════════════════════════════════
Enhanced global plugin/module registry with per-module metadata,
runtime introspection, and a clean management API.

Architecture
────────────
There are two registries operating at different scopes:

`AccountLoaderRegistry`  — maps account index → AccountLoader instance.
                           One entry per running account.  Populated by main.py
                           at startup and by the account manager when accounts
                           are added dynamically.

`PluginMetadataStore`    — maps (account_index, module_stem) → PluginMetadata.
                           Populated by the loader on every load/reload and
                           used by the system module and help handler for
                           rich introspection.

Both registries are module-level singletons exposed as `loader_registry`
and `plugin_store` respectively.

Public API
──────────
loader_registry.register(index, loader)
loader_registry.get(index)                → AccountLoader
loader_registry.remove(index)
loader_registry.all()                     → dict[int, AccountLoader]

plugin_store.upsert(account_index, stem, metadata)
plugin_store.get(account_index, stem)     → PluginMetadata | None
plugin_store.remove(account_index, stem)
plugin_store.for_account(account_index)   → list[PluginMetadata]
plugin_store.all()                        → list[PluginMetadata]
════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from core.exceptions import LoaderNotFoundError

if TYPE_CHECKING:
    from core.loader import AccountLoader


# ── Plugin metadata ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class PluginMetadata:
    """
    Rich metadata snapshot for one loaded plugin instance.
    Captured at load/reload time and stored in the PluginMetadataStore.

    Attributes:
        account_index: Which account this plugin belongs to.
        stem:          File stem, e.g. "clearer" for clearer.py.
        name:          Human-readable module name from Module.name.
        help_text:     Short help string for compact help view.
        file_path:     Absolute path to the module .py file.
        loaded_at:     Timestamp of the most recent load/reload.
        load_count:    Cumulative number of times this plugin has been loaded
                       (increments on each reload, preserved across reloads).
    """
    account_index: int
    stem:          str
    name:          str
    help_text:     str
    file_path:     str
    loaded_at:     datetime = field(default_factory=datetime.now)
    load_count:    int      = 1


# ── AccountLoader registry ────────────────────────────────────────────────────

class AccountLoaderRegistry:
    """
    Thread-safe map from account index to its `AccountLoader`.

    Writes are protected by a lock; reads are not (dict reads are GIL-safe in
    CPython and the registry is written only during startup/shutdown).
    """

    def __init__(self) -> None:
        self._registry: dict[int, "AccountLoader"] = {}
        self._lock = threading.Lock()

    def register(self, account_index: int, loader: "AccountLoader") -> None:
        """Register (or replace) the loader for *account_index*."""
        with self._lock:
            self._registry[account_index] = loader

    def get(self, account_index: int) -> "AccountLoader":
        """
        Return the loader for *account_index*.

        Raises:
            LoaderNotFoundError: If no loader is registered for the index.
        """
        try:
            return self._registry[account_index]
        except KeyError:
            raise LoaderNotFoundError(account_index) from None

    def remove(self, account_index: int) -> "AccountLoader | None":
        """Remove and return the loader for *account_index*, or ``None``."""
        with self._lock:
            return self._registry.pop(account_index, None)

    def all(self) -> dict[int, "AccountLoader"]:
        """Return a shallow copy of the full registry mapping."""
        return dict(self._registry)

    def __len__(self) -> int:
        return len(self._registry)

    def __contains__(self, account_index: int) -> bool:
        return account_index in self._registry


# ── Plugin metadata store ─────────────────────────────────────────────────────

class PluginMetadataStore:
    """
    Thread-safe store mapping (account_index, stem) → PluginMetadata.
    The loader calls ``upsert()`` on every load/reload so the store always
    reflects the current runtime state.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[int, str], PluginMetadata] = {}
        self._lock = threading.Lock()

    def upsert(
        self,
        account_index: int,
        stem: str,
        metadata: PluginMetadata,
    ) -> None:
        """Insert or update the metadata for *(account_index, stem)*."""
        with self._lock:
            key = (account_index, stem)
            existing = self._store.get(key)
            if existing is not None:
                # Preserve the cumulative load count
                metadata.load_count = existing.load_count + 1
            self._store[key] = metadata

    def get(self, account_index: int, stem: str) -> PluginMetadata | None:
        """Return metadata for *(account_index, stem)*, or ``None``."""
        return self._store.get((account_index, stem))

    def remove(self, account_index: int, stem: str) -> None:
        """Delete metadata for *(account_index, stem)* if present."""
        with self._lock:
            self._store.pop((account_index, stem), None)

    def remove_account(self, account_index: int) -> None:
        """Remove all metadata entries for *account_index*."""
        with self._lock:
            keys = [k for k in self._store if k[0] == account_index]
            for k in keys:
                del self._store[k]

    def for_account(self, account_index: int) -> list[PluginMetadata]:
        """Return all metadata entries for *account_index*, sorted by stem."""
        return sorted(
            (v for k, v in self._store.items() if k[0] == account_index),
            key=lambda m: m.stem,
        )

    def all(self) -> list[PluginMetadata]:
        """Return all stored metadata entries."""
        return list(self._store.values())


# ── Module-level singletons ──────────────────────────────────────────────────

#: Global AccountLoader registry — use this throughout the project.
loader_registry = AccountLoaderRegistry()

#: Global plugin metadata store — use this for introspection.
plugin_store = PluginMetadataStore()


# ── Legacy shim (used by existing watcher / account_manager code) ─────────────
#
# The original code used a module-level `_module_registry` with
# `register()` and `get_loader()` functions.  These aliases provide full
# backwards compatibility so the rest of the codebase does not need changes.

def register(account_index: int, loader: "AccountLoader") -> None:
    """Legacy alias for `loader_registry.register()`."""
    loader_registry.register(account_index, loader)


def get_loader(account_index: int) -> "AccountLoader":
    """Legacy alias for `loader_registry.get()`."""
    return loader_registry.get(account_index)