"""
core
════════════════════════════════════════════════════════════════
Core infrastructure for the Multi-Account Userbot.

Sub-modules
───────────
client          — TelegramClient factory per account (direct connection)
exceptions      — structured exception hierarchy
loader          — per-account plugin loader with hot-reload
logger          — centralised structured logging
plugin_registry — global loader + plugin metadata registries
reconnector     — per-account reconnect loop with exponential backoff
watcher         — file-change callbacks for config + module hot-reload
account_manager — interactive add/remove account flows

Note:
This version uses direct connection only (no proxy support).
For bypassing network restrictions, use a system-level VPN
(WireGuard, OpenVPN, V2Ray) on Termux or Windows.
════════════════════════════════════════════════════════════════
"""
from core import (
    client,
    exceptions,
    loader,
    logger,
    plugin_registry,
    reconnector,
    watcher,
    account_manager,
)

__all__ = [
    "client",
    "exceptions",
    "loader",
    "logger",
    "plugin_registry",
    "reconnector",
    "watcher",
    "account_manager",
]