"""
modules/join_left.py
════════════════════════════════════════════════════════════════
Join / Left / Folder / List / AutoLeave / Join Delay

Commands:
- `join` (reply to a message with links/usernames/IDs)
  → Join all found chats with live progress updates
  → 4-layer anti-FloodWait: smart resolution, risk-based delays,
    adaptive backoff, human-like batching
  → Add joined chats to 'joined' folder and track join time

- `join delay <seconds>`
  → Set fixed delay between joins (0 = restore smart throttling)

- `join mode fast|safe|human`
  → fast  : no smart throttling, no batching (respects delay setting)
  → safe  : Layer 1 + Layer 2 — smart resolution + risk delays [DEFAULT]
  → human : all 4 layers — adds adaptive backoff + batch cooldowns

- `left` (reply to a message with links/usernames/IDs)
  → Leave all found chats
  → Remove left chats from the 'joined' folder automatically
  → Delete command message on success

- `folder` (Saved Messages only)
  → Create / reset the 'joined' folder

- `list` (Saved Messages only)
  → Show all chats currently in the 'joined' folder

- `autoleave <days>`
  → Automatically leave joined chats after N days
  → Syncs existing folder chats on activation

- `autoleave off`
  → Disable auto-leave

- `autoleave status`
  → Show current auto-leave status and tracked chats

Anti-FloodWait Layers:
  Layer 1 — Smart Link Resolution
    Before joining via invite hash, calls CheckChatInviteRequest to peek
    at the destination. If the chat has a public username, switches to
    JoinChannelRequest (much more lenient rate limit). Cache persisted to
    join_left_invite_cache.json across restarts.

  Layer 2 — Risk-Based Delays (safe + human mode)
    Different join operations carry different FloodWait risk. Delays are
    applied proportionally to risk when no manual `join delay` is set.
      username / channel_id / numeric_id : 2–3s
      invite resolved to username         : 4s
      invite direct (private group hash)  : 8s

  Layer 3 — Adaptive FloodWait Response (safe + human mode)
    FloodWait duration signals how aggressively Telegram is rate-limiting.
    Short (<30s) → wait + 1.5× delay multiplier
    Medium (<300s) → wait + extra cooldown + 3.0× multiplier
    Heavy (≥300s)  → long wait + 5.0× multiplier + all-joins pause
    Multiplier decays 10% per successful join back toward 1.0.

  Layer 4 — Batch & Cooldown / Human Pattern (human mode only)
    After BATCH_SIZE joins: short cooldown (30s ± jitter).
    After BATCHES_BEFORE_LONG batches: long cooldown (120s ± jitter).
    ±20% random jitter on all delays to avoid predictable timing.

Other Features:
- Auto-delete command output after 5 seconds
- Silent logging (DEBUG level for command execution)
- Instance-level folder cache (no shared state between accounts)
- Defensive settings loading (handles corrupted JSON gracefully)
- Auto-removal of inaccessible chats from tracking
- Auto-sync of existing folder chats when autoleave is activated
════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
import time
from typing import TYPE_CHECKING

from telethon import TelegramClient, errors, events
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    DeleteHistoryRequest,
    GetDialogFiltersRequest,
    ImportChatInviteRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.types import (
    Channel,
    Chat,
    DialogFilter,
    InputPeerSelf,
    KeyboardButtonUrl,
    ReplyInlineMarkup,
    TextWithEntities,
    User,
)

from helpers.utils import safe_delete
from modules.base import Module

if TYPE_CHECKING:
    from config import AccountConfig

# Module-level logger — used only by the free functions below (folder cache
# helpers etc.) that live outside the Module class and therefore have no
# access to self._log_*. All in-class logging uses self._log_* instead.
log = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

_JOINED_FOLDER_NAME = "joined"
_FOLDER_CACHE_TTL   = 30.0   # seconds
_EDIT_THROTTLE      = 2.5    # seconds between message edits to avoid FloodWait
_AUTO_DELETE_DELAY  = 5.0    # seconds before auto-deleting command output

# ── Anti-FloodWait constants ──────────────────────────────────────────────────

# Layer 2: risk-based delay per join type (seconds)
# Applied when join_mode is 'safe' or 'human' AND user_delay == 0
_SMART_DELAYS: dict[str, float] = {
    "username":             2.0,   # safest — public channel by @username
    "channel_id":           3.0,   # medium — resolved from t.me/c/ID/msg link
    "numeric_id":           3.0,   # medium — raw numeric ID
    "invite_with_username": 4.0,   # safe-ish — invite resolved to @username
    "invite_direct":        8.0,   # riskiest — private group via raw hash
}

# Layer 3: adaptive FloodWait multiplier bounds
_ADAPTIVE_MAX_MULT  = 20.0   # never exceed 20× the base smart delay
_ADAPTIVE_DECAY     = 0.90   # per-successful-join decay factor toward 1.0

# Layer 4: batch / human-pattern parameters
_BATCH_SIZE          = 5     # joins per batch before a short rest
_COOLDOWN_SHORT      = 30.0  # seconds: rest after each completed batch
_COOLDOWN_LONG       = 120.0 # seconds: rest after every Nth batch
_BATCHES_BEFORE_LONG = 3     # take a long rest after this many batches
_JITTER_FACTOR       = 0.20  # ±20% random jitter on all delays (human mode)

# Invite cache TTL: 6 hours (invite destinations rarely change)
_INVITE_CACHE_TTL   = 6 * 3600


# ── Entity extraction ─────────────────────────────────────────────────────────

def extract_telegram_entities(text: str | None) -> list[tuple[str, str | int]]:
    """
    Extract Telegram chat identifiers from free-form text.

    Returns list of (type, value) tuples where type is one of:
    'channel_id', 'username', 'invite_link', 'numeric_id'
    """
    if not text:
        return []

    entities: list[tuple[str, str | int]] = []

    # Private channel links: t.me/c/1234567890/123
    for m in re.finditer(
        r'https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.org)/c/(\d{10,15})/\d+',
        text, re.IGNORECASE,
    ):
        entities.append(('channel_id', int(m.group(1))))

    # Usernames: @name or t.me/name
    for m in re.finditer(
        r'(?:@|(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.org)/)'
        r'([a-zA-Z0-9_]{5,32})(?![a-zA-Z0-9_/])',
        text, re.IGNORECASE,
    ):
        username = m.group(1)
        if username.lower() in ('joinchat', 'c', 'proxy', 's', 'addstickers'):
            continue
        if not username.lower().endswith('bot'):
            entities.append(('username', username))

    # Invite links: t.me/+xxx or t.me/joinchat/xxx
    for m in re.finditer(
        r'(https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.org)/(?:joinchat/|\+))'
        r'([a-zA-Z0-9_-]{10,64})',
        text, re.IGNORECASE,
    ):
        entities.append(('invite_link', m.group(1) + m.group(2)))

    # Numeric IDs
    for m in re.finditer(r'\b(\d{9,14})\b', text):
        entities.append(('numeric_id', int(m.group(1))))

    return entities


def _extract_invite_hash(identifier: str) -> str | None:
    m = re.search(r'(?:\+|joinchat/)([a-zA-Z0-9_-]{10,64})$', str(identifier))
    return m.group(1) if m else None


# ── Folder cache helpers ─────────────────────────────────────────────────────
# These functions accept a cache dict as a parameter so that each JoinLeft
# instance can maintain its own isolated cache.

async def _get_folders(
    client: TelegramClient,
    cache: dict[int, tuple[float, list[DialogFilter]]],
) -> list[DialogFilter]:
    """Fetch dialog filters with TTL-based caching."""
    cid = id(client)
    now = time.monotonic()
    hit = cache.get(cid)
    if hit and (now - hit[0]) < _FOLDER_CACHE_TTL:
        return hit[1]

    result = await client(GetDialogFiltersRequest())
    filters = getattr(result, "filters", result)
    folders = [f for f in filters if isinstance(f, DialogFilter)]
    cache[cid] = (now, folders)
    return folders


def _invalidate_folder_cache(
    client: TelegramClient,
    cache: dict[int, tuple[float, list[DialogFilter]]],
) -> None:
    """Remove the cache entry for a specific client."""
    cache.pop(id(client), None)


# ── Folder helpers ────────────────────────────────────────────────────────────

def _folder_title(folder: DialogFilter) -> str:
    t = folder.title
    return t if isinstance(t, str) else getattr(t, "text", str(t))


def _peer_id(peer) -> int | None:
    return (
        getattr(peer, "user_id",    None)
        or getattr(peer, "chat_id",    None)
        or getattr(peer, "channel_id", None)
    )


def _find_joined_folder(folders: list[DialogFilter]) -> DialogFilter | None:
    return next(
        (f for f in folders if _folder_title(f).lower() == _JOINED_FOLDER_NAME),
        None,
    )


async def _create_joined_folder(
    client: TelegramClient,
    account_index: int,
    cache: dict[int, tuple[float, list[DialogFilter]]],
    extra_peers: list | None = None,
) -> DialogFilter:
    folders      = await _get_folders(client, cache)
    existing_ids = {f.id for f in folders}
    new_id       = 2
    while new_id in existing_ids:
        new_id += 1

    saved      = InputPeerSelf()
    inc_peers  = [saved] + (extra_peers or [])

    new_folder = DialogFilter(
        id               = new_id,
        title            = TextWithEntities(text=_JOINED_FOLDER_NAME, entities=[]),
        pinned_peers     = [saved],
        include_peers    = inc_peers,
        exclude_peers    = [],
        contacts         = False,
        non_contacts     = False,
        groups           = False,
        broadcasts       = False,
        bots             = False,
        exclude_muted    = False,
        exclude_read     = False,
        exclude_archived = False,
    )

    await client(UpdateDialogFilterRequest(id=new_id, filter=new_folder))
    _invalidate_folder_cache(client, cache)
    log.debug(
        "[Account%d] Created '%s' folder (id=%d) with %d peer(s).",
        account_index, _JOINED_FOLDER_NAME, new_id, len(inc_peers),
    )
    return new_folder


async def _ensure_joined_folder_exists(
    client: TelegramClient,
    account_index: int,
    cache: dict[int, tuple[float, list[DialogFilter]]],
) -> DialogFilter:
    folders = await _get_folders(client, cache)
    folder  = _find_joined_folder(folders)
    if folder:
        return folder
    log.debug("[Account%d] '%s' folder not found — creating.", account_index, _JOINED_FOLDER_NAME)
    return await _create_joined_folder(client, account_index, cache)


async def _add_peers_to_joined_folder(
    client: TelegramClient,
    entities: list,
    account_index: int,
    cache: dict[int, tuple[float, list[DialogFilter]]],
) -> int:
    if not entities:
        return 0

    new_peers: list   = []
    new_ids: set[int] = set()

    for entity in entities:
        try:
            ip  = await client.get_input_entity(entity)
            pid = _peer_id(ip)
            if pid and pid not in new_ids:
                new_peers.append(ip)
                new_ids.add(pid)
        except Exception as exc:
            log.debug(
                "[Account%d] Could not resolve peer for %s: %s",
                account_index, getattr(entity, "id", entity), exc,
            )

    if not new_peers:
        return 0

    folders = await _get_folders(client, cache)
    folder  = _find_joined_folder(folders)

    if not folder:
        await _create_joined_folder(client, account_index, cache, extra_peers=new_peers)
        return len(new_peers)

    existing_peers = list(folder.include_peers or [])
    existing_ids   = {_peer_id(p) for p in existing_peers} - {None}

    added = 0
    for peer in new_peers:
        pid = _peer_id(peer)
        if pid not in existing_ids:
            existing_peers.append(peer)
            existing_ids.add(pid)
            added += 1

    if added == 0:
        return 0

    folder.include_peers = existing_peers
    await client(UpdateDialogFilterRequest(id=folder.id, filter=folder))
    _invalidate_folder_cache(client, cache)
    log.debug("[Account%d] Added %d peer(s) to '%s' folder.", account_index, added, _JOINED_FOLDER_NAME)
    return added


async def _remove_peers_from_joined_folder(
    client: TelegramClient,
    entities: list,
    account_index: int,
    cache: dict[int, tuple[float, list[DialogFilter]]],
) -> int:
    if not entities:
        return 0

    folders = await _get_folders(client, cache)
    folder  = _find_joined_folder(folders)
    if not folder:
        return 0

    remove_ids: set[int] = set()
    for entity in entities:
        try:
            ip  = await client.get_input_entity(entity)
            pid = _peer_id(ip)
            if pid:
                remove_ids.add(pid)
        except Exception:
            pass

    if not remove_ids:
        return 0

    original    = list(folder.include_peers or [])
    kept        = [p for p in original if _peer_id(p) not in remove_ids]
    removed_cnt = len(original) - len(kept)

    if removed_cnt == 0:
        return 0

    folder.include_peers = kept
    await client(UpdateDialogFilterRequest(id=folder.id, filter=folder))
    _invalidate_folder_cache(client, cache)
    log.debug(
        "[Account%d] Removed %d peer(s) from '%s' folder.",
        account_index, removed_cnt, _JOINED_FOLDER_NAME,
    )
    return removed_cnt


async def _leave_and_reset_joined_folder(
    client: TelegramClient,
    account_index: int,
    cache: dict[int, tuple[float, list[DialogFilter]]],
) -> tuple[int, int]:
    folders      = await _get_folders(client, cache)
    folder       = _find_joined_folder(folders)
    left_count   = 0
    failed_count = 0

    if folder:
        for peer in list(folder.include_peers or []):
            if isinstance(peer, InputPeerSelf):
                continue
            pid = _peer_id(peer)
            if not pid:
                continue
            try:
                entity = await client.get_entity(peer)
                if isinstance(entity, Channel):
                    await client(LeaveChannelRequest(entity))
                    left_count += 1
                elif isinstance(entity, (Chat, User)):
                    await client(DeleteHistoryRequest(peer=entity, just_clear=False, max_id=0))
                    left_count += 1
            except errors.UserNotParticipantError:
                pass
            except Exception as exc:
                failed_count += 1
                log.debug("[Account%d] Could not leave peer id=%s: %s", account_index, pid, exc)

        await client(UpdateDialogFilterRequest(id=folder.id))
        _invalidate_folder_cache(client, cache)

    await _create_joined_folder(client, account_index, cache)
    return left_count, failed_count


# ── Module ────────────────────────────────────────────────────────────────────

class JoinLeft(Module):
    """Join/leave chats with folder management and auto-leave."""

    name = "join_left"

    def __init__(self, cfg: "AccountConfig") -> None:
        super().__init__(cfg)
        self._settings_file = cfg.settings_dir / "join_left.json"
        self._settings_lock = asyncio.Lock()
        self._settings: dict = {
            "delay": 0.0,
            "join_mode": "safe",        # fast | safe | human
            "auto_leave_days": None,
            "joined_chats": {}          # str(chat_id) -> ISO timestamp
        }
        self._auto_leave_task: asyncio.Task | None = None

        # Track auto-delete tasks so teardown() can cancel any still-pending
        # ones on hot-reload, preventing them from calling .delete() on a
        # message tied to a disconnected/stale client.
        self._pending_delete_tasks: set[asyncio.Task] = set()

        # Instance-level folder cache — each account has its own isolated cache.
        # Key: id(client) → (timestamp, list[DialogFilter])
        self._folder_cache: dict[int, tuple[float, list[DialogFilter]]] = {}

        # Layer 1: persistent invite hash → username cache
        # Structure: {hash: {"username": str | null, "checked_at": ISO}}
        # "username": null means we already checked and it's a private group
        self._invite_cache: dict[str, dict] = {}
        self._invite_cache_file = cfg.settings_dir / "join_left_invite_cache.json"
        self._invite_cache_lock = asyncio.Lock()

    def setup(self, client: TelegramClient) -> None:
        self._add_handler(client, events.NewMessage(outgoing=True), self._dispatch)
        self._load_settings_sync()
        self._load_invite_cache_sync()

        self._auto_leave_task = asyncio.create_task(
            self._auto_leave_loop(client),
            name=f"auto_leave_a{self.cfg.index}"
        )
        self._log_info("JoinLeft ready (mode=%s).", self._settings.get("join_mode", "safe"))

    def teardown(self, client: TelegramClient) -> None:
        if self._auto_leave_task and not self._auto_leave_task.done():
            self._auto_leave_task.cancel()
        self._auto_leave_task = None

        # Cancel any in-flight auto-delete tasks so they don't fire against
        # a stale client after this instance has been torn down.
        for t in list(self._pending_delete_tasks):
            if not t.done():
                t.cancel()
        self._pending_delete_tasks.clear()

        # Clear the folder cache on teardown
        self._folder_cache.clear()
        super().teardown(client)

    # ── Settings I/O ──────────────────────────────────────────────────────────

    def _load_settings_sync(self) -> None:
        """
        Load settings from disk with defensive validation.

        Handles corrupted files gracefully:
        - Missing file → use defaults
        - Invalid JSON → use defaults
        - joined_chats not a dict → reset to empty dict
        """
        if not self._settings_file.exists():
            return
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
            self._settings["delay"] = float(data.get("delay", 0.0))
            self._settings["auto_leave_days"] = data.get("auto_leave_days")

            # join_mode: validate against known values
            raw_mode = data.get("join_mode", "safe")
            self._settings["join_mode"] = raw_mode if raw_mode in ("fast", "safe", "human") else "safe"

            # Defensive: ensure joined_chats is a dict
            joined = data.get("joined_chats", {})
            if not isinstance(joined, dict):
                self._log_warning(
                    "joined_chats was %s (not a dict), resetting to empty",
                    type(joined).__name__,
                )
                joined = {}
            self._settings["joined_chats"] = joined

        except Exception as exc:
            self._log_error("Settings load error (using defaults): %s", exc)

    async def _save_settings(self) -> None:
        """
        Atomically persist settings to disk via a temp-file + rename, so a
        crash mid-write can never leave a truncated join_left.json.
        """
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._settings, ensure_ascii=False, indent=4)
            tmp_path = self._settings_file.with_suffix(".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self._settings_file)
        except Exception as exc:
            self._log_error("Settings save error: %s", exc)

    # ── Invite Cache I/O (Layer 1) ────────────────────────────────────────────

    def _load_invite_cache_sync(self) -> None:
        """Load persistent invite cache from disk. Silently ignores errors."""
        if not self._invite_cache_file.exists():
            return
        try:
            data = json.loads(self._invite_cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._invite_cache = data
                self._log_debug(
                    "[Account%d] Loaded %d invite cache entries.",
                    self.cfg.index, len(self._invite_cache),
                )
        except Exception as exc:
            self._log_debug("[Account%d] Invite cache load error (ignored): %s", self.cfg.index, exc)
            self._invite_cache = {}

    async def _save_invite_cache(self) -> None:
        """Atomically persist invite cache to disk."""
        try:
            self._invite_cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._invite_cache, ensure_ascii=False, indent=2)
            tmp_path = self._invite_cache_file.with_suffix(".cache.tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(self._invite_cache_file)
        except Exception as exc:
            self._log_debug("[Account%d] Invite cache save error: %s", self.cfg.index, exc)

    async def _prune_expired_invite_cache(self) -> None:
        """Remove invite cache entries older than _INVITE_CACHE_TTL. Call occasionally."""
        now_ts = time.time()
        expired = [
            h for h, v in self._invite_cache.items()
            if (now_ts - v.get("ts", 0)) > _INVITE_CACHE_TTL
        ]
        if expired:
            async with self._invite_cache_lock:
                for h in expired:
                    self._invite_cache.pop(h, None)
            self._log_debug(
                "[Account%d] Pruned %d expired invite cache entries.", self.cfg.index, len(expired)
            )

    # ── Auto-delete helper ────────────────────────────────────────────────────

    async def _auto_delete_after_delay(self, message, delay: float = _AUTO_DELETE_DELAY) -> None:
        """Schedule message deletion after a delay."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            await message.delete()
        except Exception:
            pass

    def _track_delete_task(self, message, delay: float = _AUTO_DELETE_DELAY) -> asyncio.Task:
        """
        Create an auto-delete task and register it in _pending_delete_tasks
        so teardown() can cancel it on hot-reload, then auto-remove itself
        from the set once it completes (success, failure, or cancellation).
        """
        task = asyncio.create_task(
            self._auto_delete_after_delay(message, delay),
            name=f"join_left_autodel_a{self.cfg.index}",
        )
        self._pending_delete_tasks.add(task)
        task.add_done_callback(self._pending_delete_tasks.discard)
        return task

    async def _safe_edit_with_auto_delete(
        self,
        event,
        text: str,
        delay: float = _AUTO_DELETE_DELAY,
        **kwargs
    ) -> None:
        """Edit message and schedule auto-deletion after delay."""
        await self._safe_edit(event, text, **kwargs)
        self._track_delete_task(event, delay)

    # ── Folder sync helper ────────────────────────────────────────────────────

    async def _sync_folder_to_tracking(self, client: TelegramClient) -> int:
        """
        Sync chats from the 'joined' Telegram folder into joined_chats tracking.

        For each chat in the folder that isn't already tracked, adds it with
        a timestamp of (now - days - 1) so it becomes eligible for auto-leave
        on the next check cycle.

        Returns the number of newly tracked chats.
        """
        days = self._settings.get("auto_leave_days")
        if days is None:
            return 0

        if not client.is_connected():
            return 0

        try:
            folders = await _get_folders(client, self._folder_cache)
            folder = _find_joined_folder(folders)
            if not folder:
                return 0

            peers = [p for p in (folder.include_peers or []) if not isinstance(p, InputPeerSelf)]
            if not peers:
                return 0

            # Timestamp to assign: old enough to trigger auto-leave immediately
            old_timestamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days + 1)
            old_iso = old_timestamp.isoformat()

            added = 0
            async with self._settings_lock:
                for peer in peers:
                    pid = _peer_id(peer)
                    if pid is None:
                        continue

                    chat_id_str = str(pid)
                    if chat_id_str not in self._settings["joined_chats"]:
                        self._settings["joined_chats"][chat_id_str] = old_iso
                        added += 1

                if added > 0:
                    await self._save_settings()

            if added > 0:
                self._log_debug(
                    "[Account%d] Synced %d existing folder chats into tracking (eligible for auto-leave).",
                    self.cfg.index, added,
                )

            return added

        except Exception as exc:
            self._log_debug(
                "[Account%d] Folder sync failed: %s",
                self.cfg.index, exc,
            )
            return 0

    # ── Layer 1: Smart Invite Resolution ─────────────────────────────────────

    async def _resolve_invite_to_username(
        self,
        client: TelegramClient,
        invite_hash: str,
    ) -> str | None:
        """
        Layer 1: Try to resolve an invite hash to a public @username.

        Uses CheckChatInviteRequest (much cheaper rate-limit than
        ImportChatInviteRequest) to peek at the destination chat. If the
        destination has a public username, we can join via JoinChannelRequest
        instead of ImportChatInviteRequest, dramatically reducing FloodWait risk.

        Results are cached (TTL: 6 hours) and persisted across restarts in
        join_left_invite_cache.json.

        Returns:
            str  — the username (without @) if public username found
            None — private group / no username / check failed
        """
        # ── Check in-memory cache first ──
        cached = self._invite_cache.get(invite_hash)
        if cached is not None:
            # Cache entry: {"username": str|null, "ts": float}
            cached_ts = cached.get("ts", 0)
            if (time.time() - cached_ts) < _INVITE_CACHE_TTL:
                result = cached.get("username")
                self._log_debug(
                    "[Account%d] Invite cache HIT for hash %.8s...: username=%s",
                    self.cfg.index, invite_hash, result,
                )
                return result  # None means "known private"
            # Expired — fall through to re-check

        self._log_debug(
            "[Account%d] Invite cache MISS for hash %.8s..., calling CheckChatInviteRequest",
            self.cfg.index, invite_hash,
        )

        try:
            result = await client(CheckChatInviteRequest(invite_hash))
        except errors.InviteHashInvalidError:
            # Invalid/expired link — cache as private so we don't retry
            async with self._invite_cache_lock:
                self._invite_cache[invite_hash] = {"username": None, "ts": time.time()}
            return None
        except errors.UserAlreadyParticipantError:
            # Already a member — we'll handle this in the join logic
            async with self._invite_cache_lock:
                self._invite_cache[invite_hash] = {"username": None, "ts": time.time()}
            return None
        except Exception as exc:
            # Any other error (FloodWait on check itself, network, etc.)
            # Don't cache failures so we retry later
            self._log_debug(
                "[Account%d] CheckChatInviteRequest failed for %.8s...: %s",
                self.cfg.index, invite_hash, exc,
            )
            return None

        # ── Extract username from result ──
        # Handle three result types:
        #   ChatInviteAlready  — user is already a member; has .chat
        #   ChatInvitePeek     — peek at a chat we can see; has .chat
        #   ChatInvite         — full invite info; may have embedded Channel
        username: str | None = None

        # First, look for a Channel or Chat object with a .username attribute
        # in any known location. We check multiple attribute names because
        # Telethon's generated types vary with API layer version.
        for attr in ("chat", "channel"):
            obj = getattr(result, attr, None)
            if obj is not None and isinstance(obj, (Channel, Chat)):
                username = getattr(obj, "username", None) or None
                break

        # For ChatInvite with no embedded entity, check the `public` flag.
        # If it's public, it MUST have a username — but we can't extract it
        # without the entity. In that case, we fall back to ImportChatInviteRequest
        # which will work and give us the entity with the username for caching.
        # (No special handling needed; just return None here.)

        # Clean empty string to None
        if username == "":
            username = None

        self._log_debug(
            "[Account%d] Invite check result for %.8s...: username=%s (type=%s)",
            self.cfg.index, invite_hash, username, type(result).__name__,
        )

        # ── Persist to cache ──
        async with self._invite_cache_lock:
            self._invite_cache[invite_hash] = {"username": username, "ts": time.time()}
        asyncio.create_task(self._save_invite_cache())

        return username

    # ── Dispatcher ────────────────────────────────────────────────────────────

    async def _dispatch(self, event) -> None:
        text  = (event.raw_text or "").strip()
        lower = text.lower()
        parts = lower.split()
        if not parts:
            return

        cmd = parts[0]

        if cmd == "join":
            if len(parts) >= 3 and parts[1] == "delay":
                await self._handle_join_delay(event, parts[2])
            elif len(parts) >= 3 and parts[1] == "mode":
                await self._handle_join_mode(event, parts[2])
            elif event.is_reply:
                await self._handle_join(event)
            else:
                await self._safe_edit_with_auto_delete(
                    event,
                    "⚠️ لطفاً به پیامی که لینک دارد reply کنید یا `join delay <seconds>` یا `join mode fast|safe|human` را بفرستید."
                )
        elif cmd == "left" and event.is_reply:
            await self._handle_left(event)
        elif cmd == "folder":
            await self._handle_folder(event)
        elif cmd == "list":
            await self._handle_list(event)
        elif cmd == "autoleave":
            await self._handle_autoleave(event, parts)

    # ── Helper: collect entities ──────────────────────────────────────────────

    @staticmethod
    def _collect_entities(reply_msg, command_msg) -> set[tuple]:
        entities: set[tuple] = set()
        entities.update(extract_telegram_entities(reply_msg.message))
        entities.update(extract_telegram_entities(command_msg.message))

        if hasattr(reply_msg, "reply_markup") and isinstance(reply_msg.reply_markup, ReplyInlineMarkup):
            for row in reply_msg.reply_markup.rows:
                for button in row.buttons:
                    if isinstance(button, KeyboardButtonUrl):
                        entities.update(extract_telegram_entities(button.url))
        return entities

    # ── Auto-Leave Logic ──────────────────────────────────────────────────────

    async def _handle_autoleave(self, event, parts: list[str]) -> None:
        client = event.client
        me_id  = await self._get_me_id(client)
        if event.chat_id != me_id:
            return

        if len(parts) == 1:
            await self._safe_edit_with_auto_delete(
                event,
                "❌ فرمت: `autoleave <days>` یا `autoleave off` یا `autoleave status`"
            )
            return

        arg = parts[1]

        if arg == "status":
            days = self._settings["auto_leave_days"]
            count = len(self._settings["joined_chats"])
            state = f"✅ فعال ({days} روز)" if days else "❌ غیرفعال"
            await self._safe_edit_with_auto_delete(
                event,
                f"📊 **وضعیت Auto-Leave:**\n"
                f"• وضعیت: {state}\n"
                f"• چت‌های ردیابی‌شده: `{count}` چت"
            )
            return

        if arg == "off":
            async with self._settings_lock:
                self._settings["auto_leave_days"] = None
                await self._save_settings()
            await self._safe_edit_with_auto_delete(event, "✅ Auto-Leave غیرفعال شد.")
            self._log_debug("[Account%d] Auto-leave disabled", self.cfg.index)
            return

        try:
            days = int(arg)
            if days <= 0:
                raise ValueError
        except ValueError:
            await self._safe_edit_with_auto_delete(event, "❌ تعداد روز باید یک عدد مثبت باشد.")
            return

        async with self._settings_lock:
            self._settings["auto_leave_days"] = days
            await self._save_settings()
        await self._safe_edit_with_auto_delete(event, f"✅ Auto-Leave روی `{days}` روز تنظیم شد.")
        self._log_debug("[Account%d] Auto-leave set to %d days", self.cfg.index, days)

        # Sync existing folder chats so they're eligible for auto-leave
        synced = await self._sync_folder_to_tracking(client)
        if synced > 0:
            self._log_debug(
                "[Account%d] Synced %d existing folder chats into auto-leave tracking.",
                self.cfg.index, synced,
            )

    async def _auto_leave_loop(self, client: TelegramClient) -> None:
        """
        Background task: waits for client to connect, then checks for
        expired chats every 6 hours.
        """
        # Wait for client to connect before first check
        for _ in range(60):  # max 60 seconds wait
            if client.is_connected():
                break
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                return

        try:
            # On first run, sync existing folder chats into tracking
            # so auto-leave covers chats joined before the bot was tracking.
            if self._settings.get("auto_leave_days") is not None:
                await self._sync_folder_to_tracking(client)

            while True:
                if client.is_connected():
                    await self._check_auto_leave(client)
                await asyncio.sleep(6 * 3600)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log_error("Auto-leave loop crashed: %s", exc)

    async def _check_auto_leave(self, client: TelegramClient) -> None:
        """
        Check for expired chats and leave them.

        Handles various failure modes gracefully:
        - UserNotParticipantError: already left, remove from tracking
        - ChannelPrivateError: banned/kicked, remove from tracking
        - ValueError (entity not found): stale data, remove from tracking
        - Other errors: log and keep in tracking (may be transient)
        """
        if not client.is_connected():
            return

        days = self._settings.get("auto_leave_days")
        if days is None:
            return

        # Defensive: ensure joined_chats is a dict (in case of race condition
        # or in-memory corruption)
        if not isinstance(self._settings.get("joined_chats"), dict):
            self._log_warning("joined_chats is not a dict, resetting")
            self._settings["joined_chats"] = {}
            await self._save_settings()
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        to_leave: list[tuple[int, str]] = []

        async with self._settings_lock:
            for chat_id_str, joined_at_str in list(self._settings["joined_chats"].items()):
                try:
                    joined_at = datetime.datetime.fromisoformat(joined_at_str)
                    if joined_at.tzinfo is None:
                        joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
                    if (now - joined_at).days >= days:
                        to_leave.append((int(chat_id_str), joined_at_str))
                except Exception:
                    continue

        for chat_id, joined_at_str in to_leave:
            try:
                entity = await client.get_entity(chat_id)
                name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)

                if isinstance(entity, Channel):
                    await client(LeaveChannelRequest(entity))
                elif isinstance(entity, (Chat, User)):
                    await client(DeleteHistoryRequest(peer=entity, just_clear=False, max_id=0))

                self._log_debug("Auto-left '%s' (id=%d, joined %s).", name, chat_id, joined_at_str)

                async with self._settings_lock:
                    self._settings["joined_chats"].pop(str(chat_id), None)
                    await self._save_settings()

                await _remove_peers_from_joined_folder(
                    client, [entity], self.cfg.index, self._folder_cache
                )

            except errors.UserNotParticipantError:
                # Already left this chat — remove from tracking
                self._log_debug("Auto-leave: not participant in %d, removing from tracking", chat_id)
                async with self._settings_lock:
                    self._settings["joined_chats"].pop(str(chat_id), None)
                    await self._save_settings()

            except errors.ChannelPrivateError:
                # Banned or kicked — can't access anymore, remove from tracking
                self._log_debug("Auto-leave: channel %d is private/inaccessible, removing from tracking", chat_id)
                async with self._settings_lock:
                    self._settings["joined_chats"].pop(str(chat_id), None)
                    await self._save_settings()

            except (ValueError, errors.UsernameNotOccupiedError) as exc:
                # Entity not found in cache — stale data, remove from tracking
                self._log_debug("Auto-leave: entity %d not found (%s), removing from tracking", chat_id, exc)
                async with self._settings_lock:
                    self._settings["joined_chats"].pop(str(chat_id), None)
                    await self._save_settings()

            except Exception as exc:
                # Other errors (FloodWait, network) — keep in tracking, retry next cycle
                self._log_debug("Auto-leave failed for %d (will retry): %s", chat_id, exc)

    # ── JOIN DELAY ────────────────────────────────────────────────────────────

    async def _handle_join_delay(self, event, arg: str) -> None:
        try:
            delay = float(arg)
            if delay < 0:
                raise ValueError
        except ValueError:
            await self._safe_edit_with_auto_delete(
                event,
                "❌ تاخیر باید یک عدد مثبت باشد (مثلاً `join delay 5`)."
            )
            return

        async with self._settings_lock:
            self._settings["delay"] = delay
            await self._save_settings()

        mode_note = ""
        if delay == 0.0:
            mode_note = f"\n💡 Smart throttling فعال شد (mode: `{self._settings.get('join_mode', 'safe')}`)."
        else:
            mode_note = "\n💡 Smart throttling غیرفعال شد (delay ثابت اعمال می‌شود)."

        await self._safe_edit_with_auto_delete(
            event,
            f"✅ تاخیر بین جوین‌ها روی `{delay}` ثانیه تنظیم شد.{mode_note}"
        )
        self._log_debug("[Account%d] Join delay set to %.2f seconds", self.cfg.index, delay)

    # ── JOIN MODE (NEW) ────────────────────────────────────────────────────────

    async def _handle_join_mode(self, event, mode_arg: str) -> None:
        """
        Handle `join mode fast|safe|human` command.

        fast  — no smart throttling, no batching. Respects `join delay`.
                 Use if you're joining public channels with a known-good account
                 and want maximum speed.
        safe  — Layer 1 (smart link resolution) + Layer 2 (risk-based delays).
                 DEFAULT. Resolves invite links to usernames when possible, then
                 applies delays proportional to FloodWait risk.
        human — All 4 layers. Adds Layer 3 (adaptive FloodWait backoff) and
                 Layer 4 (batch cooldowns with ±20% jitter). Slowest but safest.
                 Use for large batches (20+ invites) or if your account is young.
        """
        if mode_arg not in ("fast", "safe", "human"):
            await self._safe_edit_with_auto_delete(
                event,
                "❌ مقادیر معتبر: `fast` | `safe` | `human`\n"
                "• `fast`  — بدون throttling (سریع‌ترین)\n"
                "• `safe`  — حل هوشمند لینک + تأخیر بر اساس ریسک [پیش‌فرض]\n"
                "• `human` — تمام لایه‌های ضد-FloodWait (کندترین ولی ایمن‌ترین)"
            )
            return

        async with self._settings_lock:
            self._settings["join_mode"] = mode_arg
            await self._save_settings()

        desc = {
            "fast":  "بدون smart throttling و batching — سریع‌ترین",
            "safe":  "حل هوشمند لینک + تأخیر بر اساس ریسک — پیش‌فرض",
            "human": "تمام ۴ لایه ضد-FloodWait با jitter — ایمن‌ترین",
        }[mode_arg]
        await self._safe_edit_with_auto_delete(
            event,
            f"✅ حالت جوین تنظیم شد: `{mode_arg}`\n📋 {desc}"
        )
        self._log_debug("[Account%d] Join mode set to: %s", self.cfg.index, mode_arg)

    # ── FOLDER & LIST ─────────────────────────────────────────────────────────

    async def _handle_folder(self, event) -> None:
        client = event.client
        me_id  = await self._get_me_id(client)
        if event.chat_id != me_id:
            return

        await self._safe_edit(event, "🔄 Processing 'joined' folder...")
        folders = await _get_folders(client, self._folder_cache)
        folder  = _find_joined_folder(folders)

        if not folder:
            await _create_joined_folder(client, self.cfg.index, self._folder_cache)
            await self._safe_edit_with_auto_delete(
                event,
                f"✅ فولدر **'{_JOINED_FOLDER_NAME}'** ساخته شد.\n📌 Saved Messages پین شد."
            )
            self._log_debug("[Account%d] Created '%s' folder", self.cfg.index, _JOINED_FOLDER_NAME)
        else:
            await self._safe_edit(
                event,
                f"🔄 در حال ترک تمام چت‌های **'{_JOINED_FOLDER_NAME}'** و ریست..."
            )
            left, failed = await _leave_and_reset_joined_folder(
                client, self.cfg.index, self._folder_cache
            )
            msg = f"✅ فولدر **'{_JOINED_FOLDER_NAME}'** ریست شد.\n• ترک شده: {left} چت\n"
            if failed:
                msg += f"• ناموفق: {failed} چت\n"
            msg += "📌 Saved Messages پین شد."
            await self._safe_edit_with_auto_delete(event, msg)
            self._log_debug(
                "[Account%d] Reset '%s' folder (left=%d, failed=%d)",
                self.cfg.index, _JOINED_FOLDER_NAME, left, failed,
            )

    async def _handle_list(self, event) -> None:
        client = event.client
        me_id  = await self._get_me_id(client)
        if event.chat_id != me_id:
            return

        await self._safe_edit(event, "🔍 Loading 'joined' folder contents...")
        folders = await _get_folders(client, self._folder_cache)
        folder  = _find_joined_folder(folders)

        if not folder:
            await self._safe_edit_with_auto_delete(event, f"ℹ️ فولدر **'{_JOINED_FOLDER_NAME}'** وجود ندارد.")
            return

        peers = [p for p in (folder.include_peers or []) if not isinstance(p, InputPeerSelf)]
        if not peers:
            await self._safe_edit_with_auto_delete(
                event,
                f"ℹ️ فولدر **'{_JOINED_FOLDER_NAME}'** خالی است (فقط Saved Messages)."
            )
            return

        lines = [f"📁 **فولدر '{_JOINED_FOLDER_NAME}' — {len(peers)} چت:**\n"]
        for i, peer in enumerate(peers, 1):
            pid = _peer_id(peer)
            try:
                entity = await client.get_entity(peer)
                name   = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(pid)
                uname = getattr(entity, "username", None)
                tag   = f"@{uname}" if uname else f"`{pid}`"
                lines.append(f"{i}. **{name}** — {tag}")
            except Exception:
                lines.append(f"{i}. `{pid}`")

        await self._safe_edit_with_auto_delete(event, "\n".join(lines))
        self._log_debug("[Account%d] Listed %d chats in '%s' folder", self.cfg.index, len(peers), _JOINED_FOLDER_NAME)

    # ── JOIN (4-layer anti-FloodWait) ─────────────────────────────────────────

    async def _handle_join(self, event) -> None:
        """
        Main join handler with 4-layer anti-FloodWait strategy.

        Layer 1 (safe/human): resolve invite hash → username via CheckChatInviteRequest
        Layer 2 (safe/human): apply risk-proportional delays between joins
        Layer 3 (safe/human): adaptive FloodWait backoff (duration-aware multiplier)
        Layer 4 (human only): batch cooldowns + ±20% jitter on all delays
        """
        client    = event.client
        reply_msg = await event.get_reply_message()
        if not reply_msg:
            return

        all_entities = list(self._collect_entities(reply_msg, event.message))
        if not all_entities:
            await self._safe_edit_with_auto_delete(event, "ℹ️ هیچ لینک، یوزرنیم یا ID تلگرامی یافت نشد.")
            return

        # ── Read current settings ──────────────────────────────────────────────
        join_mode  = self._settings.get("join_mode", "safe")
        user_delay = self._settings.get("delay", 0.0)

        # Layer 1+2 active in safe and human modes
        use_smart  = join_mode in ("safe", "human")
        # Layer 3 active in safe and human modes
        use_adaptive = join_mode in ("safe", "human")
        # Layer 4 only in human mode
        use_batch  = (join_mode == "human")

        mode_label = {
            "fast":  "⚡ fast",
            "safe":  "🛡 safe",
            "human": "🧘 human",
        }.get(join_mode, join_mode)

        try:
            processing_msg = await event.edit(
                f"🔍 `{len(all_entities)}` مورد یافت شد. "
                f"(mode: `{join_mode}`, delay: `{user_delay}s`)\n⏳ در حال جوین..."
            )
        except Exception as exc:
            self._log_error("Failed to create join progress message: %s", exc)
            return

        # ── Shared state for the loop ──────────────────────────────────────────
        results: list[str]        = []
        joined_entities: list     = []

        start_time    = time.monotonic()
        join_times: list[float]   = []
        success_count = 0
        fail_count    = 0
        flood_count   = 0

        # Layer 3: adaptive delay multiplier — starts at 1.0, grows on FloodWait,
        # decays by 10% on each successful join.
        adaptive_mult: float = 1.0

        # Layer 4: batch tracking
        batch_join_count  = 0   # joins since last batch break
        completed_batches = 0   # how many batch breaks taken so far

        last_edit_time = 0.0

        # ── Throttled edit helper ──────────────────────────────────────────────
        async def safe_edit(text: str) -> None:
            """Throttled live-progress edit (≥2.5s between edits)."""
            nonlocal last_edit_time
            now = time.time()
            if now - last_edit_time > _EDIT_THROTTLE:
                try:
                    await processing_msg.edit(text, parse_mode="Markdown")
                    last_edit_time = now
                except errors.FloodWaitError as e:
                    self._log_warning("Edit FloodWait %ds", e.seconds)
                    await asyncio.sleep(e.seconds)
                except Exception as exc:
                    self._log_error("Throttled edit failed: %s", exc)

        # ── FloodWait countdown helper ─────────────────────────────────────────
        async def floodwait_countdown(total_seconds: int, label: str) -> None:
            """
            Update the progress message every few seconds during a FloodWait.
            Uses 5-second chunks for short waits, 30-second chunks for long waits.
            """
            remaining = total_seconds
            chunk = 5 if total_seconds < 60 else 30
            while remaining > 0:
                sleep_for = min(remaining, chunk)
                try:
                    await processing_msg.edit(
                        f"⏳ **FloodWait** — `{label}`\n"
                        f"⏱ باقی‌مانده: **{remaining}s**\n"
                        f"_تا کنون: {success_count} موفق / {fail_count} ناموفق_",
                        parse_mode="Markdown",
                    )
                    last_edit_time.__class__  # touch to suppress warnings
                except Exception:
                    pass
                await asyncio.sleep(sleep_for)
                remaining -= sleep_for

        # ── Layer 3: adaptive FloodWait handler ───────────────────────────────
        async def handle_floodwait(exc: errors.FloodWaitError, label: str) -> float:
            """
            Handle a FloodWait adaptively based on duration.

            Short  (<30s)  → wait + 2s buffer  → multiply delay by 1.5×
            Medium (<300s) → wait + 10s buffer → multiply delay by 3.0×
            Heavy  (≥300s) → wait + 60s buffer → multiply delay by 5.0×
                              + pause ALL joins for extra_pause seconds

            The returned value is the new adaptive_mult (capped at _ADAPTIVE_MAX_MULT).
            """
            nonlocal adaptive_mult

            seconds = exc.seconds
            self._log_debug(
                "[Account%d] FloodWait %ds for '%s' (current mult=%.1f)",
                self.cfg.index, seconds, label, adaptive_mult,
            )

            if seconds < 30:
                buffer      = 2
                multiplier  = 1.5
                severity    = "خفیف"
            elif seconds < 300:
                buffer      = 10
                multiplier  = 3.0
                severity    = "متوسط"
            else:
                buffer      = 60
                multiplier  = 5.0
                severity    = "سنگین ⚠️"

            total_wait = seconds + buffer
            new_mult   = min(adaptive_mult * multiplier, _ADAPTIVE_MAX_MULT)

            self._log_debug(
                "[Account%d] FloodWait severity=%s, waiting %ds, new mult=%.1f",
                self.cfg.index, severity, total_wait, new_mult,
            )

            await floodwait_countdown(total_wait, f"{label} [{severity}: {seconds}s]")
            return new_mult

        # ── Compute delay for this join type ──────────────────────────────────
        def compute_delay(effective_type: str) -> float:
            """
            Return the sleep duration to apply AFTER a successful join.

            Priority:
            1. user_delay > 0 → fixed delay, ignore everything else
            2. join_mode == "fast" → 0 (no delay)
            3. smart delay from _SMART_DELAYS × adaptive_mult (± jitter in human mode)
            """
            if user_delay > 0:
                return user_delay  # manual override: always honor
            if join_mode == "fast":
                return 0.0

            base = _SMART_DELAYS.get(effective_type, _SMART_DELAYS["invite_direct"])
            delay = base * adaptive_mult

            if use_batch:  # human mode — add ±JITTER_FACTOR
                jitter = random.uniform(-_JITTER_FACTOR, _JITTER_FACTOR)
                delay  = delay * (1.0 + jitter)

            return max(0.5, delay)  # always at least 0.5s in smart modes

        # ── Prune stale invite cache entries at the start of each run ────────
        if use_smart and len(self._invite_cache) > 200:
            asyncio.create_task(self._prune_expired_invite_cache())

        # ═══════════════════════════════════════════════════════════════════════
        # MAIN LOOP
        # ═══════════════════════════════════════════════════════════════════════

        for idx, (entity_type, identifier) in enumerate(all_entities, 1):
            joined_entity  = None
            attempt_start  = time.monotonic()
            effective_type = entity_type  # may be upgraded by Layer 1

            # ── Layer 1: resolve invite link to username (safe/human mode) ────
            if use_smart and entity_type == "invite_link":
                invite_hash = _extract_invite_hash(str(identifier))
                if invite_hash:
                    resolved_username = await self._resolve_invite_to_username(client, invite_hash)
                    if resolved_username:
                        effective_type = "invite_with_username"
                        self._log_debug(
                            "[Account%d] Invite %.8s... → @%s (safe path)",
                            self.cfg.index, invite_hash, resolved_username,
                        )

            # ── RETRY loop for FloodWait ──────────────────────────────────────
            while True:
                try:
                    # ── Join by effective type ─────────────────────────────────

                    if entity_type == "channel_id":
                        # t.me/c/ID/msg — already a member of this channel
                        chan_id = int(f"-100{identifier}")
                        try:
                            joined_entity = await client.get_entity(chan_id)
                        except Exception:
                            joined_entity = await client.get_entity(identifier)

                    elif entity_type == "username":
                        try:
                            ip      = await client.get_input_entity(f"@{identifier}")
                            updates = await client(JoinChannelRequest(ip))
                            joined_entity = updates.chats[0] if updates.chats else None
                        except (errors.UsernameNotOccupiedError, errors.ChannelPrivateError):
                            raise
                        except Exception:
                            joined_entity = await client.get_entity(f"@{identifier}")

                    elif entity_type == "numeric_id":
                        joined_entity = await client.get_entity(identifier)

                    elif entity_type == "invite_link":

                        if effective_type == "invite_with_username":
                            # ── SAFE PATH: join via username (Layer 1 win) ────
                            # We resolved the invite to a public @username.
                            # JoinChannelRequest is far less rate-limited.
                            try:
                                ip      = await client.get_input_entity(f"@{resolved_username}")
                                updates = await client(JoinChannelRequest(ip))
                                joined_entity = updates.chats[0] if updates.chats else None
                                if joined_entity is None:
                                    # Unlikely but handle: fallback to get_entity
                                    joined_entity = await client.get_entity(f"@{resolved_username}")
                            except errors.UserAlreadyParticipantError:
                                joined_entity = await client.get_entity(f"@{resolved_username}")
                            except Exception:
                                # Username resolution succeeded but join failed;
                                # fall back to the direct hash path.
                                self._log_debug(
                                    "[Account%d] Username join failed for @%s, falling back to hash",
                                    self.cfg.index, resolved_username,
                                )
                                effective_type = "invite_direct"
                                invite_hash = _extract_invite_hash(str(identifier))
                                if invite_hash:
                                    updates = await client(ImportChatInviteRequest(invite_hash))
                                    joined_entity = updates.chats[0] if updates.chats else None

                        else:
                            # ── RISKY PATH: ImportChatInviteRequest (raw hash) ─
                            invite_hash = _extract_invite_hash(str(identifier))
                            if not invite_hash:
                                results.append(f"❌ [{identifier}] — لینک قابل parse نیست")
                                fail_count += 1
                                break
                            try:
                                updates       = await client(ImportChatInviteRequest(invite_hash))
                                joined_entity = updates.chats[0] if updates.chats else None

                                # Opportunistic: cache the entity's username for future runs
                                if joined_entity is not None and use_smart:
                                    uname = getattr(joined_entity, "username", None)
                                    async with self._invite_cache_lock:
                                        self._invite_cache[invite_hash] = {
                                            "username": uname or None,
                                            "ts": time.time(),
                                        }
                                    asyncio.create_task(self._save_invite_cache())

                            except errors.UserAlreadyParticipantError:
                                results.append(f"ℹ️ [{identifier}] — قبلاً عضو شده")
                                break
                            except Exception:
                                raise

                    # ── Record success ─────────────────────────────────────────
                    if joined_entity:
                        title = getattr(joined_entity, "title", None) or str(identifier)

                        # Annotate join type so user can see which path was used
                        path_icon = {
                            "username":             "📢",
                            "channel_id":           "🔗",
                            "numeric_id":           "🔢",
                            "invite_with_username": "🛡",  # smart resolution
                            "invite_direct":        "🔑",  # raw hash
                        }.get(effective_type, "✅")

                        joined_entities.append(joined_entity)
                        results.append(f"{path_icon} [{title}] ✅")
                        success_count += 1
                        join_times.append(time.monotonic() - attempt_start)

                        # Layer 3: decay adaptive multiplier on success
                        if use_adaptive and adaptive_mult > 1.0:
                            adaptive_mult = max(1.0, adaptive_mult * _ADAPTIVE_DECAY)

                        # Track join timestamp
                        async with self._settings_lock:
                            self._settings["joined_chats"][str(joined_entity.id)] = \
                                datetime.datetime.now(datetime.timezone.utc).isoformat()
                            await self._save_settings()

                    break  # ← success, exit retry loop

                # ── FloodWait handling (Layer 3) ──────────────────────────────
                except errors.FloodWaitError as exc:
                    flood_count += 1
                    if use_adaptive:
                        adaptive_mult = await handle_floodwait(exc, str(identifier))
                    else:
                        # fast mode: simple wait, no multiplier adjustment
                        await floodwait_countdown(exc.seconds + 2, str(identifier))
                    continue  # ← retry the same join after waiting

                # ── Other errors ──────────────────────────────────────────────
                except Exception as exc:
                    err = str(exc)
                    fail_count += 1
                    if "INVITE_REQUEST_SENT" in err:
                        status = "⏳ درخواست ارسال شد"
                    elif isinstance(exc, errors.InviteHashInvalidError) or "INVITE_HASH_INVALID" in err:
                        status = "❌ لینک نامعتبر"
                    elif isinstance(exc, errors.UsernameNotOccupiedError):
                        status = "❌ یوزرنیم وجود ندارد"
                    elif isinstance(exc, errors.ChannelPrivateError):
                        status = "🔒 خصوصی/محدود"
                    elif "FLOOD_WAIT" in err:
                        # Edge case: FloodWait not caught as FloodWaitError
                        status = f"⏳ FloodWait: {err[:40]}"
                    else:
                        status = f"❌ خطا: {err[:40]}"
                    results.append(f"[{identifier}] — {status}")
                    break

            # ── Live progress update ───────────────────────────────────────────
            await safe_edit(
                f"🔄 در حال جوین... ({idx}/{len(all_entities)}) {mode_label}\n"
                f"آخرین: {results[-1] if results else '-'}\n"
                f"_mult: {adaptive_mult:.1f}×_"
                if use_adaptive and adaptive_mult > 1.0
                else
                f"🔄 در حال جوین... ({idx}/{len(all_entities)}) {mode_label}\n"
                f"آخرین: {results[-1] if results else '-'}"
            )

            # ─── Apply inter-join delay (Layers 2 + 3) ────────────────────────
            if idx < len(all_entities):
                this_delay = compute_delay(effective_type)
                if this_delay > 0:
                    await asyncio.sleep(this_delay)

            # ── Layer 4: Batch cooldown (human mode only) ─────────────────────
            if use_batch and idx < len(all_entities):
                batch_join_count += 1
                if batch_join_count >= _BATCH_SIZE:
                    batch_join_count  = 0
                    completed_batches += 1

                    if completed_batches % _BATCHES_BEFORE_LONG == 0:
                        cooldown = _COOLDOWN_LONG
                        rest_msg = f"☕ استراحت طولانی بعد از {completed_batches} دسته"
                    else:
                        cooldown = _COOLDOWN_SHORT
                        rest_msg = f"☕ استراحت کوتاه (دسته {completed_batches})"

                    # ±20% jitter on cooldowns too
                    jitter   = random.uniform(-_JITTER_FACTOR, _JITTER_FACTOR)
                    cooldown = cooldown * (1.0 + jitter)

                    self._log_debug(
                        "[Account%d] Batch cooldown: %.0fs (batches=%d)",
                        self.cfg.index, cooldown, completed_batches,
                    )
                    try:
                        await processing_msg.edit(
                            f"🧘 {rest_msg} — {cooldown:.0f}s\n"
                            f"_تا کنون: {success_count} موفق / {fail_count} ناموفق_",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(cooldown)

        # ═══════════════════════════════════════════════════════════════════════
        # POST-LOOP: Folder update + summary
        # ═══════════════════════════════════════════════════════════════════════

        folder_note = ""
        if joined_entities:
            try:
                await _ensure_joined_folder_exists(client, self.cfg.index, self._folder_cache)
                added = await _add_peers_to_joined_folder(
                    client, joined_entities, self.cfg.index, self._folder_cache
                )
                folder_note = f"\n📁 `{added}` چت به فولدر '{_JOINED_FOLDER_NAME}' اضافه شد."
            except Exception as exc:
                self._log_error("Failed to update folder: %s", exc)
                folder_note = f"\n⚠️ خطا در بروزرسانی فولدر: {exc}"

        total_time = time.monotonic() - start_time
        avg_time   = sum(join_times) / len(join_times) if join_times else 0
        min_time   = min(join_times) if join_times else 0
        max_time   = max(join_times) if join_times else 0

        # Layer 1 efficiency stats
        smart_wins = sum(1 for r in results if "🛡" in r)
        smart_note = (
            f"\n🛡 `{smart_wins}` لینک از invite به username تبدیل شد (Layer 1)"
            if use_smart and smart_wins > 0
            else ""
        )

        summary = (
            f"--- **نتایج جوین** ({join_mode}) ---\n"
            f"{chr(10).join(results)}\n"
            f"------------------\n"
            f"📊 **آمار تفصیلی:**\n"
            f"• ✅ موفق: `{success_count}` | ❌ ناموفق: `{fail_count}` | ⏳ FloodWait: `{flood_count}`\n"
            f"• ⏱ زمان کل: `{total_time:.1f}s` | میانگین: `{avg_time:.2f}s`\n"
            f"• 🚀 سریع‌ترین: `{min_time:.2f}s` | 🐢 کندترین: `{max_time:.2f}s`"
            f"{smart_note}"
            f"{folder_note}"
        )

        try:
            await processing_msg.edit(summary, parse_mode="Markdown")
        except Exception:
            try:
                await event.respond(summary, parse_mode="Markdown")
            except Exception:
                pass

        # Schedule auto-delete after 5 seconds — tracked for teardown() cancellation
        self._track_delete_task(processing_msg, _AUTO_DELETE_DELAY)

        self._log_debug(
            "[Account%d] Join completed: mode=%s, success=%d, fail=%d, flood=%d, time=%.1fs, "
            "final_mult=%.1f",
            self.cfg.index, join_mode, success_count, fail_count,
            flood_count, total_time, adaptive_mult,
        )

    # ── LEFT ──────────────────────────────────────────────────────────────────

    async def _handle_left(self, event) -> None:
        client = event.client
        reply_msg = await event.get_reply_message()
        if not reply_msg:
            return

        all_entities = self._collect_entities(reply_msg, event.message)
        if not all_entities:
            await self._safe_edit_with_auto_delete(event, "ℹ️ هیچ لینک، یوزرنیم یا ID تلگرامی یافت نشد.")
            return

        try:
            processing_msg = await event.edit(f"🔍 `{len(all_entities)}` مورد یافت شد. در حال ترک...")
        except Exception as exc:
            self._log_error("Failed to create leave progress message: %s", exc)
            return

        results: list[str] = []
        left_entities: list = []
        any_successful_left = False

        for entity_type, identifier in all_entities:
            try:
                target_entity = None
                left_ok = False

                if entity_type == 'channel_id':
                    chan_id = int(f"-100{identifier}")
                    try:
                        target_entity = await client.get_entity(chan_id)
                    except Exception:
                        target_entity = await client.get_entity(identifier)
                elif entity_type == 'username':
                    target_entity = await client.get_entity(f"@{identifier}")
                elif entity_type == 'numeric_id':
                    target_entity = await client.get_entity(identifier)
                elif entity_type == 'invite_link':
                    invite_hash = _extract_invite_hash(str(identifier))
                    if not invite_hash:
                        results.append(f"❌ [{identifier}] — لینک قابل parse نیست")
                        continue
                    try:
                        updates = await client(ImportChatInviteRequest(invite_hash))
                        target_entity = updates.chats[0] if updates.chats else None
                    except Exception as exc:
                        results.append(f"❌ [{identifier}] — خطا ({exc})")
                        continue

                if target_entity is None:
                    continue

                name = getattr(target_entity, "title", None) or getattr(target_entity, "first_name", None) or str(identifier)

                if isinstance(target_entity, Channel):
                    await client(LeaveChannelRequest(target_entity))
                    left_ok = True
                    results.append(f"✅ [{name}] — ترک شد")
                elif isinstance(target_entity, (Chat, User)):
                    await client(DeleteHistoryRequest(peer=target_entity, just_clear=False, max_id=0))
                    left_ok = True
                    results.append(f"✅ [{name}] — حذف شد")

                if left_ok:
                    any_successful_left = True
                    left_entities.append(target_entity)
                    async with self._settings_lock:
                        self._settings["joined_chats"].pop(str(target_entity.id), None)
                        await self._save_settings()

            except errors.FloodWaitError as exc:
                self._log_debug("Left FloodWait %ds", exc.seconds)
                try:
                    await processing_msg.edit(f"⏳ Flood wait {exc.seconds}s برای `{identifier}`...")
                except Exception:
                    pass
                await asyncio.sleep(exc.seconds)
                results.append(f"⏳ [{identifier}] — Flood wait")
            except Exception as exc:
                results.append(f"❌ [{identifier}] — {str(exc)[:40]}")

        folder_note = ""
        if left_entities:
            try:
                removed = await _remove_peers_from_joined_folder(
                    client, left_entities, self.cfg.index, self._folder_cache
                )
                if removed:
                    folder_note = f"\n📁 `{removed}` چت از فولدر '{_JOINED_FOLDER_NAME}' حذف شد."
            except Exception as exc:
                self._log_error("Failed to remove peers from folder: %s", exc)

        final_text = "--- نتایج ترک ---\n" + "\n".join(results) + "\n------------------" + folder_note
        try:
            await processing_msg.edit(final_text, parse_mode="Markdown")
        except Exception:
            pass

        # Schedule auto-delete after 5 seconds — tracked for teardown() cancellation
        self._track_delete_task(processing_msg, _AUTO_DELETE_DELAY)

        if any_successful_left:
            await safe_delete(client, event.chat_id, event.message.id)
            if event.is_reply and reply_msg and reply_msg.out:
                try:
                    await client.edit_message(reply_msg, ".")
                except Exception:
                    pass

        self._log_debug(
            "[Account%d] Left completed: %d entities processed",
            self.cfg.index, len(all_entities)
        )


# ── Help Texts (در انتهای ماژول طبق قوانین) ─────────────────────────────────

help_text = (
    "• `join` (reply) | عضویت در چت‌های reply شده\n"
    "• `left` (reply) | ترک چت‌های reply شده\n"
    "• `join delay <seconds>` | تنظیم تاخیر ثابت (0 = بازگشت به smart)\n"
    "• `join mode fast|safe|human` | تنظیم حالت ضد-FloodWait\n"
    "• `folder` | ایجاد یا ریست فولدر joined\n"
    "• `list` | نمایش لیست چت‌های فولدر\n"
    "• `autoleave <days>` | ترک خودکار پس از N روز\n"
    "• `autoleave off` | غیرفعال‌سازی ترک خودکار\n"
    "• `autoleave status` | نمایش وضعیت فعلی\n"
)

help_extra = (
    "عضویت و ترک - مدیریت چت‌ها با فولدر و ترک خودکار\n\n"
    "دستورات اصلی:\n"
    "• `join` (reply) | عضویت در همه چت‌های یافت‌شده در پیام reply\n"
    "• `left` (reply) | ترک همه چت‌های یافت‌شده در پیام reply\n\n"
    "انواع لینک‌های پشتیبانی‌شده:\n"
    "• لینک‌های عمومی | `t.me/username`\n"
    "• لینک‌های خصوصی جدید | `t.me/+AbCdEfGh`\n"
    "• لینک‌های خصوصی قدیمی | `t.me/joinchat/AbCdEfGh`\n"
    "• شناسه عددی | `1234567890`\n"
    "• لینک‌های خصوصی کانال | `t.me/c/1234567890/123`\n\n"
    "سیستم ضد-FloodWait (۴ لایه):\n"
    "• `join mode fast`  | بدون throttling، بدون batching — سریع‌ترین\n"
    "• `join mode safe`  | Layer 1+2: حل هوشمند لینک + تأخیر بر اساس ریسک [پیش‌فرض]\n"
    "• `join mode human` | Layer 1+2+3+4: تمام لایه‌ها + batch cooldown — ایمن‌ترین\n\n"
    "Layer 1 — Smart Link Resolution:\n"
    "  → invite link→ بررسی قبل از جوین → اگر username داشت → JoinChannelRequest (ایمن)\n"
    "  → نتایج cache می‌شوند (۶ ساعت) در join_left_invite_cache.json\n"
    "  → نماد 🛡 = از smart path استفاده شد | نماد 🔑 = مستقیم از hash\n\n"
    "Layer 2 — Risk-Based Delays:\n"
    "  → username: 2s | channel_id/numeric_id: 3s | invite→username: 4s | invite direct: 8s\n"
    "  → وقتی `join delay` روی ۰ باشد (پیش‌فرض) فعال است\n\n"
    "Layer 3 — Adaptive FloodWait (safe/human):\n"
    "  → FloodWait خفیف (<30s): ضریب ×1.5\n"
    "  → FloodWait متوسط (<300s): ضریب ×3.0\n"
    "  → FloodWait سنگین (≥300s): ضریب ×5.0\n"
    "  → با هر جوین موفق، ضریب ۱۰٪ کاهش می‌یابد\n\n"
    "Layer 4 — Batch & Cooldown (human only):\n"
    "  → بعد از هر ۵ جوین: استراحت ۳۰s\n"
    "  → بعد از هر ۳ دسته: استراحت ۱۲۰s\n"
    "  → jitter ±۲۰٪ روی تمام تأخیرها\n\n"
    "تنظیمات تاخیر:\n"
    "• `join delay <seconds>` | تأخیر ثابت — smart throttling را غیرفعال می‌کند\n"
    "• `join delay 0` | بازگشت به smart throttling\n"
    "• پیش‌فرض | ۰ ثانیه + smart mode = safe\n\n"
    "مدیریت فولدر:\n"
    "• `folder` | ایجاد یا ریست فولدر `joined` با چت‌های فعلی\n"
    "• `list` | نمایش لیست چت‌های موجود در فولدر `joined`\n\n"
    "ترک خودکار:\n"
    "• `autoleave <days>` | ترک چت‌های فولدر `joined` پس از N روز\n"
    "  → شامل چت‌های از قبل موجود در فولدر هم می‌شود\n"
    "• `autoleave off` | غیرفعال‌سازی ترک خودکار\n"
    "• `autoleave status` | نمایش وضعیت فعلی\n\n"
    "مثال‌ها:\n"
    "• یک پیام با چند لینک چت را reply کنید و `join` بفرستید\n"
    "• `join mode human` | حالت کاملاً ایمن برای جوین انبوه\n"
    "• `join delay 3` | تأخیر ثابت ۳ ثانیه (smart را غیرفعال می‌کند)\n"
    "• `join delay 0` | بازگشت به smart throttling\n"
    "• `autoleave 7` | ترک خودکار بعد از یک هفته\n\n"
    "نکات مهم:\n"
    "• `join`, `left`, `join delay`, `join mode` در هر چتی کار می‌کنند\n"
    "• `folder`, `list`, `autoleave` فقط در Saved Messages کار می‌کنند\n"
    "• چت‌هایی که قبلاً عضو هستید، رد می‌شوند\n"
    "• در صورت FloodWait، شمارش معکوس زنده نمایش داده می‌شود\n"
    "• گزارش نهایی شامل تعداد موفق/ناموفق/FloodWait و آمار Layer 1 است\n"
    "• چت‌های موفق در فولدر `joined` ذخیره می‌شوند\n"
    "• پس از `left` موفق، پیام دستور به‌صورت خودکار حذف می‌شود\n"
    "• چت‌های غیرقابل دسترس به‌صورت خودکار از tracking حذف می‌شوند\n"
    "• هنگام فعال‌سازی autoleave، چت‌های موجود در فولدر هم ردیابی می‌شوند\n"
)

JoinLeft.help_text = help_text
JoinLeft.help_extra = help_extra


def create_module(cfg: "AccountConfig") -> Module:
    return JoinLeft(cfg)