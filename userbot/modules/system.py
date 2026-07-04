"""
modules/system.py
════════════════════════════════════════════════════════════════
System — Owner management commands (read-only subset).

All commands work only inside Saved Messages and require the message
to be outgoing (sent by the account owner).

Commands:
• `.modules`  — list all loaded plugins
• `.account`  — show current account info
• `.stats`    — show system statistics (uptime, modules, etc.)
• `.ping`     — test latency to Telegram servers

Features:
• Auto-delete command output after 8 seconds (keeps Saved Messages clean)
• Silent logging (DEBUG level for routine operations)
════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient, events

import config
from core.exceptions import LoaderNotFoundError
from core.logger import get_logger
from core.plugin_registry import loader_registry
from modules.base import Module

if TYPE_CHECKING:
    from config import AccountConfig

log = get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────

_AUTO_DELETE_DELAY = 8.0  # seconds before auto-deleting command output


# ── Command set ───────────────────────────────────────────────────────────────

_OWNER_COMMANDS: frozenset[str] = frozenset({
    ".modules", ".account", ".stats", ".ping",
})


# ── Module ────────────────────────────────────────────────────────────────────

class SystemModule(Module):
    """Owner management commands (Saved Messages only, read-only subset)."""

    name = "system"

    def __init__(self, cfg: "AccountConfig") -> None:
        super().__init__(cfg)
        self._pending_tasks: list[asyncio.Task] = []
        self._start_time: float = time.time()

    def setup(self, client: TelegramClient) -> None:
        self._add_handler(client, events.NewMessage(outgoing=True), self._on_outgoing)
        self._log_info("SystemModule ready.")

    def teardown(self, client: TelegramClient) -> None:
        for t in self._pending_tasks:
            t.cancel()
        self._pending_tasks.clear()
        super().teardown(client)

    # ── Helper: track a fire-and-forget task for teardown ─────────────────────

    def _track_task(self, coro, *, name: str | None = None) -> asyncio.Task:
        """
        Create a task, append it to _pending_tasks, and remove it automatically
        when it finishes. This ensures teardown() can cancel all outstanding
        tasks on hot-reload without leaking references.
        """
        task = asyncio.create_task(coro, name=name)
        self._pending_tasks.append(task)
        task.add_done_callback(
            lambda t: self._pending_tasks.remove(t) if t in self._pending_tasks else None
        )
        return task

    # ── Helper: Auto-delete after delay ───────────────────────────────────────

    async def _schedule_delete(self, message, delay: float = _AUTO_DELETE_DELAY) -> None:
        """Schedule message deletion after a delay."""
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception:
            pass

    async def _edit_and_auto_delete(
        self,
        event,
        text: str,
        delay: float = _AUTO_DELETE_DELAY,
        **kwargs,
    ) -> None:
        """Edit message and schedule auto-deletion after delay."""
        await self._safe_edit(event, text, **kwargs)
        self._track_task(
            self._schedule_delete(event, delay),
            name=f"auto_delete_a{self.cfg.index}",
        )

    # ── Helper: Get loader from registry ──────────────────────────────────────

    def _get_loader(self):
        """
        Get the AccountLoader for this account from the global registry.

        Returns None if the loader is not registered (e.g. during startup),
        rather than raising LoaderNotFoundError.
        """
        try:
            return loader_registry.get(self.cfg.index)
        except LoaderNotFoundError:
            return None

    # ── Owner check ───────────────────────────────────────────────────────────

    async def _is_owner_saved(self, event) -> bool:
        """
        Return True when the message is outgoing and sent in Saved Messages.

        This replaces the old admin check. Since this is a personal tool,
        all accounts belong to the owner. The `event.out` check is
        synchronous (no API call) and reliable.
        """
        me_id = await self._get_me_id(event.client)
        return event.out and event.chat_id == me_id

    # ── Outgoing handler (commands) ───────────────────────────────────────────

    async def _on_outgoing(self, event) -> None:
        text = (event.raw_text or "").strip()
        if not text:
            return

        parts = text.split()
        if not parts:
            return

        cmd = parts[0].lower()
        if cmd not in _OWNER_COMMANDS:
            return

        # Only process if sent by owner in Saved Messages
        if not await self._is_owner_saved(event):
            return

        # ── Command dispatch table ────────────────────────────────────────────
        dispatch = {
            ".modules": self._cmd_modules,
            ".account": self._cmd_account,
            ".stats":   self._cmd_stats,
            ".ping":    self._cmd_ping,
        }

        handler = dispatch.get(cmd)
        if handler:
            await handler(event)

    # ── .modules ──────────────────────────────────────────────────────────────

    async def _cmd_modules(self, event) -> None:
        loader = self._get_loader()
        mods = loader.list_modules() if loader else []
        body = (
            "\n".join(f"• `{m}`" for m in mods)
            if mods else "_No modules loaded._"
        )
        await self._edit_and_auto_delete(
            event,
            f"📦 **Loaded modules — Account #{self.cfg.index} ({len(mods)}):**\n\n{body}"
        )
        self._log_debug("[Account%d] .modules executed", self.cfg.index)

    # ── .account ──────────────────────────────────────────────────────────────

    async def _cmd_account(self, event) -> None:
        """Show current account information."""
        cfg = self.cfg

        try:
            me = await event.client.get_me()
        except Exception as exc:
            self._log_error("Failed to get me: %s", exc)
            await self._edit_and_auto_delete(event, f"❌ خطا در دریافت اطلاعات: `{exc}`")
            return

        client = event.client
        if client.is_connected():
            connection_status = "✅ متصل"
        else:
            connection_status = "❌ قطع"

        session_ok = Path(cfg.session_path + ".session").exists()

        await self._edit_and_auto_delete(event,
            f"👤 **Account #{cfg.index}**\n\n"
            f"• **User ID:** `{me.id}`\n"
            f"• **Username:** @{me.username or 'N/A'}\n"
            f"• **Phone:** `{cfg.phone or 'N/A'}`\n"
            f"• **API ID:** `{cfg.api_id}`\n"
            f"• **Session:** {'✅' if session_ok else '❌ missing'}\n"
            f"• **Connection:** `{connection_status}`\n"
        )
        self._log_debug("[Account%d] .account executed", self.cfg.index)

    # ── .stats ────────────────────────────────────────────────────────────────

    async def _cmd_stats(self, event) -> None:
        """Show system statistics."""
        uptime_seconds = int(time.time() - self._start_time)
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = ""
        if days > 0:
            uptime_str += f"{days}d "
        uptime_str += f"{hours}h {minutes}m {seconds}s"

        total_modules = 0
        connected_accounts = 0
        for loader in loader_registry.all().values():
            total_modules += len(loader.list_modules())
            client = loader.client
            if client is not None and client.is_connected():
                connected_accounts += 1

        await self._edit_and_auto_delete(event,
            f"📊 **آمار یوزربات**\n\n"
            f"• اکانت‌های فعال: `{len(config.ACCOUNTS)}`\n"
            f"• اکانت‌های متصل: `{connected_accounts}`\n"
            f"• ماژول‌های لود شده: `{total_modules}`\n"
            f"• Uptime: `{uptime_str}`\n"
        )
        self._log_debug("[Account%d] .stats executed", self.cfg.index)

    # ── .ping ─────────────────────────────────────────────────────────────────

    async def _cmd_ping(self, event) -> None:
        """Measure latency to Telegram servers."""
        from telethon.tl.functions.updates import GetStateRequest

        # Measure API call latency
        start = time.monotonic()
        try:
            await event.client(GetStateRequest())
            end = time.monotonic()
            api_latency = (end - start) * 1000
            api_status = f"`{api_latency:.2f} ms`"
            is_success = True
        except Exception:
            api_status = "❌ خطا"
            is_success = False
            api_latency = 0.0

        # Measure message edit latency
        start_edit = time.monotonic()
        await event.edit("🏓 **Pong!**")
        end_edit = time.monotonic()
        edit_latency = (end_edit - start_edit) * 1000

        # Evaluate connection quality
        if is_success:
            if api_latency < 150:
                quality = "🟢 عالی"
            elif api_latency < 300:
                quality = "🟡 خوب"
            elif api_latency < 800:
                quality = "🟠 متوسط"
            else:
                quality = "🔴 ضعیف"
        else:
            quality = "❌ قطع"

        await self._edit_and_auto_delete(event,
            f"🏓 **Pong!**\n\n"
            f"• **API Latency:** {api_status}\n"
            f"• **Edit Latency:** `{edit_latency:.2f} ms`\n"
            f"• **کیفیت اتصال:** {quality}"
        )
        self._log_debug("[Account%d] .ping executed", self.cfg.index)


# ── Help Texts (در انتهای ماژول طبق قوانین) ──────────────────────────────────

help_text = (
    "• `.modules` | لیست ماژول‌های فعال\n"
    "• `.account` | اطلاعات اکانت فعلی\n"
    "• `.stats`   | آمار کلی سیستم\n"
    "• `.ping`    | تست تأخیر اتصال\n"
)

help_extra = (
    "دستورات سیستم (فقط خواندنی)\n\n"
    "همه این دستورات فقط در Saved Messages کار می‌کنند.\n\n"
    "وضعیت سیستم:\n"
    "• `.modules` | نمایش لیست همه ماژول‌های فعال این اکانت\n"
    "• `.stats`   | آمار کلی شامل اکانت‌ها، ماژول‌ها و uptime\n"
    "• `.ping`    | تست سرعت پاسخگویی و تأخیر اتصال به سرورهای تلگرام\n\n"
    "مدیریت اکانت‌ها:\n"
    "• `.account` | اطلاعات کامل اکانت فعلی\n\n"
    "مثال‌ها:\n"
    "• `.modules` | نمایش ۹ ماژول فعال\n"
    "• `.ping`    | بررسی تأخیر اتصال (API و Edit Latency)\n"
    "• `.account` | نمایش User ID, API ID, Connection\n"
    "• `.stats`   | نمایش uptime و تعداد اکانت‌های متصل\n\n"
    "نکات مهم:\n"
    "• اتصال به‌صورت مستقیم انجام می‌شود\n"
    "• برای دور زدن محدودیت‌های شبکه از VPN سیستمی استفاده کنید\n"
    "• خروجی همه دستورات پس از ۸ ثانیه به‌صورت خودکار حذف می‌شود\n"
)

SystemModule.help_text = help_text
SystemModule.help_extra = help_extra


def create_module(cfg: "AccountConfig") -> Module:
    return SystemModule(cfg)