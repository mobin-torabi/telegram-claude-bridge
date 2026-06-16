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
import re
import shutil
import string
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

# Appended in every mode: how Claude hands a file back to the user's phone.
FILE_SYS = (
    "FILE DELIVERY — IMPORTANT. When the user asks to send / get / download / "
    "share / 'give me' a FILE (e.g. 'send me X', 'get me the file Y'), you MUST "
    "deliver the actual file as an attachment, NOT its text. Do this by "
    "outputting a line EXACTLY like:\n"
    "[[SENDFILE: <absolute path to the file>]]\n"
    "one line per file. The bridge then delivers that file to the user (it "
    "stages it to their cloud drive — you don't handle that part). Rules:\n"
    "- Do NOT print, paste, quote, dump, or summarize the file's contents when "
    "asked to SEND it. Reply with at most one short sentence plus the marker.\n"
    "- Works for any file type (html, pdf, images, code, zip, ...). It is "
    "read-only, so do it directly without asking for approval.\n"
    "- Only show a file's contents instead when the user explicitly asks to "
    "'read', 'open', 'show', or 'see inside' it.\n"
    "- For several files or a whole folder, if you may act, zip them first and "
    "send the zip's path.\n"
    "Example — user: 'send me notes.txt on my desktop' → you reply:\n"
    "Here you go.\n"
    "[[SENDFILE: C:\\Users\\You\\Desktop\\notes.txt]]"
)


CLAUDE_BIN = shutil.which("claude") or "claude"

# Per-conversation Claude session id, so the bot keeps context between messages.
_session_id = None
# True while a proposed plan is waiting for the owner's yes/no.
_pending = False
_claude_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# Permission modes (controllable from the bot)
# --------------------------------------------------------------------------- #
#   lock  -> answers questions only; never changes anything, even on "yes".
#   ask   -> proposes a plan and waits for "yes" before acting (default).
#   auto  -> acts immediately with full permissions, no asking.
VALID_MODES = ("lock", "ask", "auto")
MODE_DESC = {
    "lock": "🔒 lock — I only answer questions; I won't change anything.",
    "ask":  "🟡 ask — I propose changes and wait for your 'yes' (default).",
    "auto": "🟢 auto — I act immediately, no asking. Full access to the laptop.",
}
STATE_PATH = BASE_DIR / ".bridge_state.json"
_mode = "ask"


def load_state() -> None:
    global _mode
    try:
        m = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("mode")
        if m in VALID_MODES:
            _mode = m
    except Exception:  # noqa: BLE001
        pass


