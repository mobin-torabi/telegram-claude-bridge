"""
Telegram <-> Claude Code bridge.

Listens for Telegram messages from a single authorized user and runs each
message as a prompt through the local Claude Code CLI (headless mode) on this
laptop. Claude's reply is sent back to Telegram. Because it runs with
--dangerously-skip-permissions, Claude can actually do things on the machine
(run commands, edit files, etc.) -- so the ONLY thing standing between a
stranger and your laptop is the chat-id allowlist. Keep your bot token secret.

On first run (or with --setup) it walks you through creating your own Telegram
bot, auto-detects your chat id and any needed proxy, and writes a .env. After
that it just runs. Each person runs their own copy with their own bot + Claude
account.
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


ENV_PATH = BASE_DIR / ".env"
TG_LIMIT = 4000  # Telegram hard limit is 4096; leave headroom.

# Config globals — populated by apply_env() from os.environ / .env.
BOT_TOKEN = ALLOWED_CHAT_ID = WORKING_DIR = CLAUDE_MODEL = PROXY = ""
CLAUDE_TIMEOUT = POLL_TIMEOUT = 0
PROXIES = None
API = ""


def apply_env() -> None:
    """(Re)load config globals from os.environ. Safe to call after setup."""
    global BOT_TOKEN, ALLOWED_CHAT_ID, WORKING_DIR, CLAUDE_MODEL
    global CLAUDE_TIMEOUT, PROXY, PROXIES, POLL_TIMEOUT, API
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
    ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()
    WORKING_DIR = os.environ.get("WORKING_DIR", str(Path.home())).strip()
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()  # e.g. claude-opus-4-8
    # Hard ceiling so a runaway task can't block the bot forever (seconds).
    CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800") or "1800")
    # Optional proxy for api.telegram.org (e.g. where Telegram is blocked).
    PROXY = os.environ.get("PROXY", "").strip()
    PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
    # Long-poll seconds. Keep short behind a proxy that drops idle tunnels.
    POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "20" if not PROXY else "10")
                       or "10")
    API = f"https://api.telegram.org/bot{BOT_TOKEN}"


load_env(ENV_PATH)
apply_env()

# Sent on the read-only (plan) pass: answer questions, but only *propose* actions.
PLAN_SYS = (
    "You are being operated remotely by your owner through a Telegram bot on "
    "their Windows laptop. They may be on a phone, so keep replies short and "
    "readable on a small screen. If they only want information or ask a "
    "question, answer it directly (reading/inspecting the machine to answer is "
    "fine). If fulfilling the request requires CHANGING anything on the machine "
    "(running commands, creating/editing/deleting files, installing, sending "
    "things, etc.), do NOT do it yet — instead present a short plan and request "
    "approval. Keep the plan to a few plain lines a phone user can skim."
)

# Sent on the execute pass, after the owner approves.
EXEC_SYS = (
    "You are being operated remotely by your owner through a Telegram bot on "
    "their Windows laptop, with full permissions. They just APPROVED the plan "
    "you proposed. Carry out every step needed to complete it now, then report "
    "briefly what you did. Keep the reply short and phone-friendly."
)

CLAUDE_BIN = shutil.which("claude") or "claude"

# Per-conversation Claude session id, so the bot keeps context between messages.
_session_id = None
# True while a proposed plan is waiting for the owner's yes/no.
_pending = False
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

def run_claude(prompt: str, mode: str) -> dict:
    """Run one prompt through the Claude CLI.

    mode="plan"    -> read-only: answers questions, can't change anything.
    mode="execute" -> full permissions: actually carries out the work.

    Returns {"text": str, "plan": str|None}. When "plan" is set, Claude wants
    approval before acting (it tried to leave plan mode) and "plan" holds the
    proposed steps.
    """
    global _session_id

    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    if mode == "plan":
        cmd += ["--permission-mode", "plan", "--append-system-prompt", PLAN_SYS]
    else:
        cmd += ["--dangerously-skip-permissions",
                "--append-system-prompt", EXEC_SYS]

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
        return {"text": f"⏱️ Timed out after {CLAUDE_TIMEOUT}s. Try /new and a "
                        "smaller step.", "plan": None}
    except Exception as exc:  # noqa: BLE001
        return {"text": f"⚠️ Could not run Claude: {exc}", "plan": None}

    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()
        return {"text": f"⚠️ Claude returned nothing.\n{err[:1500]}", "plan": None}

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"text": out[:TG_LIMIT], "plan": None}

    # Keep session continuity in sync with what the CLI actually used.
    _session_id = data.get("session_id", _session_id)
    result = data.get("result", "")
    if data.get("is_error"):
        return {"text": f"⚠️ {result or 'Claude reported an error.'}", "plan": None}

    # In plan mode, a request to act shows up as a denied ExitPlanMode call
    # whose input carries the proposed plan.
    if mode == "plan":
        for d in data.get("permission_denials", []):
            if d.get("tool_name") == "ExitPlanMode":
                plan = (d.get("tool_input") or {}).get("plan", "").strip()
                return {"text": result, "plan": plan or result}

    return {"text": result or "(no text result)", "plan": None}


# --------------------------------------------------------------------------- #
# Message handling
# --------------------------------------------------------------------------- #

HELP = (
    "🤖 Claude bridge\n\n"
    "Ask me anything and I'll answer. If a request means changing something on "
    "your laptop, I'll show a short plan and ask first — reply *yes* and I'll "
    "do it.\n\n"
    "/new — start a fresh conversation (clears memory)\n"
    "/cwd — show my working directory\n"
    "/ping — check I'm alive\n"
    "/help — this message"
)

# Words/phrases that count as approving a pending plan.
_YES_WORDS = {
    "yes", "y", "yeah", "yep", "yup", "ya", "ok", "okay", "k", "sure", "fine",
    "confirm", "confirmed", "approve", "approved", "proceed", "go", "run", "do",
    "بله", "بعله", "اره", "آره", "باشه", "اوکی", "اوکیه", "تایید", "تأیید",
    "برو", "حتما", "بزن", "انجامش", "انجام",
}
_YES_PHRASES = (
    "do it", "go ahead", "go for it", "do that", "yes please", "please do",
    "make it", "go on", "sounds good", "lets do it", "let's do it",
    "انجام بده", "انجامش بده", "انجام بدش", "برو جلو", "برو بریم",
)
_NO_WORDS = ("no", "dont", "don't", "stop", "cancel", "nope", "نه", "نکن", "لغو",
             "بیخیال", "نخیر")


def is_affirmative(text: str) -> bool:
    """True only when the reply clearly approves the pending plan."""
    t = text.strip().lower().rstrip(".!۔، ")
    if any(n == t or t.startswith(n + " ") for n in _NO_WORDS):
        return False
    if t in _YES_WORDS:
        return True
    return any(p in t for p in _YES_PHRASES)


def ask_claude(chat_id, text: str, mode: str) -> dict:
    """Run Claude with a live 'typing…' indicator and return its result dict."""
    stop = threading.Event()
    t = threading.Thread(target=typing_loop, args=(chat_id, stop), daemon=True)
    t.start()
    try:
        with _claude_lock:  # one Claude run at a time keeps sessions sane
            return run_claude(text, mode)
    finally:
        stop.set()


def handle(chat_id, text: str) -> None:
    global _session_id, _pending
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
            _pending = False
        send(chat_id, "🧹 Fresh conversation started.")
        return

    # A plan is awaiting approval and this reply is a clear yes -> execute it.
    if _pending and is_affirmative(text):
        _pending = False
        res = ask_claude(chat_id, text, mode="execute")
        send(chat_id, res["text"])
        return

    # Otherwise treat it as a fresh request: read-only plan pass. Nothing on the
    # laptop changes here — at most Claude proposes a plan and asks.
    _pending = False
    res = ask_claude(chat_id, text, mode="plan")
    if res["plan"]:
        _pending = True
        msg = "📋 I'd like to do this:\n\n" + res["plan"]
        msg += "\n\n— Reply *yes* to go ahead, or tell me what to change."
        send(chat_id, msg)
    else:
        send(chat_id, res["text"])


# --------------------------------------------------------------------------- #
# First-run setup wizard
# --------------------------------------------------------------------------- #

# Local proxy ports commonly exposed by VPN clients (v2rayN, Clash, Tor, etc.).
PROXY_PORTS = [10808, 10809, 2080, 1080, 7890, 7891, 8889, 8080, 10800, 9150]


def _check_token(token: str, proxy: str, timeout: int = 8):
    """Return the bot info dict if getMe succeeds via this proxy, else None."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe",
                         timeout=timeout, proxies=proxies)
        data = r.json()
        return data["result"] if data.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def find_connection(token: str):
    """Find a way to reach Telegram. Returns (proxy_url, bot_info) or (None, None)."""
    print("→ Testing a direct connection to Telegram ...")
    info = _check_token(token, "", timeout=10)
    if info:
        print("  ✓ Reachable directly — no proxy needed.")
        return "", info

    print("  ✗ Can't reach Telegram directly (it may be blocked on this network).")
    print("→ Scanning for a local proxy from your VPN client ...")
    for port in PROXY_PORTS:
        for scheme in ("socks5h", "http"):
            url = f"{scheme}://127.0.0.1:{port}"
            info = _check_token(token, url, timeout=6)
            if info:
                print(f"  ✓ Found a working proxy: {url}")
                return url, info
    print("  ✗ No working local proxy found.")

    while True:
        url = input("  Enter a proxy URL (e.g. socks5h://127.0.0.1:10808), "
                    "or blank to retry direct: ").strip()
        info = _check_token(token, url, timeout=10)
        if info:
            return url, info
        print("  ✗ Still can't reach Telegram. Make sure your VPN/proxy is "
              "running, then try again. (Ctrl+C to abort.)")


