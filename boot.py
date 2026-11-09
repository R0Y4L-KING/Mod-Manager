"""
Boot shim for MODAPPSKING Search Bot
====================================
Loads bot.py, installs improved app-matching, then starts the bot.

What this shim changes
----------------------
1. DIRECT MATCH (no AI, no quota): if a known app name appears in the
   message, reply immediately — works even when the Gemini free-tier
   quota is exhausted.
2. STRICT FUZZY MATCH: similarity-based (difflib >= 0.75) instead of
   naive substring matching, so wrong apps are no longer returned.
3. CLEAN REPLY + INLINE BUTTON, e.g.
       Here Is Your *Lumii* Pro ⬇️
       📅 Validity: 07/12/2026
       [ ⬇️ DOWNLOAD LUMII ]
4. VALIDITY LOOKUP: reads the newest post that actually contains a
   VALIDITY line (falls back through recent posts).
5. EDITED POSTS: when you edit a channel post (e.g. add/fix VALIDITY)
   the stored text is updated immediately instead of going stale.
6. PERIODIC RE-IMPORT every 6h so new/edited posts are picked up even
   if the bot was asleep.

Start command should be:  python boot.py
"""

import re
import time
import sqlite3
import asyncio
import logging
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import MessageHandler, filters

import bot

logger = logging.getLogger("boot")

DB_PATH = bot.DB_PATH
AUTO_IMPORT_HOURS = 6


# ---------------------------------------------------------------------------
# Direct matching (no AI)
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


_cache = {"count": -1, "items": []}


def _invalidate_cache():
    _cache["count"] = -1


def _get_name_index():
    """Cached [(normalized_key, app_name)] from the DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM channel_posts").fetchone()[0]
        conn.close()
    except Exception:
        count = -1
    if count == _cache["count"] and _cache["items"]:
        return _cache["items"]
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT app_name FROM channel_posts WHERE app_name IS NOT NULL"
    ).fetchall()
    conn.close()
    items = []
    for (name,) in rows:
        if not name:
            continue
        key = _norm(name)
        if key:
            items.append((key, name))
    _cache["count"] = count
    _cache["items"] = items
    return items


def _latest_post(app_name: str):
    """Newest stored post for an exact app name."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT app_name, full_text, link, message_id FROM channel_posts "
        "WHERE app_name = ? ORDER BY message_id DESC LIMIT 1", (app_name,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"app_name": row[0], "text": row[1], "link": row[2], "message_id": row[3]}


def find_direct_matches(message_text: str, limit: int = 5) -> list:
    """Known app names that literally appear in the message (no AI)."""
    if len(message_text.split()) > 15:
        return []
    msg_norm = _norm(message_text)
    if not msg_norm:
        return []
    tokens = {_norm(t) for t in re.split(r"[^A-Za-z0-9]+", message_text) if t}

    hits = []
    for key, app_name in _get_name_index():
        if len(key) <= 3:
            ok = key in tokens
        else:
            ok = key in msg_norm
        if ok:
            hits.append((len(key), app_name))
    if not hits:
        return []

    hits.sort(reverse=True)          # longer / more specific names first
    out = []
    for _, app_name in hits[:limit]:
        post = _latest_post(app_name)
        if post:
            out.append(post)
    return out


def fuzzy_search(app_query: str, limit: int = 3) -> list:
    """Strict close-match fallback (no AI call)."""
    from difflib import SequenceMatcher
    q = _norm(app_query)
    if len(q) < 3:
        return []
    scored = []
    for key, app_name in _get_name_index():
        if len(key) < 3:
            continue
        if q == key:
            ratio = 1.0
        elif (q in key and len(q) / len(key) >= 0.6) or \
             (key in q and len(key) / len(q) >= 0.6):
            ratio = 0.9
        else:
            ratio = SequenceMatcher(None, q, key).ratio()
        if ratio >= 0.75:
            scored.append((ratio, app_name))
    scored.sort(reverse=True)
    out = []
    for _, app_name in scored[:limit]:
        post = _latest_post(app_name)
        if post:
            out.append(post)
    return out


# ---------------------------------------------------------------------------
# Validity lookup + reply formatting
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})")
_LIFETIME_RE = re.compile(
    r"(lifetime|till\s*update|until\s*update|never\s*expire)", re.IGNORECASE)


def _validity_from_text(text: str) -> str:
    """Pull the VALIDITY value out of a post text (tolerant)."""
    if not text:
        return ""
    m = re.search(r"VALIDITY", text, re.IGNORECASE)
    if m:
        window = text[m.end():m.end() + 45]
        d = _DATE_RE.search(window)
        if d:
            return f"{d.group(1)}/{d.group(2)}/{d.group(3)}"
        lt = _LIFETIME_RE.search(window)
        if lt:
            return "Lifetime"
    if _LIFETIME_RE.search(text):
        return "Lifetime"
    return ""


