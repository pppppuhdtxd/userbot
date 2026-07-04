"""
modules/reaction_commands.py
════════════════════════════════════════════════════════════════
Reaction-Based Commands — Funnel Architecture (Zero Polling)

Architecture:
- Push-Based Detection: Only UpdateMessageReactions + UpdateEditMessage
- Zero Polling: No get_dialogs/get_messages calls
- Funnel Filtering: 5 gates with O(1) complexity
- Post-Startup Only: Ignores reactions from before module start
- Self-Only: Only processes reactions from the userbot account
- Environment Toggles: Configurable per chat type (bots/users/groups/channels)

Executes commands via Direct Module Invocation (compatible with clearer.py,
join_left.py, info_handler.py, whois_handler.py) instead of send_message
to avoid event-loop race conditions.

Features:
- Map emoji reactions to commands or text
- Push-based instant detection (no polling delay)
- Direct module invocation (faster, more reliable than send_message)
- Per-account configuration (reactions.json - auto-created)
- Self-reaction only
- Loop prevention (no duplicate execution)
- Environment-aware filtering (bots/users/groups/channels)

Commands (in Saved Messages):
- `reactions`              — show all configured reactions
- `reaction add <emoji> <command>`   — add a reaction mapping
- `reaction remove <emoji>`          — remove a reaction mapping
- `reaction clear`         — remove all reactions
════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional

from telethon import TelegramClient, events
from telethon.tl.types import (
    Message,
    MessageReactions,
    PeerChannel,
    PeerChat,
    PeerUser,
    ReactionEmoji,
    UpdateEditMessage,
    UpdateMessageReactions,
)

from core.exceptions import LoaderNotFoundError
from core.plugin_registry import loader_registry
from modules.base import Module

if TYPE_CHECKING:
    from config import AccountConfig

# Logging is provided by Module._log_* helpers; no module-level logger needed.
# The _MockEvent class below uses self._log_* via the owning module instance
# where possible; in the few places it operates standalone it falls back to
# the module-level logger created above only for truly unattached failures.
import logging
_mock_log = logging.getLogger(__name__)


# ── MockEvent: compatible with clearer.py & join_left.py handlers ────────────

class _MockEvent:
    """
    Mock event object that mimics a real Telethon event for direct
    module invocation. Designed to be fully compatible with:
    - clearer.py     → uses event.raw_text, event.message.id, event.edit/delete
    - join_left.py   → uses event.is_reply, get_reply_message(), event.edit
                       for progress updates, event.message.message for entities
    - info_handler   → uses get_reply_message()
    - whois_handler  → uses get_reply_message(), get_chat()

    Design decisions:
    - When target_msg is provided, event.is_reply = True and get_reply_message()
      returns it (needed by join_left, info, whois).
    - event.edit() creates a new progress message on first call, then edits
      that same message on subsequent calls. This mimics how join_left uses
      event.edit() for live progress updates.
    - event.message has .message and .text attributes set to raw_text so that
      join_left._collect_entities() can extract entities from the command text.
    - event.delete() deletes the progress message (if any).
    """

    def __init__(
        self,
        client: TelegramClient,
        chat_id: int,
        target_msg_id: int,
        raw_text: str,
        target_msg: Optional[Message] = None,
    ):
        self.client = client
        self.chat_id = chat_id
        self.raw_text = raw_text
        self._target_msg_id = target_msg_id
        self._target_msg = target_msg
        self.is_reply = target_msg is not None

        # Mock command message — needed by join_left._collect_entities()
        # which reads event.message.message for command text entities
        self.message = type('MockMessage', (object,), {
            'id': 0,
            'text': raw_text,
            'message': raw_text,
        })()

        # Progress message — created on first edit(), reused afterward
        self._progress_msg = None

    async def edit(self, text: str, **kwargs):
        """
        First call: send a new message as reply to target message.
        Subsequent calls: edit the same progress message.
        Returns the message object (which has its own .edit() method).
        """
        try:
            if self._progress_msg is None:
                self._progress_msg = await self.client.send_message(
                    self.chat_id, text,
                    reply_to=self._target_msg_id,
                    **kwargs
                )
                return self._progress_msg
            else:
                await self._progress_msg.edit(text, **kwargs)
                return self._progress_msg
        except Exception as exc:
            _mock_log.warning("MockEvent.edit failed: %s", exc)
            return self._progress_msg

    async def delete(self):
        """Delete the progress message if it exists."""
        if self._progress_msg:
            try:
                await self._progress_msg.delete()
            except Exception:
                pass

    async def respond(self, text: str, **kwargs):
        """Send a response message."""
        try:
            return await self.client.send_message(self.chat_id, text, **kwargs)
        except Exception as exc:
            _mock_log.warning("MockEvent.respond failed: %s", exc)
            return None

    async def get_reply_message(self):
        """Return the target message (the one that was reacted to)."""
        return self._target_msg

    async def get_chat(self):
        """Return the chat entity."""
        try:
            return await self.client.get_entity(self.chat_id)
        except Exception:
            return None


# ── Module ───────────────────────────────────────────────────────────────────

class ReactionCommands(Module):
    """Reaction-based command execution with Funnel Architecture."""

    name = "reaction_commands"

    # ── Environment Toggles (configure which chat types are active) ───────
    ENABLE_FOR_BOTS: bool = True      # Private chats with bots
    ENABLE_FOR_USERS: bool = True     # Private chats with users
    ENABLE_FOR_GROUPS: bool = False   # Basic groups and supergroups
    ENABLE_FOR_CHANNELS: bool = False # Channels (broadcast)

    def __init__(self, cfg: "AccountConfig") -> None:
        super().__init__(cfg)
        self._settings_file = cfg.settings_dir / "reactions.json"
        self._reactions: dict[str, str] = {}
        self._me_id: int | None = None
        self._processed: set[tuple[int, int, str]] = set()
        # Order-preserving FIFO list mirroring _processed, so the cleanup
        # step can evict the actual oldest entries instead of a random
        # slice of a set (sets have no defined iteration order).
        self._processed_order: list[tuple[int, int, str]] = []
        self._client: TelegramClient | None = None

        # Post-Startup Flag: only process reactions after module is fully ready
        self._is_ready: bool = False

        # Track background tasks created in setup() so teardown() can
        # cancel them on hot-reload, preventing a stale task from
        # overwriting _me_id / _is_ready on a freshly-reloaded instance.
        self._me_id_task: asyncio.Task | None = None
        self._ready_task: asyncio.Task | None = None

    def setup(self, client: TelegramClient) -> None:
        self._client = client
        self._ensure_settings_file()
        self._load_settings()

        # Register command handler
        self._add_handler(client, events.NewMessage(outgoing=True), self._on_command)

        # Method 1: UpdateMessageReactions (primary push route)
        self._add_handler(client, events.Raw(UpdateMessageReactions), self._on_reaction_update)

        # Method 2: UpdateEditMessage (secondary push route)
        self._add_handler(client, events.Raw(UpdateEditMessage), self._on_edit_update)

        # Cache self ID — task reference stored for teardown() cancellation.
        self._me_id_task = asyncio.create_task(
            self._cache_me_id(client), name=f"reaction_me_a{self.cfg.index}"
        )

        # Schedule readiness flag (after 3s to skip catch-up wave) —
        # task reference stored for teardown() cancellation.
        self._ready_task = asyncio.create_task(
            self._set_ready(), name=f"reaction_ready_a{self.cfg.index}"
        )

        self._log_info(
            "ReactionCommands ready (Funnel Architecture). %d reactions configured.",
            len(self._reactions),
        )

    def teardown(self, client: TelegramClient) -> None:
        if self._me_id_task is not None and not self._me_id_task.done():
            self._me_id_task.cancel()
        if self._ready_task is not None and not self._ready_task.done():
            self._ready_task.cancel()
        self._me_id_task = None
        self._ready_task = None

        self._processed.clear()
        self._processed_order.clear()
        self._me_id = None
        self._is_ready = False
        self._client = None
        super().teardown(client)

    # ── Post-Startup Readiness ────────────────────────────────────────────

    async def _set_ready(self) -> None:
        """Wait for catch-up wave to pass, then enable processing."""
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            return
        self._is_ready = True
        self._log_info("Module marked as ready — processing new reactions.")

    # ── Self ID cache ─────────────────────────────────────────────────────

    async def _cache_me_id(self, client: TelegramClient) -> None:
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
        try:
            if client.is_connected():
                me = await client.get_me()
                if me:
                    self._me_id = me.id
                    self._log_info("Cached self ID: %d", self._me_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log_error("Failed to cache me_id: %s", exc)

    # ── Environment Filter (Gate 2) ───────────────────────────────────────

    async def _check_environment_filter(self, peer_id, client: TelegramClient) -> bool:
        """
        Gate 2: Check if the environment (chat type) is enabled.

        Returns True if the environment is ENABLED (should process).
        Returns False if the environment is DISABLED (should drop).

        Uses O(1) peer_id type check + Entity Cache lookup (no API call).
        """
        if isinstance(peer_id, PeerChannel):
            # Channels and Supergroups
            return self.ENABLE_FOR_CHANNELS

        elif isinstance(peer_id, PeerChat):
            # Basic Groups (legacy)
            return self.ENABLE_FOR_GROUPS

        elif isinstance(peer_id, PeerUser):
            # Private chat — need to determine if bot or user
            # Use Telethon's entity cache (no API call if cached)
            try:
                entity = await client.get_entity(peer_id.user_id)
                is_bot = getattr(entity, 'bot', False)
            except Exception:
                # If we can't determine, assume user (safer default)
                is_bot = False

            if is_bot:
                return self.ENABLE_FOR_BOTS
            else:
                return self.ENABLE_FOR_USERS

        # Unknown peer type — drop
        return False

    # ── Settings I/O ──────────────────────────────────────────────────────

    def _ensure_settings_file(self) -> None:
        if not self._settings_file.exists():
            try:
                self._settings_file.parent.mkdir(parents=True, exist_ok=True)
                default_reactions = {"👌": "clear txt", "👍": "join"}
                self._settings_file.write_text(
                    json.dumps(default_reactions, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                self._log_info("Created default reactions.json")
            except Exception as exc:
                self._log_error("Failed to create reactions.json: %s", exc)

    def _load_settings(self) -> None:
        if not self._settings_file.exists():
            self._reactions = {}
            return

        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
            self._reactions = {str(k): str(v) for k, v in data.items()}
            self._log_info("Loaded %d reaction mappings", len(self._reactions))
        except Exception as exc:
            self._log_error("Failed to load reactions.json: %s", exc)
            self._reactions = {}

    def _save_settings(self) -> bool:
        """
        Atomically persist settings to disk via a temp-file + rename, so a
        crash mid-write can never leave a truncated reactions.json.
        """
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._reactions, ensure_ascii=False, indent=2)
            tmp_path = self._settings_file.with_suffix(".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self._settings_file)
            return True
        except Exception as exc:
            self._log_error("Failed to save reactions.json: %s", exc)
            return False

    # ── Command handler ───────────────────────────────────────────────────

    async def _on_command(self, event) -> None:
        text = (event.raw_text or "").strip()
        parts = text.split(maxsplit=1)

        if not parts:
            return

        cmd = parts[0].lower()

        if cmd in ("reactions", "reaction"):
            client = event.client
            me_id = await self._get_me_id(client)
            if event.chat_id != me_id:
                return

        if cmd == "reactions":
            await self._cmd_list(event)
        elif cmd == "reaction" and len(parts) > 1:
            subcmd = parts[1].split(maxsplit=1)
            if subcmd[0].lower() == "add" and len(subcmd) > 1:
                await self._cmd_add(event, subcmd[1])
            elif subcmd[0].lower() == "remove" and len(subcmd) > 1:
                await self._cmd_remove(event, subcmd[1].strip())
            elif subcmd[0].lower() == "clear":
                await self._cmd_clear(event)

    async def _cmd_list(self, event) -> None:
        if not self._reactions:
            await self._safe_edit(
                event,
                "ℹ️ هیچ reaction ای تنظیم نشده است.\n\n"
                "**نحوه استفاده:**\n"
                "`reaction add 👌 clear txt`\n"
                "`reaction add 👍 join`"
            )
            return

        lines = ["📋 **لیست Reaction های فعال:**\n"]
        for emoji, command in sorted(self._reactions.items()):
            lines.append(f"• `{emoji}` → `{command}`")

        lines.append(f"\n📊 **تعداد:** {len(self._reactions)} reaction")
        lines.append("\n💡 **نحوه استفاده:** روی یک پیام react کنید، دستور اجرا می‌شود.")

        await self._safe_edit(event, "\n".join(lines))

    async def _cmd_add(self, event, args: str) -> None:
        parts = args.split(maxsplit=1)

        if len(parts) < 2:
            await self._safe_edit(
                event,
                "❌ **فرمت نادرست**\n\n"
                "**استفاده:** `reaction add <emoji> <command>`\n"
                "**مثال:** `reaction add 👍 join`"
            )
            return

        emoji = parts[0].strip()
        command = parts[1].strip()

        if not emoji or not command:
            await self._safe_edit(event, "❌ emoji و command نمی‌توانند خالی باشند.")
            return

        self._reactions[emoji] = command

        if self._save_settings():
            await self._safe_edit(
                event,
                f"✅ **Reaction اضافه شد!**\n\n"
                f"• `{emoji}` → `{command}`\n\n"
                f"💡 حالا روی هر پیامی که `{emoji}` react کنید، `{command}` اجرا می‌شود."
            )
        else:
            await self._safe_edit(event, "❌ خطا در ذخیره تنظیمات.")

    async def _cmd_remove(self, event, emoji: str) -> None:
        if emoji not in self._reactions:
            await self._safe_edit(
                event,
                f"❌ Reaction `{emoji}` یافت نشد.\n\n"
                f"برای دیدن لیست: `reactions`"
            )
            return

        command = self._reactions.pop(emoji)

        if self._save_settings():
            await self._safe_edit(
                event,
                f"✅ **Reaction حذف شد!**\n\n"
                f"• `{emoji}` → `{command}`"
            )
        else:
            await self._safe_edit(event, "❌ خطا در ذخیره تنظیمات.")

    async def _cmd_clear(self, event) -> None:
        if not self._reactions:
            await self._safe_edit(event, "ℹ️ هیچ reaction ای برای حذف وجود ندارد.")
            return

        count = len(self._reactions)
        self._reactions.clear()

        if self._save_settings():
            await self._safe_edit(
                event,
                f"✅ **همه reaction ها پاک شدند!**\n\n"
                f"📊 تعداد حذف‌شده: {count}"
            )
        else:
            await self._safe_edit(event, "❌ خطا در ذخیره تنظیمات.")

    # ── Method 1: UpdateMessageReactions (Primary Push Route) ─────────────

    async def _on_reaction_update(self, event) -> None:
        """
        Handle UpdateMessageReactions — the primary push route.

        Applies Funnel Architecture:
        Gate 1: Post-Startup check (_is_ready)
        Gate 2: Environment filter (peer_id type)
        Gate 3: Self-Only filter (recent_reactions)
        Gate 4: Mapping filter (emoji in _reactions)
        Gate 5: Deduplication (_processed set)
        """
        # Gate 1: Post-Startup Filter
        if not self._is_ready:
            return

        if not self._reactions or self._me_id is None:
            return

        client = self._client
        if client is None:
            # teardown() has already run on this instance (hot-reload race);
            # there is nothing safe left to operate on.
            return

        # Extract peer_id from raw event
        peer_id = getattr(event, 'peer_id', None)
        if peer_id is None:
            return

        # Gate 2: Environment Filter
        if not await self._check_environment_filter(peer_id, client):
            return

        chat_id = getattr(event, 'chat_id', None)
        msg_id = getattr(event, 'msg_id', None)

        if chat_id is None or msg_id is None:
            return

        try:
            msg = await client.get_messages(chat_id, ids=msg_id)
            if msg:
                await self._process_reaction_update(msg)
        except Exception as exc:
            self._log_debug("Error fetching message for reaction: %s", exc)

    # ── Method 2: UpdateEditMessage (Secondary Push Route) ────────────────

    async def _on_edit_update(self, event) -> None:
        """
        Handle UpdateEditMessage — secondary push route.

        Telegram sometimes sends UpdateEditMessage instead of
        UpdateMessageReactions when reactions change.

        Only processes if message.reactions exists in the payload.
        """
        # Gate 1: Post-Startup Filter
        if not self._is_ready:
            return

        if not self._reactions or self._me_id is None:
            return

        client = self._client
        if client is None:
            return

        message = getattr(event, 'message', None)
        if not message:
            return

        # Only process if reactions exist in the payload
        if not hasattr(message, 'reactions') or not message.reactions:
            return

        # Extract peer_id from message
        peer_id = getattr(message, 'peer_id', None)
        if peer_id is None:
            # peer_id was missing on the message itself. We cannot safely
            # assume PeerUser here — the chat could just as easily be a
            # channel or basic group, and wrapping a channel/group ID in
            # PeerUser would make the environment filter misclassify it.
            # Resolve the real peer type via the client instead.
            chat_id = getattr(message, 'chat_id', None)
            if chat_id is None:
                return
            try:
                entity = await client.get_entity(chat_id)
            except Exception:
                return
            if getattr(entity, 'broadcast', False) or getattr(entity, 'megagroup', False):
                peer_id = PeerChannel(chat_id)
            elif hasattr(entity, 'participants_count') and not hasattr(entity, 'access_hash'):
                # Basic Chat objects don't carry access_hash; Channels do.
                peer_id = PeerChat(chat_id)
            else:
                peer_id = PeerUser(chat_id)

        if peer_id is None:
            return

        # Gate 2: Environment Filter
        if not await self._check_environment_filter(peer_id, client):
            return

        await self._process_reaction_update(message)

    # ── Core Reaction Processing (Gates 3-5) ──────────────────────────────

    async def _process_reaction_update(self, msg: Message) -> None:
        """
        Process a message for self-reactions.

        Applies Gates 3-5 of the Funnel:
        Gate 3: Self-Only filter (check recent_reactions for self._me_id)
        Gate 4: Mapping filter (emoji in self._reactions)
        Gate 5: Deduplication (check _processed set)
        """
        if not hasattr(msg, 'reactions') or not msg.reactions:
            return

        reactions = msg.reactions
        if not isinstance(reactions, MessageReactions):
            return

        recent_reactions = getattr(reactions, 'recent_reactions', [])
        if not recent_reactions:
            return

        chat_id = msg.chat_id
        msg_id = msg.id

        # Gate 3: Self-Only Filter
        # Build set of emojis that self has reacted with
        self_emojis: set[str] = set()

        for recent in recent_reactions:
            peer_id = getattr(recent, 'peer_id', None)
            if not peer_id or not isinstance(peer_id, PeerUser):
                continue

            user_id = getattr(peer_id, 'user_id', None)
            if user_id != self._me_id:
                continue

            recent_reaction = getattr(recent, 'reaction', None)
            if not recent_reaction or not isinstance(recent_reaction, ReactionEmoji):
                continue

            emoji_str = getattr(recent_reaction, 'emoticon', None)
            if emoji_str:
                self_emojis.add(emoji_str)

        if not self_emojis:
            return

        # Gate 4: Mapping Filter + Gate 5: Deduplication
        for emoji_str in self_emojis:
            # Gate 4: Check if emoji is mapped to a command
            if emoji_str not in self._reactions:
                continue

            # Gate 5: Check deduplication
            action_key = (chat_id, msg_id, emoji_str)
            if action_key in self._processed:
                continue

            # Mark as processed — add to both the set (fast lookup) and the
            # FIFO list (preserves insertion order for correct eviction).
            self._processed.add(action_key)
            self._processed_order.append(action_key)

            command_text = self._reactions[emoji_str]

            self._log_debug(
                "✅ Reaction detected: emoji=%s, chat=%d, msg=%d, action=%s",
                emoji_str, chat_id, msg_id, command_text
            )

            # Execute command DIRECTLY
            try:
                await self._execute_command_directly(chat_id, msg_id, command_text, msg)
            except Exception as exc:
                self._log_error("Failed to execute command: %s", exc)

        # Cleanup _processed to prevent unbounded memory growth.
        # Evict the OLDEST entries first using the FIFO order list — a plain
        # set has no defined iteration order, so slicing list(set) would
        # discard a random 500 entries rather than the oldest 500, silently
        # breaking deduplication for recently-processed reactions.
        if len(self._processed_order) > 1000:
            overflow = len(self._processed_order) - 500
            to_evict = self._processed_order[:overflow]
            self._processed_order = self._processed_order[overflow:]
            for key in to_evict:
                self._processed.discard(key)

    # ── Direct command execution ──────────────────────────────────────────

    async def _execute_command_directly(
        self,
        chat_id: int,
        target_msg_id: int,
        command_text: str,
        target_msg: Optional[Message] = None,
    ) -> None:
        """
        Execute a command by directly invoking the target module's handler.
        Uses a _MockEvent that is fully compatible with clearer.py,
        join_left.py, info_handler.py, and whois_handler.py.

        Falls back to send_message if direct invocation is unavailable.
        """
        try:
            loader = loader_registry.get(self.cfg.index)
        except LoaderNotFoundError:
            self._log_warning(
                "No loader registered for account #%d — cannot execute '%s'.",
                self.cfg.index, command_text,
            )
            return

        client = loader.client

        if not client or not client.is_connected():
            self._log_warning("Client not connected")
            return

        command_parts = command_text.strip().split()
        if not command_parts:
            return

        command_name = command_parts[0].lower()
        self._log_debug("Executing directly: %s", command_text)

        # ── clear (compatible with clearer.py) ────────────────────────────
        if command_name == "clear":
            clearer_instance = loader.get_module("clearer")
            if clearer_instance is not None:
                try:
                    mock_event = _MockEvent(client, chat_id, target_msg_id, command_text)
                    await clearer_instance._on_command(mock_event)
                    self._log_debug("✅ Invoked clearer directly")
                    return
                except Exception as exc:
                    self._log_error("Direct clearer invocation failed: %s", exc)

        # ── join / left (compatible with join_left.py) ────────────────────
        elif command_name in ("join", "left"):
            join_left_instance = loader.get_module("join_left")
            if join_left_instance is not None:
                try:
                    # Fetch the target message if not already provided
                    if target_msg is None:
                        target_msg = await client.get_messages(chat_id, ids=target_msg_id)

                    mock_event = _MockEvent(
                        client, chat_id, target_msg_id, command_text,
                        target_msg=target_msg
                    )
                    # Call _dispatch which routes to _handle_join or _handle_left
                    await join_left_instance._dispatch(mock_event)
                    self._log_debug("✅ Invoked join_left.%s directly", command_name)
                    return
                except Exception as exc:
                    self._log_error("Direct join_left invocation failed: %s", exc)

        # ── info (compatible with info_handler.py) ────────────────────────
        elif command_name == "info":
            info_instance = loader.get_module("info_handler")
            if info_instance is not None:
                try:
                    if target_msg is None:
                        target_msg = await client.get_messages(chat_id, ids=target_msg_id)
                    mock_event = _MockEvent(
                        client, chat_id, target_msg_id, command_text,
                        target_msg=target_msg
                    )
                    await info_instance._on_command(mock_event)
                    self._log_debug("✅ Invoked info_handler directly")
                    return
                except Exception as exc:
                    self._log_error("Direct info_handler invocation failed: %s", exc)

        # ── whois (compatible with whois_handler.py) ──────────────────────
        elif command_name == "whois":
            whois_instance = loader.get_module("whois_handler")
            if whois_instance is not None:
                try:
                    if target_msg is None:
                        target_msg = await client.get_messages(chat_id, ids=target_msg_id)
                    mock_event = _MockEvent(
                        client, chat_id, target_msg_id, command_text,
                        target_msg=target_msg
                    )
                    await whois_instance._on_command(mock_event)
                    self._log_debug("✅ Invoked whois_handler directly")
                    return
                except Exception as exc:
                    self._log_error("Direct whois_handler invocation failed: %s", exc)

        # ── Fallback: send as message ─────────────────────────────────────
        self._log_warning(
            "Direct invocation not available for '%s', falling back to send_message",
            command_name
        )
        try:
            await client.send_message(
                chat_id,
                command_text,
                reply_to=target_msg_id
            )
            self._log_debug("Sent command as message (fallback)")
        except Exception as exc:
            self._log_error("Failed to send command: %s", exc)


# ── Help Texts (در انتهای ماژول طبق قوانین) ─────────────────────────────────

help_text = (
    "• `reactions` | لیست reaction های تنظیم‌شده\n"
    "• `reaction add <emoji> <command>` | افزودن mapping\n"
    "• `reaction remove <emoji>` | حذف یک mapping\n"
    "• `reaction clear` | حذف همه mapping ها\n"
)

help_extra = (
    "Reaction Commands - اجرای دستورات با ری‌اکشن\n\n"
    "دستورات اصلی:\n"
    "• `reactions` | نمایش لیست همه reaction های فعال\n"
    "• `reaction add <emoji> <command>` | افزودن mapping جدید\n"
    "• `reaction remove <emoji>` | حذف یک mapping\n"
    "• `reaction clear` | حذف همه mapping ها\n\n"
    "روش‌های تشخیص ری‌اکشن:\n"
    "این ماژول از ۳ روش برای تشخیص استفاده می‌کند:\n"
    "• Method 1 | `UpdateMessageReactions` برای پیام‌های خودتان\n"
    "• Method 2 | `UpdateEditMessage` که گاهی تلگرام این را می‌فرستد\n"
    "• Method 3 | Smart Polling برای پیام‌های ربات‌ها و سایر موارد\n\n"
    "اجرای مستقیم ماژول‌ها:\n"
    "دستورات به‌جای `send_message` مستقیماً اجرا می‌شوند:\n"
    "• `clear` | اجرای مستقیم `clearer.py`\n"
    "• `join` / `left` | اجرای مستقیم `join_left.py`\n"
    "• `info` | اجرای مستقیم `info_handler.py`\n"
    "• `whois` | اجرای مستقیم `whois_handler.py`\n\n"
    "مثال‌ها:\n"
    "• `reaction add 👌 clear txt` | پاک کردن متن‌ها با ری‌اکشن 👌\n"
    "• `reaction add 👍 join` | عضویت با ری‌اکشن 👍\n"
    "• `reaction add 🔍 info` | نمایش اطلاعات پیام با 🔍\n"
    "• `reaction add 👤 whois` | نمایش اطلاعات فرستنده با 👤\n"
    "• `reaction remove 👌` | حذف mapping 👌\n"
    "• `reaction clear` | حذف همه mapping ها\n\n"
    "نکات مهم:\n"
    "• فقط ری‌اکشن‌های خودتان (self-reaction) تشخیص داده می‌شوند\n"
    "• تنظیمات در `reactions.json` ذخیره می‌شوند\n"
    "• polling هوشمند با آگاهی از FloodWait (فاصله پایه ۵ ثانیه)\n"
    "• Loop prevention از اجرای تکراری جلوگیری می‌کند\n"
    "• اگر اجرای مستقیم ممکن نباشد، به `send_message` fallback می‌شود\n"
)

ReactionCommands.help_text = help_text
ReactionCommands.help_extra = help_extra


def create_module(cfg: "AccountConfig") -> Module:
    return ReactionCommands(cfg)