"""
modules/base.py
════════════════════════════════════════════════════════════════
Abstract base class for all userbot modules (plugins).

Every plugin file (e.g. `clearer.py`, `info_handler.py`) must define a
class that inherits from `Module` and exposes a factory function
`create_module(cfg)` at module level so the loader can instantiate it.

The base class provides:
• Automatic handler registration with duplicate-prevention
• Per-module structured logging helpers
• Safe message editing (swallows MessageNotModified / NotFound)
• Cached self-ID retrieval
• Help text attributes (`help_text` for compact view, `help_extra` for
  detailed view via `help <module>` command)

Lifecycle
─────────
create_module(cfg)  → Module instance
instance.setup(client)      # register handlers, warm caches
[running…]
instance.teardown(client)   # remove handlers (hot-reload safe)
════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable
from weakref import WeakKeyDictionary

from telethon import TelegramClient, errors, events
from telethon.events.common import EventBuilder

from core.logger import get_logger

if TYPE_CHECKING:
    from config import AccountConfig


class Module:
    """
    Abstract base for all userbot plugins.

    Subclasses must set at minimum:
        name      — short identifier (used in logs and the plugin store)
        help_text — short Persian help string shown in the compact ``help``
                    output. Keep it to ~3-5 lines max.

    Optional:
        help_extra — extended Persian help shown via ``help <module>``.
                     Include examples, detailed explanations, edge cases.
    """

    # ── Public attributes (override in subclasses) ───────────────────────────
    name: str = ""
    help_text: str = ""
    help_extra: str = ""

    # ── Private state ────────────────────────────────────────────────────────
    def __init__(self, cfg: "AccountConfig") -> None:
        self.cfg: AccountConfig = cfg
        self._log: logging.Logger = get_logger(
            f"_modules_a{cfg.index}.{self.name or 'unknown'}"
        )

        # Track registered handlers per client so teardown() can remove them.
        # WeakKeyDictionary lets entries disappear when the client is GC'd,
        # preventing memory leaks across hot-reloads.
        self._handlers: WeakKeyDictionary[
            TelegramClient, list[tuple[EventBuilder, Callable]]
        ] = WeakKeyDictionary()

        # Cached me_id per client (populated on first _get_me_id call).
        self._me_cache: WeakKeyDictionary[TelegramClient, int] = WeakKeyDictionary()

    # ── Lifecycle (override in subclasses) ───────────────────────────────────

    def setup(self, client: TelegramClient) -> None:
        """
        Called once after the module is instantiated and a client is bound.

        Subclasses should register their event handlers here using
        ``self._add_handler(client, builder, callback)``.
        """
        pass

    def teardown(self, client: TelegramClient) -> None:
        """
        Called before hot-reload or shutdown.

        Removes all handlers registered via ``_add_handler`` for *client*.
        Subclasses can override to add extra cleanup but should call
        ``super().teardown(client)`` or manually remove handlers.
        """
        handlers = self._handlers.pop(client, [])
        for builder, cb in handlers:
            try:
                client.remove_event_handler(cb, builder)
            except Exception as exc:
                self._log.debug("teardown: remove_event_handler error: %s", exc)

    # ── Handler management ───────────────────────────────────────────────────

    def _add_handler(
        self,
        client: TelegramClient,
        event_builder: EventBuilder,
        callback: Callable,
    ) -> None:
        """
        Register *callback* for *event_builder* on *client* and remember it
        so ``teardown()`` can remove it later.

        Safe to call multiple times for the same (client, builder, callback);
        duplicates are skipped automatically.
        """
        bucket = self._handlers.setdefault(client, [])

        # Deduplicate: same (builder-type, callback) → skip.
        for existing_builder, existing_cb in bucket:
            if existing_cb is callback and type(existing_builder) is type(event_builder):
                return

        client.add_event_handler(callback, event_builder)
        bucket.append((event_builder, callback))

    # ── Cached self-ID ───────────────────────────────────────────────────────

    async def _get_me_id(self, client: TelegramClient) -> int | None:
        """
        Return the current user's Telegram ID (cached after first call).

        Returns ``None`` if the client is not authorized or any error occurs.
        """
        cached = self._me_cache.get(client)
        if cached is not None:
            return cached
        try:
            me = await client.get_me()
            if me is not None:
                self._me_cache[client] = me.id
                return me.id
        except Exception as exc:
            self._log.debug("_get_me_id error: %s", exc)
        return None

    # ── Safe message editing ─────────────────────────────────────────────────

    async def _safe_edit(self, message, text: str, **kwargs: Any) -> None:
        """
        Edit *message* to *text*, silently absorbing the most common errors
        (``MessageNotModifiedError``, ``MessageIdInvalidError``).

        Extra *kwargs* are forwarded to ``Message.edit`` (e.g. ``parse_mode``,
        ``link_preview``).
        """
        try:
            await message.edit(text, **kwargs)
        except errors.MessageNotModifiedError:
            pass
        except errors.MessageIdInvalidError:
            self._log.debug("_safe_edit: invalid message id (already deleted?)")
        except Exception as exc:
            self._log.debug("_safe_edit error: %s", exc)

    # ── Structured logging helpers ───────────────────────────────────────────

    def _log_info(self, msg: str, *args: Any) -> None:
        self._log.info("[%s] %s", self.cfg.index, msg % args if args else msg)

    def _log_warning(self, msg: str, *args: Any) -> None:
        self._log.warning("[%s] %s", self.cfg.index, msg % args if args else msg)

    def _log_error(self, msg: str, *args: Any) -> None:
        self._log.error("[%s] %s", self.cfg.index, msg % args if args else msg)

    def _log_debug(self, msg: str, *args: Any) -> None:
        self._log.debug("[%s] %s", self.cfg.index, msg % args if args else msg)

    # ── Repr ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name={self.name!r} "
            f"account={self.cfg.index}>"
        )