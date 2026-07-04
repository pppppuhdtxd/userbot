```markdown
# Multi-Account Telegram Userbot

A professional, async, hot-reload-capable Telegram account management system
built with Python 3.11+ and [Telethon](https://docs.telethon.dev/).

**Current version:** `1.6.0`

See [CHANGELOG.md](CHANGELOG.md) for the full history.

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Architecture Overview](#architecture-overview)
- [Message Classification System](#message-classification-system)
- [Account Setup](#account-setup)
- [Configuration](#configuration)
- [Available Commands](#available-commands)
- [Hot-Reload System](#hot-reload-system)
- [Writing a New Module](#writing-a-new-module)
- [Versioning Guide](#versioning-guide)
- [FAQ](#faq)

---

## Features

| Feature | Description |
| --- | --- |
| **Multi-account** | Run any number of Telegram accounts simultaneously |
| **Hot-reload** | Add, remove, or edit module files while the bot is running — no restart |
| **MTProxy support** | Auto-selects fastest proxy; health monitor switches on failure |
| **Message classification** | Unified priority system (`file > vid > pic > link > txt > other`) across all modules |
| **Category-based help** | Beautiful, RTL/LTR-friendly help output with 7 logical categories |
| **Permission-aware clearing** | `clear` checks admin rights before scanning and reports accurately |
| **Reaction commands** | Execute commands by reacting to messages with emojis (works on bots, users, channels) |
| **Plugin registry** | Rich metadata, introspection, and runtime management API |
| **File watchers** | `proxies.txt` and `account.json` changes apply instantly |
| **Semantic versioning** | Every change tracked in `VERSION` + `CHANGELOG.md` |
| **Python 3.11+** | Native union types, `match` statements, `slots=True` dataclasses |

---

## Requirements

- Python 3.11 or newer
- A Telegram API app from [my.telegram.org/apps](https://my.telegram.org/apps)

---

## Quick Start

```bash
# 1. Clone / extract the project
cd userbot_v2

# 2. Create a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Add your first account
cd userbot/
python add_account.py

# 5. (Optional) Configure environment variables
cp ../.env.example .env
# Edit .env with your settings

# 6. Run
python main.py
```

---

## Project Structure

```
userbot_v2/
├── VERSION                     ← single source of truth for version
├── CHANGELOG.md                ← all changes since the beginning
├── README.md                   ← this file
├── requirements.txt
├── .env.example
│
└── userbot/
    ├── __init__.py             ← exposes __version__
    ├── main.py                 ← entry point
    ├── config.py               ← AccountConfig, global settings
    │
    ├── core/
    │   ├── __init__.py
    │   ├── exceptions.py       ← structured exception hierarchy
    │   ├── plugin_registry.py  ← AccountLoaderRegistry + PluginMetadataStore
    │   ├── client.py           ← TelegramClient factory
    │   ├── loader.py           ← per-account plugin loader + hot-reload
    │   ├── logger.py           ← centralised structured logging
    │   ├── proxy.py            ← MTProxy / direct connection manager
    │   ├── reconnector.py      ← per-account reconnect loop
    │   ├── watcher.py          ← file-change callbacks
    │   └── account_manager.py  ← interactive add/remove account flows
    │
    ├── helpers/
    │   ├── __init__.py
    │   └── utils.py            ← shared utilities + classify_message()
    │
    ├── modules/
    │   ├── __init__.py
    │   ├── base.py             ← abstract Module base class
    │   ├── system.py           ← admin management commands
    │   ├── help_handler.py     ← `help` + `help more` commands
    │   ├── clearer.py          ← manual message clearing
    │   ├── auto_clearer.py     ← automatic message clearing
    │   ├── auto_forwarder.py   ← auto-forward bot messages
    │   ├── join_left.py        ← join/leave chats + folder management
    │   ├── reaction_commands.py ← execute commands via emoji reactions
    │   ├── info_handler.py     ← message info (reply)
    │   └── whois_handler.py    ← user/chat info
    │
    ├── accounts/
    │   ├── 1/
    │   │   ├── account.json    ← credentials + flags
    │   │   └── session.session ← Telethon session (auto-created)
    │   └── 2/ …
    │
    ├── proxies.txt             ← MTProxy list
    └── data/
        ├── logs/               ← rotating log files
        └── settings/           ← per-account runtime settings
            ├── account1/
            │   ├── join_left.json
            │   ├── reactions.json
            │   └── autoclear.json
            └── account2/ …
