"""
core/account_manager.py
════════════════════════════════════════════════════════════════
Runtime Account Management — Interactive Add/Remove Flows

Provides interactive flows for adding and removing accounts via
Telegram Saved Messages commands:

Add-account flow:
    1. Admin sends `.addaccount`
    2. Bot asks for `api_id`
    3. Admin replies with `api_id`
    4. Bot asks for `api_hash`
    5. Admin replies with `api_hash`
    6. Bot asks for phone number
    7. Admin replies with phone
    8. Bot sends Telegram verification code request
    9. Admin replies with received code
    10. If 2FA enabled, bot asks for password
    11. Session saved, `account.json` written, account started live

Remove-account flow:
    1. User sends `.removeaccount <index>`
    2. Bot confirms and asks for explicit approval
    3. User replies `yes` to confirm
    4. Account stopped, session + folder deleted

Note:
This version uses direct connection only (no proxy support).
For bypassing network restrictions, use a system-level VPN
(WireGuard, OpenVPN, V2Ray) on Termux or Windows.

Public API:
    start_add_flow(event, cfg, start_account_cb)
    start_remove_flow(event, cfg, args)
    cancel_active_flow(account_index) → bool
════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from telethon import TelegramClient, errors
from telethon import events as tl_events
from telethon.network.connection import ConnectionTcpFull

import config
from core.exceptions import LoaderNotFoundError
from core.logger import get_logger
from core.plugin_registry import loader_registry

if TYPE_CHECKING:
    from config import AccountConfig

log = get_logger(__name__)


# Seconds before an inactive flow auto-expires
_FLOW_TIMEOUT: int = 300


# ── Flow step state machine ───────────────────────────────────────────────────

class _Step(Enum):
    """All possible steps across add and remove flows."""
    # Add flow
    WAIT_API_ID   = auto()
    WAIT_API_HASH = auto()
    WAIT_PHONE    = auto()
    WAIT_CODE     = auto()
    WAIT_PASSWORD = auto()
    # Remove flow
    WAIT_CONFIRM  = auto()


# ── Flow data container ──────────────────────────────────────────────────────

class _Flow:
    """Holds the mutable state for one active interactive session."""
    __slots__ = (
        "account_index", "admin_id", "chat_id", "step", "client",
        "status_msg_id", "api_id", "api_hash", "phone",
        "tmp_client", "code_hash", "remove_index", "start_account_cb",
        "event_handler", "_account_dir_index",
    )

    def __init__(
        self,
        account_index:    int,
        admin_id:         int,
        chat_id:          int,
        step:             _Step,
        client:           TelegramClient,
        status_msg_id:    int,
        start_account_cb: Callable | None = None,
    ) -> None:
        self.account_index:    int                   = account_index
        self.admin_id:         int                   = admin_id
        self.chat_id:          int                   = chat_id
        self.step:             _Step                 = step
        self.client:           TelegramClient        = client
        self.status_msg_id:    int                   = status_msg_id
        self.start_account_cb: Callable | None       = start_account_cb

        # Collected credentials
        self.api_id:   int = 0
        self.api_hash: str = ""
        self.phone:    str = ""

        # Temp client used during sign-in
        self.tmp_client:  TelegramClient | None = None
        self.code_hash:   str                   = ""

        # Remove flow
        self.remove_index: int = 0

        # Index reserved by _step_phone() — used in _finish_add() to avoid
        # calling _next_account_index() a second time and getting a different
        # value if another account folder was created concurrently.
        self._account_dir_index: int = 0

        # Event handler reference (for cleanup)
        self.event_handler = None

    async def edit(self, text: str) -> None:
        try:
            await self.client.edit_message(
                self.chat_id, self.status_msg_id, text, parse_mode="Markdown"
            )
        except Exception as exc:
            log.warning("Flow.edit error: %s", exc)

    async def reply(self, text: str) -> None:
        try:
            await self.client.send_message(self.chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            log.warning("Flow.reply error: %s", exc)


# ── Active-flow registry (per account_index) ─────────────────────────────────

_flows: dict[int, _Flow] = {}  # account_index → _Flow


def is_flow_active(account_index: int) -> bool:
    """Return `True` if account_index has an active interactive flow."""
    return account_index in _flows


async def cancel_active_flow(account_index: int) -> bool:
    """
    Cancel and clean up the active flow for account_index.
    Returns True if a flow was found and cancelled, False otherwise.
    """
    flow = _flows.pop(account_index, None)
    if flow is None:
        return False

    # Remove event handler
    if flow.event_handler is not None:
        try:
            flow.client.remove_event_handler(flow.event_handler)
        except Exception:
            pass

    # Disconnect temp client — store the task reference to prevent GC warnings
    if flow.tmp_client is not None:
        task = asyncio.get_running_loop().create_task(
            _safe_disconnect(flow.tmp_client),
            name="flow_cancel_disconnect",
        )
        # Suppress unhandled-exception noise if the task fails silently
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    log.info("Flow cancelled for account #%d.", account_index)
    return True


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def _next_account_index() -> int:
    """Return the next available numeric account directory index."""
    config.ACCOUNTS_DIR.mkdir(exist_ok=True)
    existing = [
        int(p.name) for p in config.ACCOUNTS_DIR.iterdir()
        if p.is_dir() and p.name.isdigit()
    ]
    return max(existing, default=0) + 1


def _build_tmp_client(api_id: int, api_hash: str, session_path: str) -> TelegramClient:
    """
    Build a temporary `TelegramClient` using direct connection.

    This version uses direct connection only (no proxy support).
    """
    return TelegramClient(
        session_path,
        api_id,
        api_hash,
        connection=ConnectionTcpFull,
    )


async def _expire_flow(account_index: int) -> None:
    """Called by the event-loop timer when a flow times out."""
    flow = _flows.pop(account_index, None)
    if flow is None:
        return

    # Remove event handler
    if flow.event_handler is not None:
        try:
            flow.client.remove_event_handler(flow.event_handler)
        except Exception:
            pass

    if flow.tmp_client:
        task = asyncio.get_running_loop().create_task(
            _safe_disconnect(flow.tmp_client),
        )
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    task = asyncio.get_running_loop().create_task(
        flow.reply(f"⏰ Flow به دلیل عدم فعالیت ({_FLOW_TIMEOUT}s) لغو شد.")
    )
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    log.info("Flow expired for account #%d.", account_index)


# ── Reply handler (registered per-flow) ──────────────────────────────────────

async def _handle_reply(event, account_index: int) -> None:
    """Handle incoming reply from admin for an active flow."""
    flow = _flows.get(account_index)
    if flow is None:
        return

    # Only handle messages in Saved Messages from admin
    if event.chat_id != flow.chat_id or event.sender_id != flow.admin_id:
        return

    text = (event.raw_text or "").strip()
    if not text:
        return

    # Check for cancel command
    if text.lower() in (".cancelflow", "cancel", "/cancel"):
        await cancel_active_flow(account_index)
        await flow.reply("✅ Flow لغو شد.")
        return

    try:
        match flow.step:
            case _Step.WAIT_API_ID:
                await _step_api_id(flow, text)
            case _Step.WAIT_API_HASH:
                await _step_api_hash(flow, text)
            case _Step.WAIT_PHONE:
                await _step_phone(flow, text)
            case _Step.WAIT_CODE:
                await _step_code(flow, text)
            case _Step.WAIT_PASSWORD:
                await _step_password(flow, text)
            case _Step.WAIT_CONFIRM:
                await _step_confirm_remove(flow, text)
    except Exception as exc:
        log.exception(
            "Flow step error (account #%d, step %s): %s",
            account_index, flow.step, exc,
        )
        await cancel_active_flow(account_index)
        await flow.reply(f"❌ خطای غیرمنتظره: `{exc}`\nFlow لغو شد.")


# ── Public flow starters ─────────────────────────────────────────────────────

async def start_add_flow(
    event,
    cfg: "AccountConfig",
    start_account_cb: Callable | None = None,
) -> None:
    """
    Begin the interactive add-account flow.

    Compatible with `system.py` which calls:
        start_add_flow(event, cfg, start_account_cb)
    """
    account_index = cfg.index
    client = event.client
    admin_id = event.sender_id
    chat_id = event.chat_id

    if account_index in _flows:
        await event.edit(
            "⚠️ یه flow قبلاً فعاله. ابتدا `.cancelflow` رو بفرست."
        )
        return

    # Create initial status message
    msg = await event.edit(
        "➕ **افزودن اکانت جدید**\n\n"
        "**مرحله ۱/۴** — `api_id` رو از https://my.telegram.org/apps بفرست:\n\n"
        "_برای لغو: `.cancelflow`_"
    )

    flow = _Flow(
        account_index    = account_index,
        admin_id         = admin_id,
        chat_id          = chat_id,
        step             = _Step.WAIT_API_ID,
        client           = client,
        status_msg_id    = msg.id,
        start_account_cb = start_account_cb,
    )
    _flows[account_index] = flow

    # Register reply handler for this flow
    async def handler(e):
        await _handle_reply(e, account_index)

    client.add_event_handler(
        handler,
        tl_events.NewMessage(outgoing=True, chats=chat_id)
    )
    flow.event_handler = handler

    # Set expiration timer — capture account_index by value via default arg
    # to prevent late-binding if the variable were ever mutated later.
    def _schedule_expire(_idx: int = account_index) -> None:
        task = asyncio.get_running_loop().create_task(
            _expire_flow(_idx),
            name=f"flow_expire_a{_idx}",
        )
        task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )

    asyncio.get_running_loop().call_later(_FLOW_TIMEOUT, _schedule_expire)

    log.info("Add-account flow started for account #%d.", account_index)


async def start_remove_flow(
    event,
    cfg: "AccountConfig",
    args: str,
) -> None:
    """
    Begin the interactive remove-account flow.

    Compatible with `system.py` which calls:
        start_remove_flow(event, cfg, args)
    """
    account_index = cfg.index
    client = event.client
    admin_id = event.sender_id
    chat_id = event.chat_id

    if account_index in _flows:
        await event.edit(
            "⚠️ یه flow قبلاً فعاله. ابتدا `.cancelflow` رو بفرست."
        )
        return

    # Parse target index from args
    args = (args or "").strip()
    if not args:
        await event.edit(
            "❌ **شماره اکانت را وارد کنید.**\n\n"
            "**استفاده:** `.removeaccount <n>`\n\n"
            "**مثال:** `.removeaccount 3`"
        )
        return

    try:
        remove_index = int(args)
    except ValueError:
        await event.edit("❌ **شماره اکانت باید یک عدد باشد.**")
        return

    target = next((a for a in config.ACCOUNTS if a.index == remove_index), None)
    if target is None:
        await event.edit(f"❌ اکانت #{remove_index} پیدا نشد.")
        return

    # Create initial status message
    msg = await event.edit(
        f"🗑️ **حذف اکانت #{remove_index}**\n\n"
        f"• شماره: `{target.phone or 'N/A'}`\n"
        f"• API ID: `{target.api_id}`\n\n"
        f"⚠️ سشن و تمام فایل‌های اکانت حذف می‌شن.\n\n"
        f"برای تأیید `yes` بفرست، برای لغو `.cancelflow`:"
    )

    flow = _Flow(
        account_index  = account_index,
        admin_id       = admin_id,
        chat_id        = chat_id,
        step           = _Step.WAIT_CONFIRM,
        client         = client,
        status_msg_id  = msg.id,
    )
    flow.remove_index = remove_index
    _flows[account_index] = flow

    # Register reply handler
    async def handler(e):
        await _handle_reply(e, account_index)

    client.add_event_handler(
        handler,
        tl_events.NewMessage(outgoing=True, chats=chat_id)
    )
    flow.event_handler = handler

    # Set expiration timer
    def _schedule_expire(_idx: int = account_index) -> None:
        task = asyncio.get_running_loop().create_task(
            _expire_flow(_idx),
            name=f"flow_expire_a{_idx}",
        )
        task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )

    asyncio.get_running_loop().call_later(_FLOW_TIMEOUT, _schedule_expire)

    log.info(
        "Remove-account flow started for account #%d, target #%d.",
        account_index, remove_index,
    )


# ── Step handlers ─────────────────────────────────────────────────────────────

async def _step_api_id(flow: _Flow, text: str) -> None:
    try:
        api_id = int(text.strip())
        if api_id <= 0:
            raise ValueError
    except ValueError:
        await flow.edit("❌ `api_id` باید یه عدد مثبت باشه. دوباره بفرست:")
        return

    flow.api_id = api_id
    flow.step   = _Step.WAIT_API_HASH
    await flow.edit(
        f"➕ **افزودن اکانت جدید**\n\n"
        f"✅ `api_id`: `{api_id}`\n\n"
        f"**مرحله ۲/۴** — `api_hash` رو بفرست:"
    )


async def _step_api_hash(flow: _Flow, text: str) -> None:
    api_hash = text.strip()
    if len(api_hash) < 20:
        await flow.edit("❌ `api_hash` خیلی کوتاهه. دوباره بفرست:")
        return

    flow.api_hash = api_hash
    flow.step     = _Step.WAIT_PHONE
    await flow.edit(
        f"➕ **افزودن اکانت جدید**\n\n"
        f"✅ `api_id`: `{flow.api_id}`\n"
        f"✅ `api_hash`: `{api_hash[:6]}…`\n\n"
        f"**مرحله ۳/۴** — شماره تلفن رو با `+` بفرست:\n"
        f"مثال: `+989123456789`"
    )


async def _step_phone(flow: _Flow, text: str) -> None:
    phone = text.strip()
    if not phone.startswith("+"):
        await flow.edit("❌ شماره باید با `+` شروع بشه. دوباره بفرست:")
        return

    flow.phone = phone

    # Reserve the account index here and store it on the flow so that
    # _finish_add() uses the exact same index — not a second call to
    # _next_account_index() which could return a different value if another
    # account folder appeared between now and sign-in completion.
    idx = _next_account_index()
    flow._account_dir_index = idx

    account_dir  = config.ACCOUNTS_DIR / str(idx)
    account_dir.mkdir(parents=True, exist_ok=True)
    session_path = str(account_dir / "session")

    # Build temp client with direct connection
    flow.tmp_client = _build_tmp_client(flow.api_id, flow.api_hash, session_path)

    await flow.edit(
        f"➕ **افزودن اکانت جدید**\n\n📱 در حال ارسال کد تأیید به `{phone}`..."
    )

    try:
        await flow.tmp_client.connect()
        result         = await flow.tmp_client.send_code_request(phone)
        flow.code_hash = result.phone_code_hash
        flow.step      = _Step.WAIT_CODE
        await flow.edit(
            f"➕ **افزودن اکانت جدید**\n\n"
            f"✅ کد تأیید به `{phone}` ارسال شد.\n\n"
            f"**مرحله ۴/۴** — کد دریافتی رو بفرست:"
        )
    except errors.FloodWaitError as exc:
        await cancel_active_flow(flow.account_index)
        await flow.reply(f"⏳ Flood wait {exc.seconds}s. بعداً دوباره امتحان کن.")
    except errors.PhoneNumberInvalidError:
        await cancel_active_flow(flow.account_index)
        await flow.reply("❌ شماره تلفن نامعتبره. Flow لغو شد.")
    except Exception as exc:
        await cancel_active_flow(flow.account_index)
        await flow.reply(f"❌ خطا در ارسال کد: `{exc}`\nFlow لغو شد.")


async def _step_code(flow: _Flow, text: str) -> None:
    code = text.strip().replace(" ", "").replace("-", "")
    if not code.isdigit():
        await flow.edit("❌ کد باید فقط عدد باشه. دوباره بفرست:")
        return

    try:
        await flow.tmp_client.sign_in(
            flow.phone, code, phone_code_hash=flow.code_hash
        )
        await _finish_add(flow)
    except errors.SessionPasswordNeededError:
        flow.step = _Step.WAIT_PASSWORD
        await flow.edit(
            "🔐 **تأیید دو مرحله‌ای (2FA) فعاله.**\n\nرمز 2FA رو بفرست:"
        )
    except errors.PhoneCodeExpiredError:
        await cancel_active_flow(flow.account_index)
        await flow.reply("❌ کد منقضی شده. دوباره `.addaccount` رو بزن.")
    except errors.PhoneCodeInvalidError:
        await flow.edit("❌ کد اشتباهه. دوباره بفرست:")
    except Exception as exc:
        await cancel_active_flow(flow.account_index)
        await flow.reply(f"❌ خطا در ورود: `{exc}`\nFlow لغو شد.")


async def _step_password(flow: _Flow, text: str) -> None:
    try:
        await flow.tmp_client.sign_in(password=text.strip())
        await _finish_add(flow)
    except errors.PasswordHashInvalidError:
        await flow.edit("❌ رمز 2FA اشتباهه. دوباره بفرست:")
    except Exception as exc:
        await cancel_active_flow(flow.account_index)
        await flow.reply(f"❌ خطا در 2FA: `{exc}`\nFlow لغو شد.")


async def _finish_add(flow: _Flow) -> None:
    """Called after successful sign-in — saves files and starts the account."""
    account_index    = flow.account_index
    start_account_cb = flow.start_account_cb

    me = await flow.tmp_client.get_me()

    # Use the index that was reserved in _step_phone() — never call
    # _next_account_index() again here; a second call would return a
    # different value if any other account folder was created between
    # _step_phone() and now.
    idx = flow._account_dir_index
    if idx == 0:
        # Fallback: derive from the session file path set by _step_phone()
        session_str = getattr(
            getattr(flow.tmp_client, "session", None), "filename", None
        )
        if session_str:
            try:
                idx = int(Path(session_str).parent.name)
            except (ValueError, AttributeError):
                pass
        if idx == 0:
            idx = _next_account_index()

    account_dir = config.ACCOUNTS_DIR / str(idx)
    account_dir.mkdir(parents=True, exist_ok=True)

    await flow.tmp_client.disconnect()

    # Write account.json — no is_admin field; the admin system has been
    # removed.  All accounts are equal and owned by the user.
    cfg_data = {
        "api_id":   flow.api_id,
        "api_hash": flow.api_hash,
        "phone":    flow.phone,
    }
    (account_dir / "account.json").write_text(
        json.dumps(cfg_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Build AccountConfig with only the fields the dataclass defines.
    new_cfg = config.AccountConfig(
        index        = idx,
        account_dir  = account_dir,
        session_path = str(account_dir / "session"),
        api_id       = flow.api_id,
        api_hash     = flow.api_hash,
        phone        = flow.phone,
        log_file     = str(config.LOGS_DIR / f"account{idx}.log"),
        settings_dir = config.SETTINGS_DIR / f"account{idx}",
    )
    new_cfg.settings_dir.mkdir(parents=True, exist_ok=True)
    config.ACCOUNTS.append(new_cfg)

    log.info(
        "Account #%d added: %s (ID %d).",
        idx, me.first_name, me.id,
    )

    # Clean up flow state AFTER collecting all needed data
    await cancel_active_flow(account_index)

    await flow.reply(
        f"✅ **اکانت #{idx} با موفقیت اضافه شد!**\n\n"
        f"• نام: **{me.first_name}**\n"
        f"• ID: `{me.id}`\n"
        f"• شماره: `{flow.phone}`\n\n"
        f"🚀 در حال استارت اکانت…"
    )

    if start_account_cb is not None:
        task = asyncio.get_running_loop().create_task(
            start_account_cb(new_cfg), name=f"account{idx}"
        )
        task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )
        await asyncio.sleep(2)
        try:
            await flow.reply(f"✅ اکانت #{idx} استارت شد و در حال اجراست.")
        except Exception:
            pass
    else:
        try:
            await flow.reply(
                f"✅ اکانت #{idx} ذخیره شد.\n"
                f"⚠️ برای اجرا ربات رو ریستارت کن."
            )
        except Exception:
            pass


async def _step_confirm_remove(flow: _Flow, text: str) -> None:
    if text.lower() not in ("yes", "بله", "y"):
        await cancel_active_flow(flow.account_index)
        await flow.reply("❌ حذف لغو شد.")
        return

    idx           = flow.remove_index
    account_index = flow.account_index

    target = next((a for a in config.ACCOUNTS if a.index == idx), None)
    if target is None:
        await cancel_active_flow(account_index)
        await flow.reply(f"❌ اکانت #{idx} دیگه در لیست نیست.")
        return

    await flow.edit(f"🗑️ در حال حذف اکانت #{idx}…")

    # Cancel flow (removes event handler) before proceeding with removal
    await cancel_active_flow(account_index)

    # Stop the account's loader and client via the loader's public API.
    # loader_registry.get() raises LoaderNotFoundError if not registered —
    # catch it explicitly so the removal continues regardless.
    try:
        loader = loader_registry.get(idx)
        # Unload all modules through the loader's public unload_all() path so
        # that each module's teardown() is called correctly, handlers are
        # removed, and background tasks are cancelled before the client closes.
        loader.unload_all()
        client = loader.client
        if client is not None and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass
        loader_registry.remove(idx)
        log.info("Account #%d client disconnected for removal.", idx)
    except LoaderNotFoundError:
        # Account was never fully started or was already removed — not an error
        log.info("Account #%d had no registered loader (was not running).", idx)
    except Exception as exc:
        log.warning("Account #%d stop error: %s", idx, exc)

    # Remove from in-memory accounts list
    config.ACCOUNTS[:] = [a for a in config.ACCOUNTS if a.index != idx]

    # Delete account folder from disk
    account_dir = config.ACCOUNTS_DIR / str(idx)
    try:
        shutil.rmtree(account_dir)
        log.info("Account #%d folder deleted: %s", idx, account_dir)
    except Exception as exc:
        log.error("Account #%d folder delete error: %s", idx, exc)
        try:
            await flow.reply(
                f"⚠️ اکانت #{idx} متوقف شد ولی حذف فولدر با خطا مواجه شد:\n`{exc}`"
            )
        except Exception:
            pass
        return

    try:
        await flow.reply(
            f"✅ **اکانت #{idx} حذف شد.**\n\n"
            f"• شماره: `{target.phone or 'N/A'}`\n"
            f"• سشن و فایل‌ها پاک شدن."
        )
    except Exception:
        pass

    log.info("Account #%d fully removed.", idx)


# ── Public API ───────────────────────────────────────────────────────────────

__all__ = [
    "start_add_flow",
    "start_remove_flow",
    "cancel_active_flow",
    "is_flow_active",
]