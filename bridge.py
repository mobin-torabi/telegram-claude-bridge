"""
Telegram <-> Claude Code bridge.

Listens for Telegram messages from a single authorized user and runs each
message as a prompt through the local Claude Code CLI (headless mode) on this
laptop. Claude's reply is sent back to Telegram. Because it runs with
--dangerously-skip-permissions, Claude can actually do things on the machine
(run commands, edit files, etc.) -- so the ONLY thing standing between a
stranger and your laptop is the chat-id allowlist. Keep your bot token secret.

Config comes from a .env file next to this script (see .env.example).
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

# Windows consoles default to cp1252 and choke on emoji in our logs.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) into os.environ."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()
WORKING_DIR = os.environ.get("WORKING_DIR", str(Path.home())).strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()  # optional, e.g. claude-opus-4-8
# Hard ceiling so a runaway task can't block the bot forever (seconds).
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
# Optional proxy for reaching api.telegram.org (e.g. where Telegram is blocked).
# Example: http://127.0.0.1:10808  (v2rayN's default local proxy)
PROXY = os.environ.get("PROXY", "").strip()
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
# Long-poll seconds. Keep short when behind a proxy that drops idle tunnels.
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "20" if not PROXY else "10"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_LIMIT = 4000  # Telegram hard limit is 4096; leave headroom.

SYSTEM_PROMPT = (
    "You are being operated remotely by your owner through a Telegram bot, "
    "running on their Windows laptop with full permissions. They may be on a "
    "phone, so keep replies concise and readable on a small screen. When asked "
    "to do something on the machine, just do it and report the result briefly. "
    "Do not ask for confirmation for routine actions; you are trusted here."
)

CLAUDE_BIN = shutil.which("claude") or "claude"

# Per-conversation Claude session id, so the bot keeps context between messages.
_session_id = None
_claude_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Telegram helpers
# --------------------------------------------------------------------------- #

def tg(method: str, **params):
    try:
        # Read timeout must exceed the long-poll window we ask Telegram for.
        r = requests.post(f"{API}/{method}", data=params,
                          timeout=POLL_TIMEOUT + 15, proxies=PROXIES)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[tg] {method} failed: {exc}", file=sys.stderr)
        return {"ok": False}


def send(chat_id, text: str) -> None:
    """Send text to Telegram, splitting on the 4096-char limit."""
    text = text if text.strip() else "(empty reply)"
    for i in range(0, len(text), TG_LIMIT):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + TG_LIMIT],
           disable_web_page_preview=True)


def typing_loop(chat_id, stop: threading.Event) -> None:
    """Keep the 'typing…' indicator alive while Claude works."""
    while not stop.is_set():
        tg("sendChatAction", chat_id=chat_id, action="typing")
        stop.wait(4)


# --------------------------------------------------------------------------- #
# Claude invocation
# --------------------------------------------------------------------------- #

def run_claude(prompt: str) -> str:
    """Run one prompt through the Claude Code CLI and return its text result."""
    global _session_id

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if _session_id:
        cmd += ["--resume", _session_id]
    else:
        _session_id = str(uuid.uuid4())
        cmd += ["--session-id", _session_id]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]

    try:
        proc = subprocess.run(
            cmd, cwd=WORKING_DIR, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ Timed out after {CLAUDE_TIMEOUT}s. Try /new and a smaller step."
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Could not run Claude: {exc}"

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()
        return f"⚠️ Claude returned nothing.\n{err[:1500]}"

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out[:TG_LIMIT]

    # Keep session continuity in sync with what the CLI actually used.
    _session_id = data.get("session_id", _session_id)
    result = data.get("result", "")
    if data.get("is_error"):
        return f"⚠️ {result or 'Claude reported an error.'}"
    return result or "(no text result)"


# --------------------------------------------------------------------------- #
# Message handling
# --------------------------------------------------------------------------- #

HELP = (
    "🤖 Claude bridge — talk to me and I'll act on your laptop.\n\n"
    "Just send a message and I'll run it.\n\n"
    "/new — start a fresh conversation (clears memory)\n"
    "/cwd — show my working directory\n"
    "/ping — check I'm alive\n"
    "/help — this message"
)


def handle(chat_id, text: str) -> None:
    global _session_id
    cmd = text.strip().lower()

    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
        return
    if cmd == "/ping":
        send(chat_id, "pong ✅")
        return
    if cmd == "/cwd":
        send(chat_id, f"📂 {WORKING_DIR}")
        return
    if cmd == "/new":
        with _claude_lock:
            _session_id = None
        send(chat_id, "🧹 Fresh conversation started.")
        return

    stop = threading.Event()
    t = threading.Thread(target=typing_loop, args=(chat_id, stop), daemon=True)
    t.start()
    try:
        with _claude_lock:  # one Claude run at a time keeps sessions sane
            reply = run_claude(text)
    finally:
        stop.set()
    send(chat_id, reply)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def main() -> None:
    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        sys.exit("Missing BOT_TOKEN or ALLOWED_CHAT_ID. Copy .env.example to "
                 ".env and fill them in.")

    me = tg("getMe")
    if not me.get("ok"):
        sys.exit("Bad BOT_TOKEN — Telegram rejected it.")
    name = me["result"].get("username", "bot")
    print(f"✅ Connected as @{name}. Working dir: {WORKING_DIR}")
    print(f"   Authorized chat id: {ALLOWED_CHAT_ID}")

    # Drain any backlog so we don't replay old messages from while we were off.
    offset = None
    drain = tg("getUpdates", timeout=0)
    if drain.get("ok") and drain["result"]:
        offset = drain["result"][-1]["update_id"] + 1

    send(ALLOWED_CHAT_ID, "✅ Claude bridge online. Send /help.")

    while True:
        resp = tg("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        if not resp.get("ok"):
            time.sleep(3)
            continue
        for upd in resp["result"]:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "text" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            if str(chat_id) != ALLOWED_CHAT_ID:
                # Unknown sender: refuse and log. Never act on their input.
                print(f"⛔ Ignored message from chat id {chat_id}")
                tg("sendMessage", chat_id=chat_id,
                   text="⛔ Not authorized.")
                continue
            print(f"> {msg['text']!r}")
            try:
                handle(chat_id, msg["text"])
            except Exception as exc:  # noqa: BLE001
                print(f"[handle] error: {exc}", file=sys.stderr)
                send(chat_id, f"⚠️ Internal error: {exc}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye 👋")