```

---

## Architecture Overview

### Plugin Lifecycle

```
AccountLoader.load_all(client)
    └─ for each .py in modules/
        ├─ importlib.util.spec_from_file_location()
        ├─ create_module(cfg, loader)  ← factory call
        ├─ instance.setup(client)      ← handler registration
        └─ plugin_store.upsert(metadata)
```

### Hot-reload (watchdog triggers)

```
AccountLoader.reload_module(stem)
    ├─ instance.teardown(client)   ← remove handlers
    └─ [re-import + re-setup]
```

### Connection Flow

```
main._main()
    └─ proxy.initialize()
        ├─ _probe_direct()        → if OK: done (no proxy)
        └─ _probe_proxy() × N     → pick fastest
                │
                └─ ProxyHealthMonitor (background)
                       └─ on failure × 3: reselect()
                              └─ AccountReconnector._on_connection_change()
                                     └─ rebuild client on next cycle
```

### Registry Singletons

| Singleton | Module | Purpose |
| --- | --- | --- |
| `loader_registry` | `core.plugin_registry` | `int` → `AccountLoader` |
| `plugin_store` | `core.plugin_registry` | `(int, stem)` → `PluginMetadata` |

---

## Message Classification System

Every message processed by the bot is classified into **exactly ONE type**
based on strict priority. This ensures consistent behavior across all modules
(`clearer`, `auto_clearer`, `info_handler`, etc.).

### Priority Order (highest → lowest)

| Priority | Type | Description |
| --- | --- | --- |
| 🥇 1 | `file` | Document with filename, no video/audio/sticker attributes |
| 🥈 2 | `vid` | Document with `DocumentAttributeVideo` |
| 🥉 3 | `pic` | Photo (`MessageMediaPhoto`) or photo-like document |
| 4 | `link` | WebPage preview or URL entity in text |
| 5 | `txt` | Plain text message (no media, no links) |
| 6 | `other` | Sticker, voice, contact, location, poll, etc. |

### Example Classification

| Message | Classified As |
| --- | --- |
| `"Hello world"` | `txt` |
| `"Check out https://github.com/file.exe"` (with WebPage) | `link` |
| `"Hello"` + photo | `pic` |
| `"Check this"` + video | `vid` |
| PDF attachment | `file` |
| Sticker | `other` |

---

## Account Setup

### Automatic (recommended)

```bash
cd userbot/
python add_account.py
```

Follow the interactive prompts to enter your API credentials and phone number.

### Manual

Create `accounts/N/account.json` (replace `N` with a number):

```json
{
    "api_id":   12345678,
    "api_hash": "your_api_hash_here",
    "phone":    "+989123456789",
    "is_admin": true
}
```

Then restart the bot, or use the `.addaccount` Telegram command to add it live.

---

## Configuration

All settings can be overridden via environment variables or a `.env` file
in the `userbot/` directory.

| Variable | Default | Description |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `PROXY_FILE` | `proxies.txt` | Path to MTProxy list (relative to `userbot/`) |
| `BACKOFF_START` | `1` | Initial reconnect back-off in seconds |
| `BACKOFF_MAX` | `300` | Maximum reconnect back-off in seconds |
| `HISTORY_LIMIT` | `2000` | Max messages scanned by clearer modules |

---

## Available Commands

All commands are sent as outgoing messages. Some require Saved Messages
and/or admin privileges.

### Admin Commands (require `is_admin: true`, Saved Messages only)

| Command | Description |
| --- | --- |
| `.modules` | List all loaded plugins |
| `.reload` | Reload all plugins |
| `.reload <name>` | Reload a specific plugin |
| `.restart` | Restart the process |
| `.account` | Show current account info |
| `.accounts` | List all configured accounts |
| `.addaccount` | Interactive add-account flow |
| `.removeaccount <n>` | Interactive remove-account flow |
| `.cancelflow` | Cancel an active flow |
| `.stats` | Show system statistics |
| `.proxy` | Show proxy status |
| `.version` | Show current version |

### Help System

| Command | Description |
| --- | --- |
| `help` | Category-based compact help (7 logical groups) |
| `help more` | Extended help with examples and tips (Saved Messages only) |

### Reaction Commands (`reaction_commands` module)

Execute commands by reacting to messages with emojis. Works on any message
(bots, users, channels, self). Configuration is per-account in
`data/settings/account{N}/reactions.json`.

**Management commands (in Saved Messages only):**

| Command | Description |
| --- | --- |
| `reactions` | Show all configured emoji→command mappings |
| `reaction add <emoji> <command>` | Add a new mapping |
| `reaction remove <emoji>` | Remove a mapping |
| `reaction clear` | Remove all mappings |

**Example usage:**

```
# Setup (in Saved Messages):
reaction add 👍 join       ← react with 👍 on any message → execute "join"
reaction add 👋 left       ← react with 👋 → execute "left"
reaction add 👌 clear txt  ← react with 👌 → clear text messages
reaction add 🔥 whois      ← react with 🔥 → show sender info

