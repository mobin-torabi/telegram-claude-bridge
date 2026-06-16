---
name: telegram-claude-bridge
description: Start, stop, or troubleshoot the Telegram↔Claude bridge that lets the owner control this laptop by messaging a Telegram bot. Use when the user mentions the Telegram bridge/bot, wants to run it, or it isn't responding.
---

# Telegram ↔ Claude Code bridge

A Python bot (`bridge.py`) that long-polls Telegram, and for each message from
the authorized user runs it through the local `claude` CLI in headless mode
(`-p --output-format json --dangerously-skip-permissions`), then sends the
result back. Conversation context is kept via `--session-id`/`--resume`.

Project root: `D:\Mobin\Automation Programs\Telegram Claude Bridge`

## Run / stop

- Start: double-click `start.bat`, or `py bridge.py` from the project folder.
- Stop: close the window / Ctrl-C.
- It's **manual start** by design — not registered to auto-run on boot.

## Config

All config is in `.env` (gitignored; copy from `.env.example`):
`BOT_TOKEN`, `ALLOWED_CHAT_ID` (required), `WORKING_DIR`, `CLAUDE_MODEL`,
`CLAUDE_TIMEOUT`.

## Security model

Single-user. The bot only acts on messages whose `chat.id` equals
`ALLOWED_CHAT_ID`; everyone else gets "Not authorized" and is ignored. Because
it runs with skipped permissions, the chat-id allowlist is the whole security
boundary. Never commit `.env`; if the token leaks, `/revoke` in @BotFather.

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
