"""
config.py
════════════════════════════════════════════════════════════════
Multi-Account Userbot — Configuration

Account discovery
─────────────────
Accounts are loaded from numeric sub-folders of `accounts/`::

    accounts/
    ├── 1/
    │   ├── account.json     ← credentials
    │   └── session.session  ← Telethon session file (auto-created on login)
    ├── 2/
    │   └── …
    └── …

`account.json` schema::

    {
        "api_id":   12345678,
        "api_hash": "abcdef0123456789abcdef0123456789",
        "phone":    "+989123456789"
    }

Connection
──────────
Direct connection only (no proxy support). For bypassing restrictions,
use system-level VPN (WireGuard, OpenVPN, V2Ray) on Termux or Windows.

Add / manage accounts
─────────────────────
python add_account.py
════════════════════════════════════════════════════════════════
"""
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


# ── Environment loader ────────────────────────────────────────────────────────

def _load_env_file(env_path: Path) -> None:
    """Minimal `.env` parser used when `python-dotenv` is not installed."""
    if not env_path.exists():
        return
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


try:
    from dotenv import load_dotenv as _load_dotenv  # type: ignore[import-untyped]
    _load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    _load_env_file(Path(__file__).parent / ".env")


# ── Typed environment helpers ─────────────────────────────────────────────────

def _env(key: str, default: str) -> str:
    return (os.environ.get(key, default) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


# ── Directory layout ──────────────────────────────────────────────────────────

BASE_DIR:     Path = Path(__file__).parent
ACCOUNTS_DIR: Path = BASE_DIR / "accounts"
DATA_DIR:     Path = BASE_DIR / "data"
SETTINGS_DIR: Path = DATA_DIR / "settings"
LOGS_DIR:     Path = DATA_DIR / "logs"
MODULES_DIR:  Path = BASE_DIR / "modules"


def ensure_dirs(extra_dirs: list[Path] | None = None) -> None:
    """Create all required runtime directories, including any extra_dirs."""
    for d in (DATA_DIR, SETTINGS_DIR, LOGS_DIR, ACCOUNTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if extra_dirs:
        for d in extra_dirs:
            d.mkdir(parents=True, exist_ok=True)


# ── Global runtime settings ───────────────────────────────────────────────────

# --- Reconnection ---
#: Initial reconnect back-off in seconds.
BACKOFF_START: int = _env_int("BACKOFF_START", 1)
#: Maximum reconnect back-off in seconds.
BACKOFF_MAX: int = _env_int("BACKOFF_MAX", 300)

# --- Clearer modules ---
#: Maximum number of messages scanned by clearer modules.
HISTORY_LIMIT: int = _env_int("HISTORY_LIMIT", 2000)

# --- Logging ---
#: Root logging level. One of: DEBUG, INFO, WARNING, ERROR.
LOG_LEVEL: str = _env("LOG_LEVEL", "DEBUG").upper()


# ── AccountConfig ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AccountConfig:
    """
    Immutable configuration snapshot for a single Telegram account.
    Created once at startup by _load_accounts() and never mutated.

    Attributes:
        index:        Numeric folder name (1, 2, 3, …).
        account_dir:  Absolute path to accounts/N/.
        session_path: Path to the Telethon session file *without* the
                      .session extension.
        api_id:       Telegram API application ID.
        api_hash:     Telegram API application hash.
        phone:        E.164 phone number, e.g. "+989123456789".
        log_file:     Absolute path to the per-account log file.
        settings_dir: Absolute path to the per-account settings directory.
    """
    index:        int
    account_dir:  Path
    session_path: str
    api_id:       int
    api_hash:     str
    phone:        str
    log_file:     str
    settings_dir: Path


# ── Account discovery ─────────────────────────────────────────────────────────

def _load_accounts() -> list[AccountConfig]:
    """
    Scan `accounts/` and return one `AccountConfig` per valid sub-folder.
    Exits with a descriptive error if no accounts are found or configured.
    """
    ACCOUNTS_DIR.mkdir(exist_ok=True)

    folders: list[Path] = sorted(
        [p for p in ACCOUNTS_DIR.iterdir() if p.is_dir() and p.name.isdigit()],
        key=lambda p: int(p.name),
    )

    if not folders:
        sys.exit(
            "\n[CONFIG] No account folders found in accounts/.\n"
            "         Run:  python add_account.py\n"
        )

    accounts: list[AccountConfig] = []

    for folder in folders:
        idx      = int(folder.name)
        cfg_file = folder / "account.json"

        if not cfg_file.exists():
            print(f"[CONFIG] #{idx}: missing account.json — skipped.", file=sys.stderr)
            continue

        try:
            raw: dict = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[CONFIG] #{idx}: cannot read account.json — {exc}", file=sys.stderr)
            continue

        try:
            api_id = int(raw.get("api_id", 0))
        except (ValueError, TypeError):
            api_id = 0

        api_hash: str = str(raw.get("api_hash", "")).strip()

        if not api_id or not api_hash:
            print(f"[CONFIG] #{idx}: missing api_id or api_hash — skipped.", file=sys.stderr)
            continue

        accounts.append(
            AccountConfig(
                index        = idx,
                account_dir  = folder,
                session_path = str(folder / "session"),
                api_id       = api_id,
                api_hash     = api_hash,
                phone        = str(raw.get("phone", "")).strip(),
                log_file     = str(LOGS_DIR / f"account{idx}.log"),
                settings_dir = SETTINGS_DIR / f"account{idx}",
            )
        )

    if not accounts:
        sys.exit(
            "\n[CONFIG] No valid accounts found.\n"
            "         Check your account.json files.\n"
        )

    return accounts


# ── Runtime globals ───────────────────────────────────────────────────────────

#: Immutable list of account configurations, loaded at startup.
ACCOUNTS: list[AccountConfig] = _load_accounts()