# Then in any chat, react with the configured emoji on any message!
```

### Clearer (`clear`)

Works in **any chat**. Permission-aware reporting detects if you have
admin rights and reports accurately.

| Command | Description |
| --- | --- |
| `clear` | Delete text + link messages (default) |
| `clear all` | Delete all messages (including stickers, voice, etc.) |
| `clear media` | Delete real media only (pic + vid + file, **no links**) |
| `clear pic` | Photos only |
| `clear vid` | Videos / GIFs only |
| `clear file` | File attachments only |
| `clear txt` | Text-only messages (no links) |
| `clear link` | Messages with WebPage previews or URL entities |
| `clear self` | Your own messages |
| `clear bot` | Bot-sent messages |

**Combining filters:**
- `clear txt self` → only your text messages
- `clear media bot` → only bot's real media
- `clear all self` → all of your messages

**Strict validation:** Invalid arguments (e.g. `clear fvjnfvo`) are silently
ignored to prevent false positives.

### Auto-Clearer (`autoclear`)

Automatically delete bot messages based on type and scope.

| Command | Description |
| --- | --- |
| `autoclear <type> <on/off> <1/2/3>` | Enable/disable auto-clear |
| `autoclear status` | Show current settings (Saved Messages only) |

**Types:** `pic`, `txt`, `vid`, `file`, `link`, `media` (pic+vid+file)

**Scopes:**
- `1` → bot messages only
- `2` → your messages only
- `3` → both

**Context:**
- In **Saved Messages** → global setting (all bots)
- In a **bot chat** → bot-specific setting

### Auto-Forwarder (`autofor`)

Automatically forward bot messages back to the same bot.

| Command | Description |
| --- | --- |
| `autofor <type> <on/off>` | Enable/disable auto-forward |
| `forward status` | Show current settings (Saved Messages only) |

**Types:** `txt`, `pic`, `vid`, `file`, `caption`, `all`

### Join / Left

| Command | Description |
| --- | --- |
| `join` (reply) | Join all chats found in the replied message |
| `left` (reply) | Leave all chats found in the replied message |
| `join delay <sec>` | Set delay between join attempts |
| `folder` | Create / reset the `joined` folder (Saved Messages only) |
| `list` | List chats in the `joined` folder (Saved Messages only) |
| `autoleave <days>` | Auto-leave joined chats after N days (Saved Messages only) |
| `autoleave off` | Disable auto-leave |
| `autoleave status` | Show auto-leave status |

### Info & Whois (work in any chat)

| Command | Description |
| --- | --- |
| `info` (reply) | Show message / media metadata + classification |
| `whois` | Info about current chat |
| `whois @username` | Info about a specific user/channel/group |
| `whois 123456789` | Info by numeric ID |
| `whois` (reply) | Info about replied message sender |

---

## Hot-Reload System

The bot monitors the `modules/` directory with `watchdog`. When a `.py`
file is created, modified, or deleted:

1. The old module instance's `teardown()` is called (handlers removed).
2. The file is re-imported from disk.
3. A new instance is created and `setup()` is called.
4. Plugin metadata in `plugin_store` is updated.

This means you can iterate on module code without restarting the bot.

Syntax errors in a module are caught and logged — the rest of the plugins
continue running normally.

You can also trigger a manual reload from Telegram:

```
.reload clearer
.reload           ← reloads everything
```

---

## Writing a New Module

1. Create `userbot/modules/my_module.py`.
2. Implement the module:

```python
from telethon import TelegramClient, events
from modules.base import Module


