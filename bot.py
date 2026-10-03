"""
Update Tracker Bot
==================
Personal Telegram bot that monitors your channel's app posts and
DMs YOU before any app's VALIDITY expires, so you never miss an update.

Parsing (post format, date formats, Lifetime handling) lives in
parsing.py — see that file for details.

Note: only posts that have BOTH an #AppName and a VALIDITY value are
tracked (that is what expiry tracking needs). Posts without a
VALIDITY line are skipped by design — run /stats to see the breakdown.

Everything is automatic:
- full import on first start / after a restart
- edited posts are picked up instantly
- channel history re-imports itself every AUTO_REFRESH_HOURS
  (no need to run /refresh by hand)
"""

import os
import json
import asyncio
import logging
import sqlite3
import threading
import urllib.request
from datetime import datetime, date
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

from parsing import (
    IST,
    NO_EXPIRY,
    today_ist,
    parse_validity,
    extract_app_name,
    build_message_link,
    split_text,
    validity_to_db,
    validity_label,
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

# How often the channel history is re-imported automatically (hours).
# Set to 0 to disable auto-refresh (then /refresh must be run by hand).
try:
    AUTO_REFRESH_HOURS = float(os.getenv("AUTO_REFRESH_HOURS", "12"))
except ValueError:
    AUTO_REFRESH_HOURS = 12.0

CHECK_INTERVAL_HOURS = 6

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
         validity_to_db(validity), link, posted_at))
    # A new post for the same app (any casing) resets its notification history
    conn.execute("DELETE FROM notifications WHERE message_id != ? AND message_id IN "
                 "(SELECT message_id FROM tracked_apps WHERE app_name = ? COLLATE NOCASE)",
                 (message_id, app_name))
    conn.commit()
    conn.close()


def get_latest_apps():
    """Only the newest post per app (highest message_id = newest).

    App names are compared case-insensitively, so #XRecorder and
    #Xrecorder count as the same app.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT t.message_id, t.app_name, t.validity_date, t.link
        FROM tracked_apps t
        WHERE t.message_id = (
            SELECT MAX(t2.message_id) FROM tracked_apps t2
            WHERE LOWER(t2.app_name) = LOWER(t.app_name)
        )
    """).fetchall()
    conn.close()
    return rows


def search_posts_for_app(name_query: str):
    """All stored posts whose app_name contains name_query."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT message_id, app_name, validity_date, link, posted_at
        FROM tracked_apps
        WHERE app_name LIKE ? COLLATE NOCASE
        ORDER BY message_id
    """, (f"%{name_query}%",)).fetchall()
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

    scanned = 0
    imported = 0
    no_text = 0
    no_app = 0
    no_validity = 0

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
            scanned += 1
            text = message.text or message.message or ""
            if not text:
                no_text += 1
                continue
            app_name = extract_app_name(text)
            if not app_name:
                no_app += 1
                continue
            validity = parse_validity(text)
            if not validity:
                no_validity += 1
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

        summary = {
            "channel": title,
            "scanned": scanned,
            "tracked": imported,
            "no_text": no_text,
            "no_app_name": no_app,
            "no_validity": no_validity,
            "at": datetime.now(IST).isoformat(),
        }
        set_meta("last_import", json.dumps(summary))
        logger.info(
            "✅ [%s] scanned=%d tracked=%d (no_text=%d no_app_name=%d no_validity=%d)",
            title, scanned, imported, no_text, no_app, no_validity)
    except Exception as e:
        logger.error("Import failed for %s: %s", channel_target, e)
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
    return imported, no_validity


async def import_all_channels():
    for cid in CHANNEL_IDS:
        await import_channel_history(cid)
    logger.info("✅ Total tracked posts: %d", get_post_count())


# ---------------------------------------------------------------------------
# Expiry check + notifications
# ---------------------------------------------------------------------------
async def send_dm(app, text: str) -> bool:
    """DM the owner. Splits into multiple messages if too long."""
    if not OWNER_ID:
        logger.warning("OWNER_ID not set — cannot notify!")
        return False
    try:
        chunks = split_text(text)
        for i, chunk in enumerate(chunks):
            try:
                await app.bot.send_message(
                    chat_id=OWNER_ID, text=chunk,
                    parse_mode="Markdown", disable_web_page_preview=True)
            except Forbidden:
                if i == 0:
                    raise
                return True
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)
        return True
    except Forbidden:
        logger.error(
            "❌ Cannot DM owner! Open the bot in Telegram and send /start "
            "once so it is allowed to message you.")
        return False
    except Exception as e:
        logger.error("DM failed: %s", e)
        return False


