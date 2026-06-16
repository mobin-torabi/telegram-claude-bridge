---
name: telegram-claude-bridge
description: Start, stop, or troubleshoot the Telegram↔Claude bridge that lets the owner control this laptop by messaging a Telegram bot. Use when the user mentions the Telegram bridge/bot, wants to run it, or it isn't responding.
---

# Telegram ↔ Claude Code bridge

A Python bot (`bridge.py`) that long-polls Telegram and, for each message from
the authorized user, runs it through the local `claude` CLI in headless mode,
then sends the result back. Conversation context is kept via
`--session-id`/`--resume`.

## Ask-before-acting flow (important)

Two-phase, so Claude answers questions but never changes the machine without a
yes:

1. **Plan pass** — every fresh message runs with `--permission-mode plan`
   (read-only). Pure questions are answered. If the request needs changes,
   plan mode blocks the action; Claude's intent appears as a denied
   `ExitPlanMode` entry in the JSON `permission_denials`, whose
   `tool_input.plan` holds the proposed steps. The bot sends that plan and sets
   `_pending = True`.
2. **Execute pass** — when `_pending` and the reply is affirmative
   (`is_affirmative()`, English + Persian, with a negation guard), the bot
   resumes the same session with `--dangerously-skip-permissions` and the work
   is carried out. Any non-affirmative reply just starts a new plan pass — so
   execution only ever happens after an explicit yes.

`PLAN_SYS` / `EXEC_SYS` are the per-phase appended system prompts.

## Permission modes (set from the bot)

`_mode` ∈ {`lock`, `ask`, `auto`}, persisted in `.bridge_state.json`
(gitignored), changed via `/lock` `/ask` `/auto`, shown via `/mode`:

- **lock** — only the plan pass runs; any proposed action is refused (no
  approval offered).
- **ask** — default; plan → approval → execute (the flow above).
- **auto** — every message goes straight to the execute pass (full
  permissions, no asking).

The plan pass is launched with `--add-dir` for every drive root
(`EXTRA_DIRS` / `system_dirs()`), so read-only Q&A can reach anywhere on the
machine. Execute pass uses `--dangerously-skip-permissions` (whole machine).

## Sending files to the user

`FILE_SYS` (appended to the prompt in every mode) tells Claude to deliver a file
by printing `[[SENDFILE: <absolute path>]]`. `deliver()` strips those markers
from the reply and calls `send_file()`, which uploads via Telegram
`sendDocument` (≤ ~50 MB) or, for bigger files, `upload_large()` → 0x0.st and
sends the link. Fetching an existing file is read-only, so it works in `lock`
mode too (no approval). Uploads go through `PROXIES` like everything else.

Project root: `D:\Mobin\Automation Programs\Telegram Claude Bridge`

## Run / stop

- Start: double-click `start.bat`, or `py bridge.py` from the project folder.
- Stop: close the window / Ctrl-C.
- It's **manual start** by design — not registered to auto-run on boot.

## First-run setup (portable)

If `.env` is missing (or `py bridge.py --setup`), `setup()` runs an interactive
wizard: asks for the user's own BotFather token, calls `find_connection()` to
pick a direct route or auto-detect a local VPN/proxy port (falling back to
asking), uses `detect_chat_id()` to capture the chat id from the first message
the user sends the bot, then `write_env()` saves `.env` and `apply_env()` loads
it. Each user runs their own copy (own bot + own Claude account) — bots can't be
shared (one bot = one polling machine).

## Config

All config is in `.env` (gitignored; copy from `.env.example`):
`BOT_TOKEN`, `ALLOWED_CHAT_ID` (required), `WORKING_DIR`, `CLAUDE_MODEL`,
`CLAUDE_TIMEOUT`.

## Security model

Single-user. The bot only acts on messages whose `chat.id` equals
`ALLOWED_CHAT_ID`; everyone else gets "Not authorized" and is ignored. Plus the
approval gate above means nothing is modified without an explicit yes. Never
commit `.env`; if the token leaks, `/revoke` in @BotFather.

## Bot commands

`/help`, `/ping`, `/cwd`, `/new` (reset conversation).

## Common issues

- **"Bad BOT_TOKEN"** → token wrong/revoked in `.env`.
- **No reply** → `start.bat` window closed, or message came from a non-allowed
  account.
- **`claude` not found** → Claude Code not on PATH; `claude --version` must work.
- **Stuck/slow** → a task may be running (one at a time, lock-guarded); or it hit
  `CLAUDE_TIMEOUT`. Use `/new` to reset the session.

## How it talks to Claude

`run_claude()` builds the CLI command, tracks `_session_id` for continuity, and
parses the JSON `result`/`is_error`/`session_id` fields. A heartbeat thread keeps
the Telegram "typing…" action alive while Claude works.
