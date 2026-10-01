"""
Parsing helpers for the Update Tracker Bot.

Handles the channel's post format:
    APK INFO :- #AppName ...
    FEATURES INFO :- #Something   (ignored — not the app name)
    VALIDITY :- DD/MM/YYYY

VALIDITY values supported:
- dates: 16/09/2026, 16-09-2026, 16.09.2026, 16 / 09 / 2026,
  16 Sep 2026, 16th September 2026, 2026-09-16, MM/DD (when day > 12),
  2-digit years
- day + month without a year: '15 Jun' (assumes the current year)
- no-expiry text: 'Lifetime', 'Never Expire', 'Untill Update',
  'No Expiry', 'Unlimited'  ->  returned as NO_EXPIRY

The date window is scrubbed down to digits + separators only, so ANY
invisible / decorative character (zero-width, Hangul filler, Braille
blank, soft hyphen, ...) can no longer break a date.
"""

import re
import logging
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Telegram's hard limit is 4096 chars — stay safely below it
MSG_CHUNK_LIMIT = 3500

# Sanity range for validity years (guards against broken/truncated dates)
MIN_YEAR, MAX_YEAR = 2000, 2100

# Returned by parse_validity() for apps whose post says Lifetime / Untill Update
NO_EXPIRY = "LIFETIME"
NO_EXPIRY_LABEL = "♾️ Lifetime / Till update"

# Invisible / zero-width / soft-hyphen characters that sneak in when a
# post template is copy-pasted.
INVISIBLE_RE = re.compile(
    r"[\s\u00ad\u180e\u200b\u200c\u200d\u2060\ufeff\u200e\u200f]+")
# For dates we go further: keep ONLY digits and separators. This removes
# every possible invisible/decorative char (Hangul fillers, Braille
# blanks, bidi marks, etc.) without needing to list them all.
DATE_KEEP_RE = re.compile(r"[^0-9/.\-]")

# "APK INFO :- #AppName" — prefer this so FEATURES hashtags don't confuse us.
APK_INFO_PATTERN = re.compile(
    r"APK\s*INFO[\s:\-]{1,8}#([A-Za-z0-9_]+)", re.IGNORECASE)
# plain hashtag fallback (only used when APK INFO line is missing)
HASHTAG_PATTERN = re.compile(r"#([A-Za-z0-9][A-Za-z0-9_]{1,40})")

VALIDITY_HEAD_PATTERN = re.compile(r"VALIDITY", re.IGNORECASE)
# "VALIDITY :- 16 Sep 2026" / "16th September 2026"
VALIDITY_TEXT_PATTERN = re.compile(
    r"VALIDITY[\s:\-]{1,8}(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{2,4})",
    re.IGNORECASE)
# "15 Jun" — day + month, no year
DAY_MONTH_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\b")

NUM_DATE_RE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})")
ISO_DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# Words that mean "this app never expires" — a very common VALIDITY value
# in the channel (e.g. 'VALIDITY :- Lifetime', 'VALIDITY :- Untill Update').
NO_EXPIRY_KEYWORDS = (
    "lifetime", "never expire", "never expires", "no expiry",
    "non-expiry", "non expiry", "until update", "untill update",
    "till update", "unlimited", "no expiration", "never exp",
)


def today_ist() -> date:
    return datetime.now(IST).date()


def strip_invisible(s: str) -> str:
    """Remove whitespace and common invisible/zero-width characters."""
    return INVISIBLE_RE.sub("", s)


def scrub_date(s: str) -> str:
    r"""Keep only digits and date separators.

    Removes every invisible / decorative character, so a date like
    '21/10/20' + (Hangul filler) + '26' becomes '21/10/2026'.
    """
    return DATE_KEEP_RE.sub("", s)


def _year_ok(y: int) -> bool:
    return MIN_YEAR <= y <= MAX_YEAR


def _month_from_name(name: str):
    month_l = re.sub(r"[^a-z]", "", name.lower())
    for k, v in MONTHS.items():
        if month_l.startswith(k):
            return v
    return None