def save_state() -> None:
    try:
        STATE_PATH.write_text(json.dumps({"mode": _mode}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def system_dirs() -> list:
    """Top-level roots to grant read access to, so Q&A can reach anywhere."""
    if os.name == "nt":
        return [f"{d}:\\" for d in string.ascii_uppercase
                if os.path.exists(f"{d}:\\")]
    return ["/"]


# Whole-machine read access for the (read-only) plan pass.
EXTRA_DIRS = system_dirs()


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


SENDFILE_RE = re.compile(r"\[\[SENDFILE:\s*(.+?)\]\]")


def onedrive_drop() -> Path:
    """The OneDrive subfolder where requested files are staged for the phone."""
    base = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    root = Path(base) if base else (Path.home() / "OneDrive")
    return root / os.environ.get("ONEDRIVE_DROP", "PhoneDrops")


def stage_to_onedrive(chat_id, raw_path: str) -> None:
    """Copy one file into the OneDrive drop folder so it syncs to the phone."""
    src = Path(raw_path.strip().strip('"'))
    if not src.is_file():
        send(chat_id, f"⚠️ File not found: {src}")
        return
    drop = onedrive_drop()
    try:
        drop.mkdir(parents=True, exist_ok=True)
        dest = drop / src.name
        shutil.copy(src, dest)        # copy() gives dest a current timestamp...
        os.utime(dest, None)          # ...and this nudges OneDrive to sync it now
    except Exception as exc:  # noqa: BLE001
        send(chat_id, f"⚠️ Couldn't copy {src.name} to OneDrive: {exc}")
        return
    send(chat_id,
         f"✅ {src.name} is now in your OneDrive › {drop.name}.\n"
         "Open the OneDrive app (or onedrive.com) on your phone to grab it — "
         "give it a few seconds to sync.")


def deliver(chat_id, text: str) -> None:
    """Send Claude's reply, and stage any files it flagged with [[SENDFILE:]]."""
    paths = SENDFILE_RE.findall(text or "")
    clean = SENDFILE_RE.sub("", text or "").strip()
    if clean or not paths:
        send(chat_id, clean)
    for p in paths:
        stage_to_onedrive(chat_id, p)


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

    # NOTE: pass ONE --append-system-prompt only. This CLI keeps just the last
    # occurrence, so combine the prompts. Lead with the file rule for emphasis.
    sys_prompt = FILE_SYS + "\n\n" + (PLAN_SYS if mode == "plan" else EXEC_SYS)
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--append-system-prompt", sys_prompt]
    if mode == "plan":
        cmd += ["--permission-mode", "plan"]
        for d in EXTRA_DIRS:  # let read-only Q&A reach any drive/folder
            cmd += ["--add-dir", d]
    else:
        cmd += ["--dangerously-skip-permissions"]

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


# Map a word in the request to a standard user folder.
_LOC_MAP = [("desktop", "Desktop"), ("download", "Downloads"),
            ("document", "Documents"), ("picture", "Pictures"),
            ("photo", "Pictures"), ("video", "Videos"), ("music", "Music")]
# Words that aren't useful as filename keywords.
_STOP = {
    "send", "me", "the", "a", "an", "file", "files", "in", "on", "my", "get",
    "please", "to", "from", "of", "that", "this", "want", "need", "give", "it",
    "download", "share", "fetch", "grab", "for", "is", "there", "which", "named",
    "name", "called", "folder", "could", "you", "can", "and", "with", "at",
    "into", "onedrive", "cloud", "link", "desktop", "downloads", "document",
    "documents", "pictures", "picture", "photo", "photos", "videos", "video",
    "music",
}
_PATH_RE = re.compile(r'[A-Za-z]:[\\/][^\s"\'<>|]+')
_EXT_RE = re.compile(r'([\w\-][\w\-.]*\.[A-Za-z0-9]{1,6})\b')


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def find_files_locally(text: str):
    """Resolve which file(s) the user wants, deterministically (no model).

    Looks for: an explicit path, then a filename with extension, then keyword
    matches in the location they named (or Desktop/Downloads/Documents). Returns
    a list of existing absolute paths (possibly empty).
    """
    home = Path.home()
    t = text.lower()

    # 1. An explicit Windows path in the message.
    for m in _PATH_RE.finditer(text):
        p = Path(m.group(0).strip().strip('"').strip("'"))
        if p.is_file():
            return [str(p)]

    # Which folders to look in (named location, else the usual suspects).
    named = [home / folder for key, folder in _LOC_MAP
             if key in t and (home / folder).is_dir()]
    common = [d for d in (home / "Desktop", home / "Downloads",
                          home / "Documents", Path(WORKING_DIR))
              if d.is_dir()]
    search_dirs = _dedup(named or common)

    # 2. A filename with an extension (exact match wins, else substring).
    subs = []
    for m in _EXT_RE.finditer(text):
        target = m.group(1).strip().lower()
        for d in search_dirs + [home]:
            try:
                for f in d.iterdir():
                    if not f.is_file():
                        continue
                    if f.name.lower() == target:
                        return [str(f)]
                    if target in f.name.lower():
                        subs.append(str(f))
            except OSError:
                pass
    if subs:
        return _dedup(subs)[:5]

    # 3. Keyword match on file names in the search folders.
    words = [w for w in re.findall(r"[A-Za-z؀-ۿ]{3,}", t)
             if w not in _STOP]
    if not words:
        return []
    hits = []
    for d in search_dirs:
        try:
            for f in d.iterdir():
                if f.is_file() and any(w in f.name.lower() for w in words):
                    hits.append(str(f))
        except OSError:
            pass
    return _dedup(hits)[:5]


# --------------------------------------------------------------------------- #
# Message handling
# --------------------------------------------------------------------------- #

HELP = (
    "🤖 Claude bridge\n\n"
    "Ask me anything and I'll answer. I can also put files from the laptop into "
    "your OneDrive so you can grab them on your phone — just ask, e.g. 'send me "
    "my resume from Downloads'.\n\n"
    "If a request means changing something on your laptop, what happens depends "
    "on the permission mode:\n\n"
    "🔒 /lock — answers only, never changes anything\n"
    "🟡 /ask — propose a plan, wait for your *yes* (default)\n"
    "🟢 /auto — act immediately, no asking (full access)\n"
    "/mode — show the current mode\n\n"
    "/new — start a fresh conversation (clears memory)\n"
    "/cwd — show my working directory\n"
    "/ping — check I'm alive\n"
    "/help — this message"
)


def mode_status(changed: bool = False) -> str:
    head = "Permission mode changed to:" if changed else "Permission mode:"
    return (f"{head}\n{MODE_DESC[_mode]}\n\nSwitch anytime: /lock · /ask · /auto")

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


# A "deliver me a file" request (vs. "show me what's in it").
_SEND_VERBS = ("send", "get me", "download", "share", "fetch", "grab", "upload",
               "give me", "deliver", "بفرست", "بده", "برام بفرست", "دانلود")
_FILE_HINTS = ("file", "pdf", "zip", "doc", "xls", "ppt", "image", "photo",
               "picture", "screenshot", "html", "csv", "txt", "png", "jpg",
               "jpeg", "mp3", "mp4", "attachment", "فایل", "عکس", "فیلم")
_VIEW_WORDS = ("what's in", "whats in", "what is in", "contents", "content of",
               "read ", "open ", "show ", "inside", "summary", "summarize",
               "what does", "tell me what", "محتوا", "بازکن", "نشون")
_PATHISH = re.compile(r"[a-zA-Z]:[\\/]|[\\/][\w.\- ]+[\\/]|\.\w{2,5}\b")


def wants_file(text: str) -> bool:
    """High-confidence request to be sent a file (not shown its contents)."""
    t = text.lower()
    if any(v in t for v in _VIEW_WORDS):
        return False
    has_verb = any(v in t for v in _SEND_VERBS)
    has_hint = any(h in t for h in _FILE_HINTS) or bool(_PATHISH.search(text))
    return has_verb and has_hint


def deliver_requested_files(chat_id, text: str) -> None:
    """Find the file(s) the user wants and stage them to OneDrive."""
    paths = find_files_locally(text)
    if not paths:
        send(chat_id, "🔍 I couldn't find that file. Tell me its name or full "
                      "path (e.g. C:\\Users\\You\\Desktop\\name.ext) and which "
                      "folder it's in.")
        return
    if len(paths) > 1:
        names = "\n".join(f"• {Path(p).name}" for p in paths)
        send(chat_id, f"Found {len(paths)} matches, sending all:\n{names}")
    for p in paths:
        stage_to_onedrive(chat_id, p)


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
    global _session_id, _pending, _mode
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
    if cmd == "/mode":
        send(chat_id, mode_status())
        return
    if cmd in ("/lock", "/ask", "/auto"):
        _mode = cmd[1:]
        _pending = False
        save_state()
        send(chat_id, mode_status(changed=True))
        return
    if cmd == "/new":
        with _claude_lock:
            _session_id = None
            _pending = False
        send(chat_id, "🧹 Fresh conversation started.")
        return

    # "Send me <file>" is read-only, so handle it the reliable way in any mode:
    # resolve the path with a dedicated pass and upload the actual file.
    if wants_file(text):
        deliver_requested_files(chat_id, text)
        return

    # auto: full permissions, act immediately, no questions asked.
    if _mode == "auto":
        _pending = False
        res = ask_claude(chat_id, text, mode="execute")
        deliver(chat_id, res["text"])
        return

    # ask: a plan is awaiting approval and this reply is a clear yes -> do it.
    if _mode == "ask" and _pending and is_affirmative(text):
        _pending = False
        res = ask_claude(chat_id, text, mode="execute")
        deliver(chat_id, res["text"])
        return

    # lock / ask: read-only plan pass. Nothing on the laptop changes here
    # (fetching an existing file is read-only, so it still works in every mode).
    _pending = False
    res = ask_claude(chat_id, text, mode="plan")
    if not res["plan"]:
        deliver(chat_id, res["text"])
        return

    if _mode == "lock":
        send(chat_id, "🔒 Actions are locked, so I won't change anything.\n"
                      "Send /ask to approve per request, or /auto for full "
                      "access.\n\nFor reference, here's what I'd do:\n\n"
                      + res["plan"])
    else:  # ask
        _pending = True
        send(chat_id, "📋 I'd like to do this:\n\n" + res["plan"]
             + "\n\n— Reply *yes* to go ahead, or tell me what to change.")


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
    load_state()
    print(f"✅ Connected as @{name}. Working dir: {WORKING_DIR}")
    print(f"   Authorized chat id: {ALLOWED_CHAT_ID} | mode: {_mode}")

    # Drain any backlog so we don't replay old messages from while we were off.
    offset = None
    drain = tg("getUpdates", timeout=0)
    if drain.get("ok") and drain["result"]:
        offset = drain["result"][-1]["update_id"] + 1

    send(ALLOWED_CHAT_ID,
         f"✅ Claude bridge online.\n{MODE_DESC[_mode]}\nSend /help.")

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
