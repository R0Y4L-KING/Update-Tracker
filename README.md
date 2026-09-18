# Update Tracker Bot

Personal Telegram bot that monitors your channel's app posts and **DMs you before any app's VALIDITY expires** — so you never miss an update.

## How it works

Your channel posts look like:

```
APK INFO :- #VerticalTv ...
VALIDITY :- 02/10/2026
```

The bot:

- Imports your **full channel history** on first start (via Telethon)
- Watches **new posts in real time**
- Tracks only the **latest post per app** (a new post = app updated)
- DMs you **7 / 3 / 1 days before expiry + on expiry day**
- Sends a **daily digest of expired apps** until you update them
- When you post an updated version with a new VALIDITY, the old expiry is automatically replaced

## Setup

### 1. Create the bot
- Open [@BotFather](https://t.me/BotFather) → `/newbot` → get the token

### 2. Get your IDs
- Your Telegram user ID: open [@userinfobot](https://t.me/userinfobot)
- API_ID / API_HASH: [my.telegram.org](https://my.telegram.org) → API development tools

### 3. Generate session string (on your PC)
```bash
pip install telethon
python generate_session.py
```
Copy the printed `SESSION_STRING`.

### 4. Deploy on Render
- New Web Service → this repo
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`

Environment variables:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | BotFather token |
| `OWNER_ID` | Your Telegram user ID |
| `CHANNEL_ID` | e.g. `-1001834311011` (comma-sep for multiple) |
| `API_ID` | from my.telegram.org |
| `API_HASH` | from my.telegram.org |
| `SESSION_STRING` | from generate_session.py |
| `NOTIFY_DAYS` | optional, default `7,3,1,0` |

### 5. IMPORTANT: Start the bot in DM
Open your new bot in Telegram and send `/start` once — bots can only DM users who started them.

## Commands

| Command | What it does |
|---|---|
| `/list` | All tracked apps sorted by validity |
| `/check` | Run expiry check right now |
| `/refresh` | Re-import channel history |

## Notes

- On Render free tier the bot sleeps; a keep-alive server + self-ping keeps it awake. Add an UptimeRobot monitor (free, 5 min interval) on your service URL for best results.
- SQLite is ephemeral on Render — after a restart the bot re-imports automatically (takes ~30–60 sec).