def parse_validity(text: str):
    """Extract VALIDITY from post text.

    Returns:
        date        -> a normal expiry date
        NO_EXPIRY   -> lifetime / till-update app (never expires)
        None        -> could not parse (post is skipped)
    """
    # 1) Textual month WITH year: 16 Sep 2026 / 16th September 2026
    m = VALIDITY_TEXT_PATTERN.search(text)
    if m:
        day_s, mon_s, year_s = m.groups()
        month = _month_from_name(mon_s)
        if month:
            try:
                year = int(strip_invisible(year_s))
                if year < 100:
                    year += 2000
                if _year_ok(year):
                    return date(year, month, int(strip_invisible(day_s)))
            except ValueError:
                pass

    # 2) Everything else is read from the window right after 'VALIDITY'
    m = VALIDITY_HEAD_PATTERN.search(text)
    if not m:
        return None
    raw_window = text[m.end():m.end() + 80]

    # 2a) No-expiry text: 'VALIDITY :- Lifetime', ' :- Untill Update', ...
    head_l = raw_window[:40].lower()
    if any(k in head_l for k in NO_EXPIRY_KEYWORDS):
        return NO_EXPIRY

    # 2b) Numeric / ISO date (window scrubbed to digits + separators)
    window = scrub_date(raw_window)

    iso = ISO_DATE_RE.search(window)          # 2026-09-16
    if iso:
        y, mo, d = (int(x) for x in iso.groups())
        if _year_ok(y):
            try:
                return date(y, mo, d)
            except ValueError:
                pass

    dm = NUM_DATE_RE.search(window)           # 16/09/2026 (DD/MM)
    if dm:
        a, b, y = (int(x) for x in dm.groups())
        if y < 100:
            y += 2000
        if not _year_ok(y):
            logger.warning("Odd validity year %d | raw window=%r",
                           y, raw_window[:60])
        elif a <= 31 and b <= 31:
            day, month = a, b
            if a <= 12 and b > 12:      # looks like MM/DD
                day, month = b, a
            try:
                return date(y, month, day)
            except ValueError:
                pass

    # 2c) Day + month without a year: 'VALIDITY :- 15 Jun'
    mo_match = DAY_MONTH_RE.search(raw_window[:30])
    if mo_match:
        day_s, mon_s = mo_match.groups()
        month = _month_from_name(mon_s)
        if month:
            try:
                day_n = int(day_s)
                today = today_ist()
                candidate = date(today.year, month, day_n)
                # a month-only date far in the past probably means next year
                if (today - candidate).days > 180:
                    candidate = date(today.year + 1, month, day_n)
                return candidate
            except ValueError:
                pass

    logger.warning("No date found after VALIDITY (raw: %r)", raw_window[:60])
    return None


def extract_app_name(text: str) -> str:
    """App name = hashtag right after 'APK INFO', else first hashtag.

    The 'APK INFO :- #SonyLiv' line has the app name. The
    'FEATURES INFO :- #PrimeVideo' line is NOT the app — with the
    fallback we might grab it, so APK INFO is always preferred.
    """
    m = APK_INFO_PATTERN.search(text)
    if m:
        return strip_invisible(m.group(1))
    m = HASHTAG_PATTERN.search(text)
    return strip_invisible(m.group(1)) if m else ""


def build_message_link(chat_id: int, message_id: int) -> str:
    if chat_id < 0:
        positive = str(chat_id).replace("-100", "", 1)
        return f"https://t.me/c/{positive}/{message_id}"
    return f"https://t.me/c/{chat_id}/{message_id}"


def split_text(text: str, max_len: int = MSG_CHUNK_LIMIT) -> list:
    """Split a long message into chunks that fit Telegram's 4096 limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = current + "\n" + line if current else line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def validity_to_db(validity):
    """date -> ISO string, NO_EXPIRY -> sentinel, None -> None."""
    if validity is None:
        return None
    if isinstance(validity, str):
        return validity
    return validity.isoformat()


def validity_label(validity_str) -> str:
    """Human-readable validity for messages."""
    if not validity_str:
        return "unknown"
    if validity_str == NO_EXPIRY:
        return NO_EXPIRY_LABEL
    try:
        return date.fromisoformat(validity_str).strftime("%d/%m/%Y")
    except ValueError:
        return str(validity_str)