async def reply_chunks(message, text: str, preview: bool = False) -> None:
    """Reply, splitting into multiple messages if text is too long."""
    for i, chunk in enumerate(split_text(text)):
        try:
            await message.reply_text(
                chunk, parse_mode="Markdown",
                disable_web_page_preview=not preview)
        except Exception as e:
            logger.error("Reply chunk %d failed: %s", i, e)
            return
        await asyncio.sleep(0.3)


async def run_check(app, force_digest=False):
    """Main check: alerts for soon-to-expire apps + daily expired digest."""
    today = today_ist()
    rows = get_latest_apps()
    if not rows:
        return

    # ---- 1) Individual stage alerts (7/3/1/0 days before) ----
    for message_id, app_name, validity_str, link in rows:
        if not validity_str or validity_str == NO_EXPIRY:
            continue
        try:
            validity = date.fromisoformat(validity_str)
        except ValueError:
            continue
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
        if not validity_str or validity_str == NO_EXPIRY:
            continue
        try:
            validity = date.fromisoformat(validity_str)
        except ValueError:
            continue
        if validity < today:
            ago = (today - validity).days
            expired.append((ago, app_name, validity.strftime("%d/%m/%Y"), link))

    today_key = today.isoformat()
    digest_sent_today = (get_meta("last_digest_date") == today_key)

    if expired and (force_digest or not digest_sent_today):
        expired.sort()
        lines = ["🚨 *EXPIRED APPS — update pending!* 🚨"]
        for i, (ago, name, v_txt, link) in enumerate(expired, 1):
            lines.append(f"{i}. *{name}* — {ago} din pehle expire hua ({v_txt})\n   🔗 {link}")
        lines.append("\n➡️ In sab ke updates upload kar do!")
        if await send_dm(app, "\n".join(lines)):
            set_meta("last_digest_date", today_key)
            logger.info("Expired digest sent (%d apps)", len(expired))


# JobQueue callbacks (preferred scheduling path)
async def job_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_check(context.application)
    except Exception as e:
        logger.error("Scheduled check failed: %s", e)


