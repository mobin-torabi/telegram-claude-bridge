# Telegram ↔ Claude Code Bridge

Text a Telegram bot from anywhere and have **Claude Code run the request on your
laptop**. When you're away and the laptop is on, send the bot a message and it
acts on the machine (run commands, edit files, check things) and replies to you.

It works by feeding each Telegram message to the local `claude` CLI in headless
mode, then sending Claude's answer back to the chat. Conversation context is
kept between messages, so you can have a back-and-forth.

---

## ⚠️ Read this first — security

This bot runs Claude with `--dangerously-skip-permissions`, meaning Claude can
do **anything on your laptop without asking**. The only protection is the
**chat-id allowlist**: the bot ignores every message that isn't from your
`ALLOWED_CHAT_ID`.

- Keep your `BOT_TOKEN` secret. Anyone with the token *plus* the ability to send
  from your account could control the machine. The token lives only in `.env`,
  which is gitignored.
- The bot replies "Not authorized" to anyone else and never runs their input.
- If a token leaks, revoke it in @BotFather (`/revoke`) and put the new one in
  `.env`.

---

## One-time setup

1. **Create the bot**
   - In Telegram, open **@BotFather** → `/newbot` → pick a name and username.
   - Copy the **token** it gives you.

2. **Get your chat id**
   - Open **@userinfobot** in Telegram and press start. It shows your numeric
     **Id** — that's your `ALLOWED_CHAT_ID`.

3. **Configure**
   ```
   copy .env.example .env
   ```
   Open `.env` and fill in `BOT_TOKEN` and `ALLOWED_CHAT_ID`. Optionally set
   `WORKING_DIR` to the folder Claude should work in by default.

4. **Dependencies** are installed automatically by `start.bat`, or manually:
   ```
   py -m pip install -r requirements.txt
   ```

---

## Run it (manual start)

Double-click **`start.bat`** (or run `py bridge.py`). You'll see
`✅ Connected as @yourbot` and get an "online" message in Telegram.

Leave the window open while you want the bot live. Close it to stop.

---

## Using it

Just message the bot. Examples:

> *what's my laptop's free disk space?*
> *open the latest file in my Downloads and summarize it*
> *commit and push my notes repo*

Commands:

| Command | What it does |
|---------|--------------|
| `/help` | Show help |
| `/ping` | Check the bot is alive |
| `/cwd`  | Show Claude's working directory |
| `/new`  | Start a fresh conversation (clears context) |

While Claude works you'll see the "typing…" indicator. Long tasks just take a
bit; the reply arrives when it's done.

---

## Configuration (`.env`)

| Key | Meaning |
|-----|---------|
| `BOT_TOKEN` | Bot token from @BotFather (required) |
| `ALLOWED_CHAT_ID` | Your Telegram numeric id — the only allowed sender (required) |
| `WORKING_DIR` | Default folder Claude runs in (default: your home folder) |
| `CLAUDE_MODEL` | Optional model id, e.g. `claude-opus-4-8` |
| `CLAUDE_TIMEOUT` | Max seconds for one task before it's cancelled (default 1800) |
| `PROXY` | Optional proxy for reaching Telegram where it's blocked, e.g. `http://127.0.0.1:10808` |

> **Where Telegram is blocked:** set `PROXY` to your local VPN/proxy client's
> port (v2rayN defaults to `10808`). The bot routes only its Telegram traffic
> through it. Keep that client running while the bot is up.

---

## Troubleshooting

- **"Bad BOT_TOKEN"** — token is wrong or revoked; copy a fresh one.
- **Bot doesn't reply** — make sure the `start.bat` window is still open and you
  messaged the right bot from the allowed account.
- **"Not authorized"** — your `ALLOWED_CHAT_ID` doesn't match; recheck via
  @userinfobot.
- **`claude` not found** — make sure Claude Code is installed and on PATH
  (`claude --version` should work in a normal terminal).
- **Replies cut off** — long output is split across several Telegram messages.
