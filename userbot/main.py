"""
main.py
════════════════════════════════════════════════════════════════
Multi-Account Userbot — Entry Point (Direct Connection Only)

Startup order:
1. Create required runtime directories
2. Configure root logging (console + `main.log`)
3. Register file watchers (independent of accounts)
4. Start Ctrl+R restart listener (Windows only)
5. Setup SIGUSR1 signal handler (Unix/Termux only)
6. Start all accounts concurrently (all are equal)

Keyboard shortcuts:
- Ctrl+C → Graceful shutdown (normal exit)
- Ctrl+R → Restart (spawn new process + kill current) [Windows only]
- SIGUSR1 → Restart via signal [Unix/Termux/Linux only]

Connection:
- Direct connection only (no proxy support)
- For bypassing restrictions, use system-level VPN
  (WireGuard, OpenVPN, V2Ray) on Termux or Windows

Run:
    cd userbot/
    python main.py
════════════════════════════════════════════════════════════════
"""
import sys
sys.dont_write_bytecode = True  # Prevent __pycache__ folders

import asyncio
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Coroutine, Any

# When running `python main.py` from inside the userbot/ directory, the parent
# folder is not on sys.path, so `from userbot import ...` fails. Fix that here.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import config
from core import logger as log_module
from core.client import AccountClient
from core.loader import AccountLoader
from core.logger import get_logger
from core.reconnector import AccountReconnector
from core.watcher import setup_watchers
from core.plugin_registry import loader_registry

log: logging.Logger

# Restart flag — set by Ctrl+R handler or SIGUSR1, read by main()
_restart_requested: bool = False

# Reference to the accounts-directory Observer so it can be stopped on shutdown.
# setup_watchers() returns it; _main() stores it here for cleanup.
_watcher_observer = None


# ── Keyboard restart handler (Windows) ──────────────────────────────────────

def _keyboard_restart_listener(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """
    Background thread that listens for Ctrl+R to trigger a manual restart.

    Ctrl+R sends byte 0x12 (ASCII 18) via msvcrt.getch().
    When detected, sets the shutdown event to signal the main loop.

    Only works on Windows (uses msvcrt). On other platforms, this thread
    exits immediately and Ctrl+R restart is not available.
    """
    global _restart_requested

    try:
        import msvcrt
    except ImportError:
        # Not Windows — Ctrl+R restart not supported
        return

    while not shutdown_event.is_set():
        try:
            if msvcrt.kbhit():
                key = msvcrt.getch()
                # Ctrl+R = byte 0x12
                if key == b'\x12':
                    print("\n[RESTART] Ctrl+R detected — initiating restart...")
                    _restart_requested = True
                    # Signal the main loop to begin shutdown sequence
                    loop.call_soon_threadsafe(shutdown_event.set)
                    return
        except Exception:
            pass

        # Small sleep to avoid busy-waiting
        time.sleep(0.1)


# ── Signal handler (Unix/Termux/Linux) ──────────────────────────────────────

def _setup_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
) -> None:
    """
    Setup SIGUSR1 signal handler for manual restart on Unix-like systems.

    Works on: Linux, Termux (Android), macOS
    Does NOT work on: Windows (uses Ctrl+R instead)

    Usage from terminal:
        pkill -SIGUSR1 -f main.py
        # OR
        kill -SIGUSR1 <pid>

    This is the Termux/Linux equivalent of Ctrl+R on Windows.
    """
    global _restart_requested

    if sys.platform == "win32":
        # Windows uses Ctrl+R via msvcrt instead
        return

    # SIGUSR1 is available on POSIX systems (Linux, macOS, Termux)
    try:
        sigusr1 = signal.SIGUSR1
    except AttributeError:
        # Platform doesn't support SIGUSR1 (very rare)
        return

    def _sigusr1_handler(signum, frame):
        print("\n[RESTART] SIGUSR1 received — initiating restart...")
        _restart_requested = True
        loop.call_soon_threadsafe(shutdown_event.set)

    try:
        signal.signal(sigusr1, _sigusr1_handler)
        # Log the PID so user knows which process to signal
        print(f"[INFO] PID: {os.getpid()} — send SIGUSR1 to restart")
    except (OSError, ValueError):
        # Signal handler registration failed (e.g., not main thread)
        pass


