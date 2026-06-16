# Telegram ↔ Claude Code Bridge

Text a Telegram bot from anywhere and have **Claude Code run the request on your
laptop**. When you're away and the laptop is on, send the bot a message and it
acts on the machine (run commands, edit files, check things) and replies to you.

It works by feeding each Telegram message to the local `claude` CLI in headless
mode, then sending Claude's answer back to the chat. Conversation context is
kept between messages, so you can have a back-and-forth.

**Ask-before-acting:** every message first runs in read-only **plan mode** —
Claude can answer questions and inspect things, but cannot change anything. If a
request requires modifying the machine, Claude replies with a short plan and
waits. Only when you reply **yes** does it re-run with full permissions and
carry the plan out.

---

## ⚠️ Read this first — security

Two layers protect the machine:

1. **Approval gate.** By default Claude runs read-only (`--permission-mode
   plan`) and can't modify anything. It only executes (with
   `--dangerously-skip-permissions`) after you approve a proposed plan.
2. **Chat-id allowlist.** The bot ignores every message that isn't from your
   `ALLOWED_CHAT_ID`.

- Keep your `BOT_TOKEN` secret. Anyone with the token *plus* the ability to send
  from your account could control the machine. The token lives only in `.env`,
  which is gitignored.
- The bot replies "Not authorized" to anyone else and never runs their input.
- If a token leaks, revoke it in @BotFather (`/revoke`) and put the new one in
  `.env`.

---

## Setup — just run it

There's no config file to hand-edit. The **first time** you start it, a setup
wizard walks you through everything:

1. **Get the code & start it**
   ```
   git clone https://github.com/mobin-torabi/telegram-claude-bridge
   cd telegram-claude-bridge
   ```
   Then double-click **`start.bat`** (it installs dependencies on first run).

2. **Create your own bot** (the wizard reminds you how)
   - In Telegram, open **@BotFather** → `/newbot` → pick a name + username.
   - Copy the **token** it gives you and paste it into the wizard.

3. **It auto-detects the rest**
   - **Connection:** tries Telegram directly; if blocked, finds your VPN/proxy
     automatically (or asks for a proxy URL).
   - **Your Telegram id:** press Enter, then send your bot any message — it
     captures your id for you. (Or paste the id if you already know it.)
   - **Working folder:** press Enter for your home folder, or type another.

That writes a local `.env` and starts the bot. You won't be asked again.

> Every user runs their **own** copy: their own bot token, their own laptop,
> their own Claude account. A bot can't be shared — one bot controls one
> machine.

To reconfigure later (new bot, new folder, etc.), run `py bridge.py --setup`.

---

## Run it (manual start)

Double-click **`start.bat`** (or run `py bridge.py`). You'll see
`✅ Connected as @yourbot` and get an "online" message in Telegram.

Leave the window open while you want the bot live. Close it to stop.

---

## Using it

Just message the bot.

**Questions are answered straight away:**

> *what's my laptop's free disk space?*
> *summarize the newest file in my Downloads*

**Anything that changes the machine is confirmed first:**

> **You:** *create a backup of my notes folder on the desktop*
> **Bot:** 📋 I'd like to do this: … — Reply *yes* to go ahead.
> **You:** *yes*
> **Bot:** ✅ Done — copied 142 files to Desktop\notes-backup.

Reply with anything other than a clear yes (or a tweak like "yes but zip it")
and it re-plans instead of acting. Affirmatives are recognized in English and
Persian.

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