async def job_refresh(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        logger.info("Auto-refresh: re-importing channel history...")
        await import_all_channels()
        set_meta("last_auto_refresh", datetime.now(IST).isoformat())
    except Exception as e:
        logger.error("Auto-refresh failed: %s", e)


async def periodic_check(app):
    """Fallback background loop (used only if job-queue is unavailable)."""
    while True:
        try:
            await run_check(app)
        except Exception as e:
            logger.error("Periodic check failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)


async def periodic_refresh(app):
    """Fallback background loop (used only if job-queue is unavailable)."""
    if AUTO_REFRESH_HOURS <= 0:
        return
    while True:
        await asyncio.sleep(AUTO_REFRESH_HOURS * 3600)
        try:
            logger.info("Auto-refresh: re-importing channel history...")
            await import_all_channels()
            set_meta("last_auto_refresh", datetime.now(IST).isoformat())
        except Exception as e:
            logger.error("Auto-refresh failed: %s", e)


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
    refresh_txt = (f"har {AUTO_REFRESH_HOURS:g} ghante"
                   if AUTO_REFRESH_HOURS > 0 else "off")
    await update.message.reply_text(
        "👋 *Update Tracker Bot*\n\n"
        "Main tumhare channel ke apps ki VALIDITY track karta hoon.\n\n"
        "🟠 Alert: 7 / 3 / 1 din pehle + expiry day\n"
        "🚨 Daily digest: expired apps (jab tak update nahi karte)\n"
        "♾️ 'Lifetime' / 'Untill Update' apps skip ho jate hain\n"
        f"🔄 Auto-refresh: {refresh_txt} (manual /refresh ki zaroorat nahi)\n\n"
        f"📚 Abhi *{count}* posts track ho rahe hain.\n\n"
        "Commands:\n"
        "/list — sab apps validity ke saath\n"
        "/stats — kitne posts scan hue vs track hue\n"
        "/debug AppName — app ka data check karo\n"
        "/check — abhi check karke batao\n"
        "/raw — recent posts ki VALIDITY line raw dikhao\n"
        "/refresh — history abhi import karo (emergency ke liye)",
        parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show why only some posts are tracked."""
    if not is_owner(update):
        return
    lines = ["📊 *Update Tracker stats*\n",
             f"📚 Tracked posts: *{get_post_count()}*\n"]
    raw = get_meta("last_import")
    if raw:
        try:
            s = json.loads(raw)
            when = (s.get("at") or "")[:16].replace("T", " ")
            lines += [
                f"🔎 Last import ({when} IST):",
                f"• messages scanned: *{s.get('scanned', 0)}*",
                f"• tracked (has VALIDITY): *{s.get('tracked', 0)}*",
                f"• skipped — no text: {s.get('no_text', 0)}",
                f"• skipped — no #AppName: {s.get('no_app_name', 0)}",
                f"• skipped — no VALIDITY: {s.get('no_validity', 0)}",
            ]
        except Exception:
            pass
    last_refresh = get_meta("last_auto_refresh")
    if last_refresh:
        lines.append(f"\n🔄 Last auto-refresh: {last_refresh[:16].replace('T', ' ')} IST")
    lines.append(
        "\n💡 Tracker sirf un posts ko rakhta hai jinme *VALIDITY* ho "
        "(expiry alert ke liye). Jinke paas VALIDITY nahi hai wo skip "
        "ho jate hain — search bot (MOD MANAGER) sab posts rakhta hai, "
        "isliye uska count zyada hota hai.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    rows = get_latest_apps()
    if not rows:
        await update.message.reply_text("📺 Koi post track nahi ho raha. /refresh try karo.")
        return
    today = today_ist()
    expired, upcoming, lifetime = [], [], []
    for message_id, app_name, validity_str, link in rows:
        if validity_str == NO_EXPIRY:
            lifetime.append(app_name)
            continue
        if not validity_str:
            continue
        try:
            v = date.fromisoformat(validity_str)
        except ValueError:
            continue
        (expired if v < today else upcoming).append((v, app_name))
    expired.sort()
    upcoming.sort()
    lifetime.sort()

    total = len(expired) + len(upcoming) + len(lifetime)
    lines = [f"📋 *Tracked Apps ({total} total)*\n"]
    if expired:
        lines.append(f"🔴 *Expired — update pending ({len(expired)}):*")
        for v, name in expired:
            d = (today - v).days
            lines.append(f"• {name} — {d} din pehle ({v.strftime('%d/%m/%Y')})")
        lines.append("")
    if upcoming:
        lines.append(f"🟢 *Upcoming ({len(upcoming)}):*")
        for v, name in upcoming:
            d = (v - today).days
            lines.append(f"• {name} — {d} din baad ({v.strftime('%d/%m/%Y')})")
        lines.append("")
    if lifetime:
        lines.append(f"♾️ *No expiry — Lifetime / Till update ({len(lifetime)}):*")
        for name in lifetime:
            lines.append(f"• {name}")
    await reply_chunks(update.message, "\n".join(lines))


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect what the bot has stored for an app (and why it's missing)."""
    if not is_owner(update):
        return
    if not context.args:
        rows = get_latest_apps()
        if not rows:
            await update.message.reply_text("DB khali hai! /refresh chalao.")
            return
        lines = [f"🔍 *All tracked apps ({len(rows)}):*\n"]
        for message_id, app_name, validity_str, link in rows:
            lines.append(f"• {app_name} — {validity_label(validity_str)}")
        lines.append("\nUsage: `/debug AppName`")
        await reply_chunks(update.message, "\n".join(lines))
        return

    name = context.args[0].lstrip("#")
    rows = search_posts_for_app(name)
    if not rows:
        await update.message.reply_text(
            f"❌ *{name}* DB me nahi hai!\n\n"
            "Matlab iske posts parse nahi hue — VALIDITY line ya #hashtag "
            "nahi mila. `/raw` chala ke raw text dekho.",
            parse_mode="Markdown")
        return

    today = today_ist()
    lines = [f"🔍 *Stored posts for '{name}' ({len(rows)}):*\n"]
    latest_mid = max(r[0] for r in rows)
    for message_id, app_name, validity_str, link, posted_at in rows[:30]:
        if validity_str == NO_EXPIRY:
            status = "LIFETIME"
        else:
            try:
                v = date.fromisoformat(validity_str)
                status = "EXPIRED" if v < today else f"{max((v - today).days, 0)} din baad"
            except (ValueError, TypeError):
                status = "no date"
        marker = " ⬅️ ACTIVE (latest)" if message_id == latest_mid else ""
        posted = (posted_at or "")[:10]
        lines.append(
            f"• msg {message_id}: *{app_name}* — {validity_label(validity_str)} "
            f"[{status}]{marker}\n  posted: {posted}\n  {link}")
    lines.append("\nSirf *latest* post track hota hai (⬅️ wala).")
    await reply_chunks(update.message, "\n".join(lines), preview=False)


async def raw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic: show the raw characters around VALIDITY in recent posts.

    Invisible characters appear here as unicode escapes (backslash-u
    followed by four hex digits), which makes it easy to see why a
    date failed to parse.
    """
    if not is_owner(update):
        return
    if not (SESSION_STRING and API_ID and API_HASH and CHANNEL_IDS):
        await update.message.reply_text(
            "Session / API / CHANNEL_ID set nahi hai — /raw available nahi.")
        return
    await update.message.reply_text("🔬 Recent posts scan kar raha hoon...")
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        await update.message.reply_text("Telethon installed nahi hai.")
        return

    try:
        target = int(CHANNEL_IDS[0])
    except ValueError:
        target = CHANNEL_IDS[0]

    client = None
    lines = []
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.start()
        entity = await client.get_entity(target)
        import re as _re
        async for message in client.iter_messages(entity, limit=400):
            text = message.text or ""
            if not text:
                continue
            m = _re.search("VALIDITY", text, _re.IGNORECASE)
            if not m:
                continue
            snippet = text[m.end():m.end() + 40]
            lines.append(f"msg {message.id}: {snippet!r}")
            if len(lines) >= 8:
                break
    except Exception as e:
        logger.error("/raw failed: %s", e)
        await update.message.reply_text(f"❌ Scan fail hua: {e}")
        return
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    if not lines:
        await update.message.reply_text("Koi VALIDITY line nahi mili.")
        return
    # Sent WITHOUT Markdown so the repr escapes stay readable
    body = "🔬 Raw VALIDITY text (newest first):\n\n" + "\n".join(lines)
    for chunk in split_text(body):
        await update.message.reply_text(chunk, disable_web_page_preview=True)


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
        f"✅ Import done! Ab *{get_post_count()}* posts tracked hain.\n"
        "`/stats` chala ke pura breakdown dekho.",
        parse_mode="Markdown")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Watch new AND edited channel posts — auto-track apps/updates."""
    post = update.channel_post or update.edited_channel_post
    if not post:
        return
    text = post.text or post.caption or ""
    if not text:
        return
    validity = parse_validity(text)
    app_name = extract_app_name(text)
    if not validity or not app_name:
        return
    link = build_message_link(post.chat.id, post.message_id)
    store_post(post.message_id, post.chat.id, app_name, validity, link)
    logger.info("Channel post tracked: %s (validity %s)",
                app_name, validity_to_db(validity))
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"✅ *{app_name}* track ho gaya!\n📅 Validity: "
                f"{validity_label(validity_to_db(validity))}",
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
    # Run one check right away
    try:
        await run_check(application)
    except Exception as e:
        logger.error("Startup check failed: %s", e)

    # Schedule the recurring jobs. JobQueue is the clean PTB way; if the
    # job-queue extra isn't installed we fall back to plain asyncio tasks.
    try:
        job_queue = application.job_queue
    except Exception:
        job_queue = None
    if job_queue is not None:
        job_queue.run_repeating(
            job_check, interval=CHECK_INTERVAL_HOURS * 3600,
            first=CHECK_INTERVAL_HOURS * 3600, name="expiry-check")
        if AUTO_REFRESH_HOURS > 0:
            job_queue.run_repeating(
                job_refresh, interval=AUTO_REFRESH_HOURS * 3600,
                first=AUTO_REFRESH_HOURS * 3600, name="auto-refresh")
        logger.info("Scheduled jobs: expiry-check every %gh, auto-refresh %gh",
                    CHECK_INTERVAL_HOURS, AUTO_REFRESH_HOURS)
    else:
        logger.info("JobQueue unavailable — using asyncio task fallback.")
        application.create_task(periodic_check(application))
        application.create_task(periodic_refresh(application))


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
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("raw", raw_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POSTS, channel_post_handler))
    # Also react to EDITED channel posts (date fixes etc.)
    try:
        app.add_handler(MessageHandler(
            filters.UpdateType.EDITED_CHANNEL_POST, channel_post_handler))
    except AttributeError:
        logger.warning(
            "EDITED_CHANNEL_POST filter unavailable — edited posts will "
            "be caught by the periodic auto-refresh instead.")
    app.add_error_handler(error_handler)

    logger.info("Update Tracker Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