# ── Per-account runner (unified — all accounts equal) ───────────────────────

async def _start_account(acc_cfg: config.AccountConfig) -> None:
    """
    Build, load modules, connect, and run a single account until cancelled.

    All accounts are equal — no special-casing for account #1.
    File watchers are set up independently in _main() before this runs.

    Order is critical:
    1. Build TelegramClient with direct connection
    2. Load modules (register handlers on client object, not connection)
    3. Connect to Telegram with retry logic
    4. Start reconnector (which monitors and recovers if connection drops)
    """
    label = f"Account{acc_cfg.index}"

    log_module.add_account_handler(
        account_index=acc_cfg.index,
        log_file=acc_cfg.log_file,
        log_level=config.LOG_LEVEL,
    )

    # Step 1: Build client
    ac = AccountClient(acc_cfg)
    client = ac.build()

    # Step 2: Load modules on the (not-yet-connected) client.
    # Telethon registers handlers on the client object, not the connection,
    # so this is safe before connect(). It also closes the race window where
    # a message arrives between connect() and load_all().
    loader = AccountLoader(acc_cfg, config.MODULES_DIR)
    loader.load_all(client)
    loader_registry.register(acc_cfg.index, loader)

    # Step 3: Connect with retry (exponential backoff for mobile networks)
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            await asyncio.wait_for(client.connect(), timeout=30.0)
            log.info("[%s] Connected to Telegram.", label)
            break
        except (asyncio.TimeoutError, Exception) as exc:
            log.warning(
                "[%s] Connect attempt %d/%d failed: %s",
                label, attempt, max_attempts, exc,
            )
            if attempt == max_attempts:
                log.error(
                    "[%s] Initial connect failed — reconnector will keep trying.",
                    label,
                )
            else:
                await asyncio.sleep(min(2 ** attempt, 10))

    # Step 4: Verify authorization (only if connected)
    if client.is_connected():
        try:
            if not await asyncio.wait_for(client.is_user_authorized(), timeout=10.0):
                log.warning("[%s] Not authorized — session may be invalid.", label)
        except Exception as exc:
            log.warning("[%s] Authorization check failed: %s", label, exc)

    # Step 5: Run reconnector + file watcher concurrently
    reconnector = AccountReconnector(ac, loader)
    try:
        await asyncio.gather(
            reconnector.run(),
            loader.watch(),
        )
    except asyncio.CancelledError:
        log.info("[%s] Shutdown requested.", label)
    finally:
        if client and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass
        log.info("[%s] Disconnected.", label)


# ── Main ──────────────────────────────────────────────────────────────────────

