"""
Boot shim for MODAPPSKING Search Bot
====================================
Loads bot.py, installs improved app-matching, then starts the bot.

Why this exists
---------------
The bot answered only when Gemini was available — the free tier allows
just 20 requests/day, so the quota ran out fast and replies became
random ('kuch message par reply, kuch par nahi'). Its fuzzy match was
also too loose ('Paper' matched '4KWallpaper').

What this shim changes
----------------------
1. DIRECT MATCH (no AI, no quota): if a known app name appears in the
   message, reply immediately. This answers most real requests even
   when Gemini is rate-limited.
2. STRICT FUZZY MATCH: similarity-based (difflib >= 0.75) instead of
   naive substring matching, so wrong apps are no longer returned.
3. Heuristic fallback keeps 2-letter names like 'pw'.

Start command should be:  python boot.py
"""

import re
import sqlite3
import logging

import bot

logger = logging.getLogger("boot")

DB_PATH = bot.DB_PATH


# ---------------------------------------------------------------------------
# Direct matching (no AI)
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


_cache = {"count": -1, "items": []}


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
    """Known app names that literally appear in the message (no AI).

    Short names (<=3 chars) must match a whole word; longer names may
    sit inside a sentence.
    """
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
# Reply helper
# ---------------------------------------------------------------------------
async def send_results(update, results: list, query: str) -> None:
    if len(results) == 1:
        post = results[0]
        preview = (post["text"] or "")[:120]
        if len(post["text"] or "") > 120:
            preview += "..."
        reply = (f"📱 *{bot.escape(post['app_name'] or query)}*\n\n"
                 f"📝 {bot.escape(preview)}\n\n🔗 [Open Post]({post['link']})")
        await update.message.reply_text(
            reply, parse_mode="Markdown", disable_web_page_preview=False)
    else:
        lines = [f"📱 *Found {len(results)} matches for* `{bot.escape(query)}`:\n"]
        for i, post in enumerate(results, 1):
            preview = (post["text"] or "")[:60]
            if len(post["text"] or "") > 60:
                preview += "..."
            lines.append(
                f"{i}. *{bot.escape(post['app_name'] or 'Unknown')}*\n"
                f"   {bot.escape(preview)}\n   🔗 [Open]({post['link']})")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Improved find_app
# ---------------------------------------------------------------------------
async def find_app(update, context) -> None:
    if not update.message or not update.message.text:
        return

    message_text = update.message.text

    # 1) Direct match against known app names — no AI, no quota used.
    direct = find_direct_matches(message_text, limit=5)
    if direct:
        logger.info("Direct match: %s (from: '%s')",
                    direct[0]["app_name"], message_text)
        await send_results(update, direct, direct[0]["app_name"])
        return

    # 2) Not a known name — decide whether it looks like a request at all
    gemini_on = bot._gemini_available()
    if not bot.should_process_message(message_text, gemini_on):
        return

    # 3) AI extraction (spelling mistakes, Hinglish sentences)
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
bot.find_direct_matches = find_direct_matches
bot.fuzzy_search = fuzzy_search
bot.send_results = send_results
bot.find_app = find_app
bot.extract_app_name_from_sentence = extract_app_name_from_sentence

if __name__ == "__main__":
    logger.info("boot.py: improved matching installed — starting bot")
    bot.main()
