"""
Update Tracker Bot
==================
Personal Telegram bot that monitors your channel's app posts and
DMs YOU before any app's VALIDITY expires, so you never miss an update.

Post format it understands:
    APK INFO :- #AppName ...
    VALIDITY :- DD/MM/YYYY

Features:
- Auto-imports full channel history on first start (Telethon)
- Watches new channel posts in real time
- Same app multiple posts? Only the LATEST post is tracked
- DMs you 7 / 3 / 1 days before expiry and on expiry day
- Daily digest of apps already expired (until you update them)
- When you post an updated version, the app is automatically
  considered updated (new VALIDITY takes over)
- Runs on Render free tier (keep-alive + self-ping)
"""

import os
import re
import asyncio
import logging
import sqlite3
import threading
import urllib.request
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not found! Set it in env vars.")

CHANNEL_IDS = [c.strip() for c in os.getenv("CHANNEL_ID", "").split(",") if c.strip()]
DB_PATH = os.getenv("DB_PATH", "tracker.db")
PORT = int(os.getenv("PORT", "10000"))

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

# Days before expiry to send an individual alert (0 = expiry day)
NOTIFY_DAYS = set()
for _d in os.getenv("NOTIFY_DAYS", "7,3,1,0").split(","):
    _d = _d.strip()
    if _d:
        try:
            NOTIFY_DAYS.add(int(_d))
        except ValueError:
            pass

CHECK_INTERVAL_HOURS = 6
IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keep-alive (Render free tier)
# ---------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Update Tracker Bot is running!")

    def log_message(self, fmt, *args):
        pass


def start_keep_alive(port: int) -> None:
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info("Keep-alive server listening on port %d", port)
        server.serve_forever()
    except OSError as e:
        logger.warning("Could not start keep-alive server: %s", e)


def self_ping():
    if not RENDER_EXTERNAL_URL:
        logger.info("RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    ping_url = RENDER_EXTERNAL_URL.rstrip("/") + "/"
    logger.info("Self-ping enabled: %s (every 5 min)", ping_url)
    import time as _t
    while True:
        try:
            urllib.request.urlopen(ping_url, timeout=10)
        except Exception as e:
            logger.warning("Self-ping failed: %s", e)
        _t.sleep(300)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
VALIDITY_PATTERN = re.compile(
    r"VALIDITY\s*[:\-]\s*(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})", re.IGNORECASE)
HASHTAG_PATTERN = re.compile(r"#([A-Za-z0-9][A-Za-z0-9_]{1,40})")


def today_ist() -> date:
    return datetime.now(IST).date()


def parse_validity(text: str):
    """Extract VALIDITY date from post text. Returns date or None."""
    m = VALIDITY_PATTERN.search(text)
    if not m:
        return None
    raw = m.group(1)
    # DD/MM/YYYY (channel's standard format) and variations
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # 2-digit year → assume 2000s
    for fmt in ("%d/%m/%y", "%d-%m-%y"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return d.replace(year=2000 + (d.year % 100))
        except ValueError:
            continue
    # ISO just in case
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def extract_app_name(text: str) -> str:
    """App name = first #hashtag in the post (APK INFO :- #AppName)."""
    m = HASHTAG_PATTERN.search(text)
    return m.group(1) if m else ""


def build_message_link(chat_id: int, message_id: int) -> str:
    if chat_id < 0:
        positive = str(chat_id).replace("-100", "", 1)
        return f"https://t.me/c/{positive}/{message_id}"
    return f"https://t.me/c/{chat_id}/{message_id}"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_apps (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id    INTEGER NOT NULL UNIQUE,
            chat_id       INTEGER NOT NULL,
            app_name      TEXT NOT NULL,
            validity_date TEXT,
            link          TEXT,
            posted_at     TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            message_id  INTEGER NOT NULL,
            stage       TEXT NOT NULL,
            notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, stage)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_app_name ON tracked_apps(app_name)")
    conn.commit()
    conn.close()


def store_post(message_id, chat_id, app_name, validity, link, posted_at=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO tracked_apps "
        "(message_id, chat_id, app_name, validity_date, link, posted_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, chat_id, app_name,
         validity.isoformat() if validity else None,
         link, posted_at))
    # A new post for the same app resets its notification history
    conn.execute("DELETE FROM notifications WHERE message_id != ? AND message_id IN "
                 "(SELECT message_id FROM tracked_apps WHERE app_name = ?)",
                 (message_id, app_name))
    conn.commit()
    conn.close()


def get_latest_apps():
    """Only the newest post per app (highest message_id = newest)."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT t.message_id, t.app_name, t.validity_date, t.link
        FROM tracked_apps t
        WHERE t.message_id = (
            SELECT MAX(t2.message_id) FROM tracked_apps t2
            WHERE t2.app_name = t.app_name
        )
    """).fetchall()
    conn.close()
    return rows