async def _main() -> None:
    global log, _watcher_observer

    # ── 1. Directories ────────────────────────────────────────────
    config.ensure_dirs([acc.settings_dir for acc in config.ACCOUNTS])

    # ── 2. Root logging ───────────────────────────────────────────
    log_module.setup(
        log_level=config.LOG_LEVEL,
        log_file=str(config.LOGS_DIR / "main.log"),
    )
    log = get_logger(__name__)

    from userbot import __version__
    log.info("=" * 65)
    log.info("Multi-Account Userbot v%s starting…", __version__)
    log.info("Accounts : %d", len(config.ACCOUNTS))
    for acc in config.ACCOUNTS:
        log.info(
            "  [%d] phone=%-16s  api_id=%s",
            acc.index, acc.phone or "N/A", acc.api_id,
        )
    log.info("Log dir  : %s", config.LOGS_DIR)

    # Platform-specific shortcut hints
    if sys.platform == "win32":
        log.info("Shortcuts: Ctrl+C=exit | Ctrl+R=restart")
    else:
        log.info("Shortcuts: Ctrl+C=exit | SIGUSR1=restart (pkill -SIGUSR1 -f main.py)")

    log.info("=" * 65)

    # ── 3. File watchers (independent of account startup order) ───
    # Store the observer so we can stop it cleanly on shutdown.
    _watcher_observer = setup_watchers(start_account_cb=_start_account)
    log.info("File watchers active.")

    # ── 4. Start Ctrl+R restart listener (Windows only) ───────────
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    keyboard_thread = threading.Thread(
        target=_keyboard_restart_listener,
        args=(loop, shutdown_event),
        daemon=True,
        name="keyboard-listener",
    )
    keyboard_thread.start()

    # ── 5. Setup SIGUSR1 handler (Unix/Termux/macOS) ─────────────
    _setup_signal_handlers(loop, shutdown_event)

    # ── 6. Start all accounts concurrently — all are equal ────────
    tasks = [
        asyncio.create_task(
            _start_account(acc),
            name=f"account{acc.index}",
        )
        for acc in config.ACCOUNTS
    ]

    # Create a watcher task for the shutdown signal (Ctrl+R or SIGUSR1)
    shutdown_task = asyncio.create_task(
        shutdown_event.wait(),
        name="shutdown-wait",
    )

    try:
        # Wait for either: a task completes, or shutdown signal arrives
        done, pending = await asyncio.wait(
            tasks + [shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check if shutdown was triggered by Ctrl+R or SIGUSR1
        if shutdown_task in done:
            log.info("Shutdown — restart requested...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("All accounts stopped. Ready for restart.")
            return

        # A task finished unexpectedly — cancel shutdown watcher and continue
        shutdown_task.cancel()
        try:
            await shutdown_task
        except asyncio.CancelledError:
            pass

        # Wait for remaining account tasks
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    except asyncio.CancelledError:
        log.info("Shutdown — stopping all accounts…")
        for t in tasks:
            t.cancel()
        # Also cancel the shutdown watcher task if it's still pending
        if not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("All accounts stopped.")

    finally:
        # Stop the accounts-directory file watcher observer thread cleanly.
        # Without this the watchdog thread outlives the event loop on every
        # shutdown/restart cycle, accumulating leaked threads.
        if _watcher_observer is not None:
            try:
                _watcher_observer.stop()
                _watcher_observer.join(timeout=5.0)
            except Exception:
                pass
            _watcher_observer = None


def _spawn_restart() -> None:
    """
    Spawn a new instance of the bot and exit the current process.

    Uses subprocess.Popen with CREATE_NEW_PROCESS_GROUP on Windows
    to fully detach the new process, then os._exit(0) to kill the
    current process without asyncio cleanup (avoids hangs).
    """
    cmd = [sys.executable] + sys.argv

    try:
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(
                cmd,
                creationflags=creation_flags,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                cmd,
                start_new_session=True,
                close_fds=True,
            )
        print("[RESTART] New process spawned, exiting current...")
    except Exception as exc:
        print(f"[RESTART] Failed to spawn new process: {exc}")
        return

    # Give a moment for stdout to flush
    time.sleep(0.5)

    # Hard exit — bypass all cleanup (avoids asyncio hangs)
    os._exit(0)


def main() -> None:
    """Entry point — run the async main loop with Ctrl+R/SIGUSR1 restart support."""
    global _restart_requested

    while True:
        _restart_requested = False

        try:
            asyncio.run(_main())
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Ctrl+C received — exiting gracefully.")
            break

        # Check if restart was requested via Ctrl+R or SIGUSR1
        if _restart_requested:
            print("[RESTART] Spawning new process...")
            _spawn_restart()
            break
        else:
            # Normal exit (all accounts finished, or unexpected error)
            break


if __name__ == "__main__":
    main()