class MyModule(Module):
    name = "my_module"
    is_admin_only = False  # Set to True for admin-restricted modules

    # Compact help text shown in `help` (2-5 lines max)
    help_text = "• `mycommand` — does something cool\n"

    # Extended help shown via `help more` (optional)
    help_extra = (
        "🎯 **My Module - Extended Info:**\n\n"
        "**Commands:**\n"
        "• `mycommand` — detailed description\n"
        "• `mycommand arg` — with arguments\n\n"
        "**Examples:**\n"
        "• `mycommand` → does X\n"
        "• `mycommand foo` → does Y\n\n"
        "**Notes:**\n"
        "• Important edge case 1\n"
        "• Important edge case 2\n"
    )

    def setup(self, client: TelegramClient) -> None:
        self._add_handler(client, events.NewMessage(outgoing=True), self._on_msg)

    async def _on_msg(self, event) -> None:
        if (event.raw_text or "").strip().lower() != "mycommand":
            return
        await event.edit("Hello from my_module!")


def create_module(cfg) -> Module:
    return MyModule(cfg)
```

3. The file is picked up automatically (hot-reload) — no restart needed.

### Key Module Attributes

| Attribute | Required | Description |
| --- | --- | --- |
| `name` | ✅ | Short identifier (used in logs and plugin store) |
| `help_text` | ✅ | Compact help shown in `help` (2-5 lines) |
| `help_extra` | ❌ | Extended help shown in `help more` |
| `is_admin_only` | ❌ | If `True`, hidden from non-admin users in help |

### Recommended Methods to Use

| Method | Purpose |
| --- | --- |
| `self._add_handler(client, builder, callback)` | Register event handler (dedup-safe) |
| `self._safe_edit(message, text)` | Edit message with error handling |
| `self._get_me_id(client)` | Cached self ID lookup |
| `self._log_info/warning/error/debug(msg)` | Structured per-account logging |

---

## Versioning Guide

This project follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).

| Part | When to bump |
| --- | --- |
| **MAJOR** | Breaking architectural change or full rewrite |
| **MINOR** | New module, feature, or meaningful enhancement |
| **PATCH** | Bug fix, refactor, doc update, or minor improvement |

### After every change

1. Update `VERSION` (project root)
2. `userbot/__init__.py` reads the version automatically from `VERSION`
3. Prepend a new entry to `CHANGELOG.md`

### Version rule for AI-assisted changes

Every time an AI assistant applies a change, it must:

- Increment the version (PATCH for fixes, MINOR for features)
- Update `VERSION` and `CHANGELOG.md`
- State explicitly: **"Change applied. New version: X.Y.Z"**

---

## FAQ

**Q: Can I run this on Windows?**

A: Yes. Replace `source .venv/bin/activate` with `.venv\Scripts\activate`.

---

**Q: How do I add a proxy?**

A: Paste MTProxy URLs into `userbot/proxies.txt`. The format is:

```
https://t.me/proxy?server=…&port=…&secret=…
```

The file is watched at runtime — changes apply without restart.

---

**Q: How do I make an account an admin?**

A: Set `"is_admin": true` in its `account.json`, or edit the file while the
bot is running (it is watched automatically).

---

**Q: Where are the log files?**

A: `userbot/data/logs/main.log` and per-account `account1.log`, `account2.log`, etc.

---

**Q: How do reaction commands work?**

A: The `reaction_commands` module uses a combination of event listeners and
smart polling (every 1.5s) to detect when you react to a message. It then
directly invokes the target module's handler via a `_MockEvent` object,
which is faster and more reliable than sending a command message.

---

**Q: Can I use reaction commands on bot messages?**

A: Yes! The smart polling method specifically handles bot messages and other
edge cases where Telegram doesn't send `UpdateMessageReactions` events.

---

**Q: What's the difference between `clear txt` and `clear link`?**

A: `clear txt` deletes only plain text messages (no media, no links).
`clear link` deletes messages with WebPage previews or URL entities.
A message containing a download link like `https://github.com/file.exe`
is classified as `link`, not `txt`.

---

**Q: Why does `clear media` not delete link messages?**

A: Starting from v1.6.0, `media` refers to **real attachments** only
(photos, videos, files). WebPage previews are metadata, not media.
Use `clear link` or `clear all` for link messages.

---

**Q: What happened to the AI modules?**

A: `ai_assistant.py` and `ai_bot.py` were removed in version 1.0.0.
See `CHANGELOG.md` for the full rationale.

---

**Q: How do I write a custom module?**

A: See [Writing a New Module](#writing-a-new-module). The minimum requirement
is a class inheriting from `Module` with a `name`, `help_text`, and a
`create_module(cfg)` factory function.

---

**Q: Can modules be admin-only?**

A: Yes. Set `is_admin_only = True` on your `Module` subclass. The help
system will hide it from non-admin users automatically.
```