def _validity_for(app_name: str) -> str:
    """Validity for an app: newest post that actually has a VALIDITY line."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT full_text FROM channel_posts WHERE app_name = ? "
        "ORDER BY message_id DESC LIMIT 8", (app_name,)
    ).fetchall()
    conn.close()
    for (text,) in rows:
        v = _validity_from_text(text or "")
        if v:
            return v
    return ""


def _button(label: str, link: str):
    """One inline URL button, or None when there is no link."""
    if not link:
        return None
    return InlineKeyboardButton(label, url=link)


async def send_results(update, results: list, query: str) -> None:
    """Clean reply + inline download button."""
    if len(results) == 1:
        post = results[0]
        name = post["app_name"] or query
        val = _validity_for(name)

        text = f"Here Is Your *{bot.escape(name)}* Pro ⬇️"
        if val:
            text += f"\n\n📅 Validity: {bot.escape(val)}"

        btn = _button(f"⬇️ DOWNLOAD {name.upper()}", post.get("link"))
        markup = InlineKeyboardMarkup([[btn]]) if btn else None
        await update.message.reply_text(
            text, parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=markup)
        return

    lines = [f"Here Is Your *{bot.escape(query)}* Pro ⬇️\n"]
    rows = []
    for i, post in enumerate(results, 1):
        name = post["app_name"] or "Unknown"
        val = _validity_for(name)
        line = f"{i}. *{bot.escape(name)}*"
        if val:
            line += f" — 📅 {bot.escape(val)}"
        lines.append(line)
        btn = _button(f"{i}. ⬇️ {name.upper()}", post.get("link"))
        if btn:
            rows.append([btn])

    markup = InlineKeyboardMarkup(rows) if rows else None
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        disable_web_page_preview=True, reply_markup=markup)


# ---------------------------------------------------------------------------
# Improved find_app
# ---------------------------------------------------------------------------
async def find_app(update, context) -> None:
    if not update.message or not update.message.text:
        return

    message_text = update.message.text

    direct = find_direct_matches(message_text, limit=5)
    if direct:
        logger.info("Direct match: %s (from: '%s')",
                    direct[0]["app_name"], message_text)
        await send_results(update, direct, direct[0]["app_name"])
        return

    gemini_on = bot._gemini_available()
    if not bot.should_process_message(message_text, gemini_on):
        return

    app_query = await bot.gemini_extract_app_name(message_text)
    results = []
    if app_query and len(app_query) >= 2:
        logger.info("Searching: '%s' (from: '%s')", app_query, message_text)
        results = bot.search_by_app_name(app_query, limit=5)
        if not results:
            results = fuzzy_search(app_query)

    if not results:
        if bot.HASHTAG_PATTERN.search(message_text):
            q = app_query or message_text.strip()
            await update.message.reply_text(
                f"❌ No match for *{bot.escape(q)}*.", parse_mode="Markdown")
        return

    await send_results(update, results, app_query or results[0]["app_name"])


# ---------------------------------------------------------------------------
# Edited channel posts — keep stored text fresh
# ---------------------------------------------------------------------------
async def edited_channel_post_handler(update, context) -> None:
    """Re-store a post when it is edited (e.g. VALIDITY added later)."""
    post = getattr(update, "edited_channel_post", None)
    if not post:
        return
    text = post.text or post.caption or ""
    if not text:
        return
    try:
        tags = bot.extract_hashtags(text)
        app_name = tags[0] if tags else text[:50]
        link = bot.build_message_link(post.chat.id, post.message_id)
        bot.store_post(post.message_id, post.chat.id, app_name, text, link)
        _invalidate_cache()
        logger.info("Edited post re-stored: %s (msg %s)",
                    app_name, post.message_id)
    except Exception as e:
        logger.error("Edited-post handling failed: %s", e)


async def _post_init(application):
    """Register the edited-post handler, then run the bot's own post_init."""
    try:
        application.add_handler(MessageHandler(
            filters.UpdateType.EDITED_CHANNEL_POST, edited_channel_post_handler))
        logger.info("Edited-channel-post handler registered")
    except Exception as e:
        logger.warning("Could not register edited-post handler: %s", e)
    await _orig_post_init(application)


# ---------------------------------------------------------------------------
# Periodic re-import (new + edited posts while the bot was asleep)
# ---------------------------------------------------------------------------
def _auto_import_loop():
    while True:
        time.sleep(AUTO_IMPORT_HOURS * 3600)
        try:
            logger.info("Auto-import: refreshing channel history...")
            asyncio.run(bot.auto_import_all_channels())
            _invalidate_cache()
            logger.info("Auto-import done")
        except Exception as e:
            logger.error("Auto-import failed: %s", e)


# ---------------------------------------------------------------------------
# Heuristic that keeps 2-letter app names like 'pw'
# ---------------------------------------------------------------------------
def extract_app_name_from_sentence(text: str) -> str:
    text = text.strip()
    tags = bot.extract_hashtags(text)
    if tags:
        return tags[0]

    low = text.lower().strip()
    for phrase in bot.NON_SEARCH_PHRASES:
        if phrase in low:
            return ""

    cleaned = re.sub(r"[^\w\s]", " ", text)
    words = cleaned.split()
    app_words = [w for w in words
                 if w.lower() not in bot.NOISE_WORDS
                 and w.lower() not in bot.QUICK_SKIP
                 and len(w) >= 2]
    if not app_words:
        return ""
    return " ".join(app_words[:3])


# ---------------------------------------------------------------------------
# Install patches and start
# ---------------------------------------------------------------------------
_orig_post_init = bot.post_init

bot.find_direct_matches = find_direct_matches
bot.fuzzy_search = fuzzy_search
bot.send_results = send_results
bot.find_app = find_app
bot.extract_app_name_from_sentence = extract_app_name_from_sentence
bot.post_init = _post_init

if __name__ == "__main__":
    logger.info("boot.py: matching + clean replies + edited posts + auto-import")
    threading.Thread(target=_auto_import_loop, daemon=True).start()
    bot.main()