def detect_chat_id(token: str, proxy: str) -> str:
    """Wait for the user to message the bot and capture their chat id."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    base = f"https://api.telegram.org/bot{token}"
    # Drain backlog so we only react to a brand-new message.
    offset = None
    try:
        r = requests.get(f"{base}/getUpdates", params={"timeout": 0},
                         timeout=15, proxies=proxies).json()
        if r.get("ok") and r["result"]:
            offset = r["result"][-1]["update_id"] + 1
    except Exception:  # noqa: BLE001
        pass

    print("\n→ Open Telegram, find your bot, and send it any message (e.g. 'hi').")
    print("  Waiting for your message ...")
    while True:
        try:
            r = requests.get(f"{base}/getUpdates",
                             params={"timeout": 25, "offset": offset},
                             timeout=40, proxies=proxies).json()
        except Exception:  # noqa: BLE001
            continue
        if not r.get("ok"):
            time.sleep(2)
            continue
        for upd in r["result"]:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if msg and msg.get("chat"):
                cid = msg["chat"]["id"]
                who = msg.get("from", {}).get("first_name", "you")
                print(f"  ✓ Got it — message from {who} (id {cid}).")
                return str(cid)


def write_env(token: str, chat_id: str, working_dir: str, proxy: str) -> None:
    lines = [
        "# Written by first-run setup. Safe to edit by hand.",
        f"BOT_TOKEN={token}",
        f"ALLOWED_CHAT_ID={chat_id}",
        f"WORKING_DIR={working_dir}",
        f"PROXY={proxy}",
        "CLAUDE_MODEL=",
        "CLAUDE_TIMEOUT=1800",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup() -> None:
    """Interactive first-run setup; writes .env and loads it."""
    print("=" * 64)
    print(" Telegram ↔ Claude bridge — first-run setup")
    print("=" * 64)
    print(
        "\nThis bot runs on THIS computer with YOUR Claude account, and you\n"
        "control it from your own Telegram bot. You need your own bot (free):\n"
        "  1. In Telegram, open @BotFather\n"
        "  2. Send /newbot and pick a name + username\n"
        "  3. Copy the token it gives you (looks like 123456:ABC-def...)\n"
    )

    info = None
    proxy = token = ""
    while not info:
        token = input("Paste your bot token: ").strip()
        if not token:
            continue
        proxy, info = find_connection(token)
        if not info:
            print("  ✗ That token was rejected by Telegram. Try again.\n")
    print(f"\n✓ Connected to @{info.get('username', 'your bot')}.")

    raw = input("\nPress Enter to auto-detect your Telegram ID by messaging the "
                "bot,\nor paste your numeric ID if you know it: ").strip()
    chat_id = raw if raw.isdigit() else detect_chat_id(token, proxy)

    home = str(Path.home())
    wd = input(f"\nFolder Claude should work in [{home}]: ").strip() or home

    write_env(token, chat_id, wd, proxy)
    for k, v in {"BOT_TOKEN": token, "ALLOWED_CHAT_ID": chat_id,
                 "WORKING_DIR": wd, "PROXY": proxy}.items():
        os.environ[k] = v
    apply_env()
    print("\n✓ Setup complete — saved to .env. Starting the bot ...\n")


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def main() -> None:
    if "--setup" in sys.argv or not (BOT_TOKEN and ALLOWED_CHAT_ID):
        try:
            setup()
        except (KeyboardInterrupt, EOFError):
            sys.exit("\nSetup cancelled.")

    me = tg("getMe")
    if not me.get("ok"):
        sys.exit("Bad BOT_TOKEN — Telegram rejected it. Run again with --setup "
                 "to reconfigure.")
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