def already_notified(message_id: int, stage: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE message_id = ? AND stage = ?",
        (message_id, stage)).fetchone()
    conn.close()
    return row is not None


def mark_notified(message_id: int, stage: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO notifications (message_id, stage) VALUES (?, ?)",
        (message_id, stage))
    conn.commit()
    conn.close()


def get_meta(key: str, default=None):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_meta(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_post_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM tracked_apps").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Telethon import (channel history)
# ---------------------------------------------------------------------------
async def import_channel_history(channel_target):
    if not SESSION_STRING or not API_ID or not API_HASH:
        logger.warning("SESSION_STRING/API_ID/API_HASH missing — import skipped.")
        return 0, 0
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        logger.warning("Telethon not installed — import skipped.")
        return 0, 0

    imported, skipped = 0, 0
    try:
        target = int(channel_target)
    except ValueError:
        target = channel_target

    client = None
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.start()
        entity = await client.get_entity(target)
        title = getattr(entity, "title", str(channel_target))
        logger.info("Importing validity data from: %s", title)

        async for message in client.iter_messages(entity):
            text = message.text or message.message or ""
            if not text:
                skipped += 1
                continue
            validity = parse_validity(text)
            app_name = extract_app_name(text)
            if not validity or not app_name:
                skipped += 1
                continue
            chat_id = message.chat_id
            if hasattr(message.peer_id, "channel_id"):
                chat_id = int(f"-100{message.peer_id.channel_id}")
            link = build_message_link(chat_id, message.id)
            posted_at = message.date.isoformat() if message.date else None
            store_post(message.id, chat_id, app_name, validity, link, posted_at)
            imported += 1
            if imported % 200 == 0:
                logger.info("[%s] %d posts imported...", title, imported)

        logger.info("✅ [%s] Import done: %d tracked, %d skipped (no validity/date)",
                    title, imported, skipped)
    except Exception as e:
        logger.error("Import failed for %s: %s", channel_target, e)
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
    return imported, skipped


async def import_all_channels():
    for cid in CHANNEL_IDS:
        await import_channel_history(cid)
    logger.info("✅ Total tracked posts: %d", get_post_count())


# ---------------------------------------------------------------------------
# Expiry check + notifications
# ---------------------------------------------------------------------------
async def send_dm(app, text: str) -> bool:
    """DM the owner. Returns True if sent."""
    if not OWNER_ID:
        logger.warning("OWNER_ID not set — cannot notify!")
        return False
    try:
        await app.bot.send_message(
            chat_id=OWNER_ID, text=text,
            parse_mode="Markdown", disable_web_page_preview=True)
        return True
    except Forbidden:
        logger.error(
            "❌ Cannot DM owner! Open the bot in Telegram and send /start "
            "once so it is allowed to message you.")
        return False
    except Exception as e:
        logger.error("DM failed: %s", e)
        return False


async def run_check(app, force_digest=False):
    """Main check: alerts for soon-to-expire apps + daily expired digest."""
    today = today_ist()
    rows = get_latest_apps()
    if not rows:
        return

    # ---- 1) Individual stage alerts (7/3/1/0 days before) ----
    for message_id, app_name, validity_str, link in rows:
        if not validity_str:
            continue
        validity = date.fromisoformat(validity_str)
        days_left = (validity - today).days

        if days_left >= 0 and days_left in NOTIFY_DAYS:
            stage = f"d{days_left}"
            if already_notified(message_id, stage):
                continue
            v_txt = validity.strftime("%d/%m/%Y")
            if days_left == 0:
                msg = (f"🔴 *{app_name}* AAJ expire ho raha hai!\n\n"
                       f"📅 Validity: {v_txt} (aaj)\n"
                       f"🔗 Post: {link}\n\n"
                       f"➡️ Jaldi update kar do bhai!")
            else:
                msg = (f"🟠 *{app_name}* sirf *{days_left} din* me expire hoga!\n\n"
                       f"📅 Validity: {v_txt}\n"
                       f"🔗 Post: {link}\n\n"
                       f"➡️ Time milte hi update kar lena.")
            if await send_dm(app, msg):
                mark_notified(message_id, stage)
                logger.info("Notified: %s (%d days left)", app_name, days_left)

    # ---- 2) Daily digest of expired apps (until you update them) ----
    expired = []
    for message_id, app_name, validity_str, link in rows:
        if not validity_str:
            continue
        validity = date.fromisoformat(validity_str)
        if validity < today:
            ago = (today - validity).days
            expired.append((ago, app_name, validity.strftime("%d/%m/%Y"), link))

    today_key = today.isoformat()
    digest_sent_today = (get_meta("last_digest_date") == today_key)

    if expired and (force_digest or not digest_sent_today):
        expired.sort()
        lines = [f"🚨 *EXPIRED APPS — update pending!* 🚨\n"]
        for i, (ago, name, v_txt, link) in enumerate(expired[:30], 1):
            lines.append(f"{i}. *{name}* — {ago} din pehle expire hua ({v_txt})\n   🔗 {link}")
        if len(expired) > 30:
            lines.append(f"\n...aur {len(expired) - 30} more.")
        lines.append("\n➡️ In sab ke updates upload kar do!")
        if await send_dm(app, "\n".join(lines)):
            set_meta("last_digest_date", today_key)
            logger.info("Expired digest sent (%d apps)", len(expired))


async def periodic_check(app):
    """Background loop: check every CHECK_INTERVAL_HOURS."""
    while True:
        try:
            await run_check(app)
        except Exception as e:
            logger.error("Periodic check failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def is_owner(update: Update) -> bool:
    if not OWNER_ID:
        return True
    return update.effective_user and update.effective_user.id == OWNER_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text("❌ This is a personal bot. Access denied.")
        return
    count = get_post_count()
    await update.message.reply_text(
        "👋 *Update Tracker Bot*\n\n"
        "Main tumhare channel ke apps ki VALIDITY track karta hoon.\n\n"
        f"🟠 Alert: 7 / 3 / 1 din pehle + expiry day\n"
        f"🚨 Daily digest: expired apps (jab tak update nahi karte)\n\n"
        f"📚 Abhi *{count}* posts track ho rahe hain.\n\n"
        "Commands:\n"
        "/list — sab apps validity ke saath\n"
        "/check — abhi check karke batao\n"
        "/refresh — channel history dobara import\n\n"
        "💡 Naya app post karoge to uska nayi VALIDITY automatically "
        "track ho jayegi — purani expire wali hata jayegi.",
        parse_mode="Markdown")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    rows = get_latest_apps()
    if not rows:
        await update.message.reply_text("📺 Koi post track nahi ho raha. /refresh try karo.")
        return
    today = today_ist()
    parsed = []
    for message_id, app_name, validity_str, link in rows:
        if not validity_str:
            continue
        v = date.fromisoformat(validity_str)
        parsed.append((v, app_name))
    parsed.sort()

    expired = [p for p in parsed if p[0] < today]
    upcoming = [p for p in parsed if p[0] >= today]

    lines = [f"📋 *Tracked Apps ({len(parsed)} total)*\n"]
    if expired:
        lines.append(f"🔴 *Expired — update pending ({len(expired)}):*")
        for v, name in expired[:20]:
            d = (today - v).days
            lines.append(f"• {name} — {d} din pehle ({v.strftime('%d/%m/%Y')})")
        if len(expired) > 20:
            lines.append(f"...aur {len(expired) - 20} more")
        lines.append("")
    if upcoming:
        lines.append(f"🟢 *Upcoming ({len(upcoming)}):*")
        for v, name in upcoming[:20]:
            d = (v - today).days
            lines.append(f"• {name} — {d} din baad ({v.strftime('%d/%m/%Y')})")
        if len(upcoming) > 20:
            lines.append(f"...aur {len(upcoming) - 20} more")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text("⏳ Checking...")
    await run_check(context.application, force_digest=True)
    await update.message.reply_text("✅ Check complete! Agar kuch expire hua hai to DM dekh lo.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await update.message.reply_text("⏳ Channel history import ho rahi hai...")
    await import_all_channels()
    await update.message.reply_text(
        f"✅ Import done! Ab *{get_post_count()}* posts tracked hain.",
        parse_mode="Markdown")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Watch new channel posts — auto-track new apps/updates."""
    if not update.channel_post:
        return
    post = update.channel_post
    text = post.text or post.caption or ""
    if not text:
        return
    validity = parse_validity(text)
    app_name = extract_app_name(text)
    if not validity or not app_name:
        return
    link = build_message_link(post.chat.id, post.message_id)
    store_post(post.message_id, post.chat.id, app_name, validity, link)
    logger.info("New post tracked: %s (validity %s)", app_name, validity)
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"✅ *{app_name}* track ho gaya!\n📅 Validity: "
                f"{validity.strftime('%d/%m/%Y')}",
                parse_mode="Markdown")
        except Exception:
            pass


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception: %s", context.error)


async def post_init(application: Application) -> None:
    # Import history if DB is empty (e.g. first deploy / Render restart)
    if get_post_count() == 0:
        logger.info("Database empty — importing channel history...")
        await import_all_channels()
    else:
        logger.info("Tracked posts: %d — skipping full import.", get_post_count())
    # Run one check right away, then start the periodic loop
    try:
        await run_check(application)
    except Exception as e:
        logger.error("Startup check failed: %s", e)
    application.create_task(periodic_check(application))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    init_db()
    threading.Thread(target=start_keep_alive, args=(PORT,), daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POSTS, channel_post_handler))
    app.add_error_handler(error_handler)

    logger.info("Update Tracker Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
