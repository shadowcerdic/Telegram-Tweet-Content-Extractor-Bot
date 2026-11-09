from __future__ import annotations

import math
import os
import re
from typing import Any, Iterable, TypeVar

from constants import MAX_MESSAGE_LENGTH, MEDIA_GROUP_MAX, TRANSLATE_TO

T = TypeVar("T")

TWEET_LINK_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/(\d+)",
    re.IGNORECASE,
)

# Also matches the "instagram.com/<username>/reel/<code>" form that the
# Instagram share sheet produces.
INSTAGRAM_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/(?:[A-Za-z0-9_.]+/)?"
    r"(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Matches youtu.be/<id>, youtube.com/watch?v=<id>, and youtube.com/shorts/<id>
YOUTUBE_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/))([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def unique(items: Iterable[T]) -> list[T]:
    """De-duplicate while keeping the original order."""
    return list(dict.fromkeys(items))


def extract_youtube_ids(text: str) -> list[str]:
    if not text:
        return []
    return unique(YOUTUBE_LINK_RE.findall(text))


def extract_instagram_shortcodes(text: str) -> list[str]:
    if not text:
        return []
    return unique(INSTAGRAM_LINK_RE.findall(text))


def extract_tweet_ids(text: str) -> list[str]:
    if not text:
        return []
    return unique(TWEET_LINK_RE.findall(text))


def normalize_tweet_text(text: str | None) -> str:
    if not text:
        return ""
    return text.strip()


def _author_signature(author: Any) -> str:
    if not isinstance(author, dict):
        return ""
    name = (author.get("name") or "").strip()
    url = (author.get("url") or "").strip()
    parts = []
    if name:
        parts.append(f"— {name}")
    if url:
        parts.append(f"Profile: {url}")
    return "\n\n" + "\n\n".join(parts) if parts else ""


def _tweet_body(tweet: dict[str, Any]) -> str:
    """Tweet text, plus the Persian translation when there is a real one."""
    original = normalize_tweet_text(tweet.get("text"))
    if not original:
        return ""

    lang = (tweet.get("lang") or "").strip().lower()
    translation = tweet.get("translation")
    translated = (
        normalize_tweet_text(translation.get("text"))
        if isinstance(translation, dict)
        else ""
    )

    if not translated or translated == original:
        # Nothing came back from the translation layers (fxtwitter drops certain
        # tweet/language pairs outright). Name that instead of quietly posting
        # only the source text, which reads as the bot skipping its job.
        if TRANSLATE_TO and lang and lang != TRANSLATE_TO:
            return f"متن اصلی (ترجمه در دسترس نبود):\n\n{original}"
        return original

    label = "ترجمه فارسی" if TRANSLATE_TO == "fa" else f"Translation ({TRANSLATE_TO})"
    return f"Original:\n\n{original}\n\n{label}:\n\n{translated}"


def build_reply_text(tweet: dict[str, Any]) -> str | None:
    """Text for the Telegram reply: tweet text (+ translation) and author."""
    body = _tweet_body(tweet)
    if not body:
        return None
    return body + _author_signature(tweet.get("author"))


def quote_author_label(tweet: dict[str, Any] | None) -> str:
    """"Full Name (@handle)" for a quoted post, tolerating a missing author."""
    data = tweet if isinstance(tweet, dict) else {}
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    name = (author.get("name") or "").strip()
    screen = (author.get("screen_name") or "").strip()
    if name and screen:
        return f"{name} (@{screen})"
    return name or screen or "کاربر دیگر"


def build_quote_section(
    quote_tweet: dict[str, Any] | None, unavailable: str | None = None
) -> str:
    """The quoted-post block, rendered under the outer tweet like Twitter's card.

    Returns "" when the tweet is not a quote post, so callers can append it
    unconditionally.
    """
    if unavailable:
        return f"\n\n┄┄┄┄┄┄┄┄┄┄┄┄\n↩️ توییت نقل‌قول‌شده در دسترس نیست ({unavailable})."
    if not isinstance(quote_tweet, dict):
        return ""

    body = _tweet_body(quote_tweet)
    if not body:
        return ""

    parts = ["┄┄┄┄┄┄┄┄┄┄┄┄", f"↩️ نقل‌قول از {quote_author_label(quote_tweet)}:", "", body]
    url = (quote_tweet.get("url") or "").strip()
    if url:
        parts += ["", f"🔗 {url}"]
    return "\n\n" + "\n".join(parts)


PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
THREAD_ITEM_SEPARATOR = "┄" * 20
_UNAVAILABLE_LABELS = {
    "deleted": "حذف شده",
    "suspended": "حساب معلق است",
    "private": "حساب خصوصی است",
    "blocked": "حساب مسدود است",
    "unavailable": "در دسترس نیست",
}


def fa_number(value: Any) -> str:
    """ASCII digits -> Persian digits, so counters read as Persian text."""
    text = str(value)
    return "".join(PERSIAN_DIGITS[int(ch)] if ch.isdigit() and int(ch) <= 9 else ch for ch in text)


def unavailable_label(reason: str | None) -> str:
    """A Persian explanation for an X tombstone reason, falling back to the raw one."""
    key = (reason or "").strip().lower()
    if not key:
        return "در دسترس نیست"
    return _UNAVAILABLE_LABELS.get(key, f"در دسترس نیست ({key})")


def _thread_item_body(item: Any, index: int, total: int, focal_id: str | None) -> str:
    """One thread position: counter, text (+translation), optional quote card, link."""
    counter = f"{THREAD_ITEM_SEPARATOR}\n{fa_number(index)}/{fa_number(total)}"
    if focal_id and str(getattr(item, "item_id", "") or "") == str(focal_id):
        counter += " 👈 توییتی که فرستادید"

    payload = getattr(item, "payload", None)
    if payload is None:
        return f"{counter}\n\n❌ توییت {unavailable_label(getattr(item, 'unavailable', None))}."

    tweet = payload.tweet if isinstance(payload.tweet, dict) else {}
    body = _tweet_body(tweet)
    if not body:
        # A media-only item still has a position in the chain; saying so keeps the
        # counters honest instead of letting a gap look like a lost tweet.
        body = "📎 (بدون متن — پیوست‌ها در ادامه ارسال می‌شوند)"
    parts = [counter, "", body]

    quote_tweet = getattr(payload, "quote", None)
    unavailable = getattr(payload, "quote_unavailable", None)
    quote_section = build_quote_section(
        quote_tweet.tweet if quote_tweet is not None else None, unavailable
    )
    if quote_section:
        parts.append(quote_section)

    url = (tweet.get("url") or "").strip()
    if url:
        parts += ["", f"🔗 {url}"]
    return "\n".join(parts)


def _thread_root_url(result: Any) -> str:
    """The link a reader needs to open to reach the rest of the thread."""
    for candidate in (getattr(result, "root", None), *(getattr(result, "items", None) or [])):
        payload = getattr(candidate, "payload", None)
        if payload is None:
            continue
        url = (payload.tweet.get("url") or "").strip() if isinstance(payload.tweet, dict) else ""
        if url:
            return url
    return ""


def _thread_continuation_url(result: Any) -> str:
    """Open-where-we-stopped link for a truncated thread.

    The root link only shows the reader the part they already got, so the note
    points at the first item the cap dropped. A dropped item carries no payload
    (only its id and author), so build the link from those and fall back to root.
    """
    for item in list(getattr(result, "dropped", None) or []):
        payload = getattr(item, "payload", None)
        if payload is not None and isinstance(payload.tweet, dict):
            url = (payload.tweet.get("url") or "").strip()
            if url:
                return url
            author = payload.tweet.get("author")
            if isinstance(author, dict):
                handle = (author.get("screen_name") or "").strip()
            else:
                handle = ""
        else:
            handle = (getattr(item, "author_screen_name", "") or "").strip()
        if handle and getattr(item, "item_id", ""):
            return f"https://x.com/{handle}/status/{item.item_id}"
    return _thread_root_url(result)


def build_thread_text(result: Any) -> str | None:
    """Render a whole thread as one continuous Persian message.

    ``result`` is an :class:`x_client.ThreadResult`. Items keep their chain
    position (a deleted tweet is labelled, not dropped), each carries its own
    link, and the message ends with a byline plus a continuation note when the
    thread was longer than the cap or the linked tweet fell outside the delivered
    slice. Returns None when there is nothing renderable.
    """
    items = list(getattr(result, "items", None) or [])
    if not items:
        return None

    focal_id = str(getattr(result, "requested_id", "") or "")
    shown = len(items)
    total = max(int(getattr(result, "total", 0) or 0), shown)

    blocks = [
        _thread_item_body(item, index, shown, focal_id)
        for index, item in enumerate(items, start=1)
    ]

    author = ""
    first = items[0]
    if first.payload is not None and isinstance(first.payload.tweet.get("author"), dict):
        author = (first.payload.tweet["author"].get("screen_name") or "").strip()

    # "۲ توییت از ۵ توییت" reads like a machine join; "۲ از ۵ توییت" is what a
    # Persian writer would have typed, and it keeps the separator count at one.
    if total > shown:
        byline = f"🧵 ترد {fa_number(shown)} از {fa_number(total)} توییت"
    else:
        byline = f"🧵 ترد {fa_number(shown)} توییت"
    if author:
        byline = f"{byline} — @{author}"

    notes: list[str] = []
    if total > shown:
        root_url = _thread_continuation_url(result)
        tail = f"ادامه: {root_url}" if root_url else "برای ادامه، لینک ترد را باز کنید"
        notes.append(
            f"⚠️ {fa_number(total - shown)} توییت دیگر در این ترد بود و ارسال نشد؛ {tail}"
        )
    delivered_ids = {str(getattr(item, "item_id", "") or "") for item in items}
    if focal_id and focal_id not in delivered_ids:
        notes.append(f"👈 توییت درخواستی شما در {fa_number(shown)} توییت اول این ترد نبود.")

    text = "\n\n".join([*blocks, byline, *notes]).strip()
    return text or None


def _split_long_line(line: str, max_length: int) -> list[str]:
    """Split a single over-long line, preferring to break at whitespace."""
    pieces: list[str] = []
    while len(line) > max_length:
        cut = line.rfind(" ", 0, max_length + 1)
        if cut <= 0:
            cut = max_length
        pieces.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    if line:
        pieces.append(line)
    return pieces


def chunk_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= max_length:
            current += line
            continue

        if current.strip():
            chunks.append(current.rstrip())
        current = ""

        if len(line) > max_length:
            *full, current = _split_long_line(line, max_length) or [""]
            chunks.extend(full)
        else:
            current = line

    if current.strip():
        chunks.append(current.rstrip())

    return [chunk for chunk in chunks if chunk]


def chunk_media_items(items: list[T], max_size: int = MEDIA_GROUP_MAX) -> list[list[T]]:
    """Split into albums of near-equal size.

    Telegram albums need 2-10 items, so a naive split of 11 items (10 + 1) would
    leave a single-item album. Splitting evenly (6 + 5) avoids that.
    """
    if not items:
        return []
    parts = math.ceil(len(items) / max_size)
    size = math.ceil(len(items) / parts)
    return [items[i : i + size] for i in range(0, len(items), size)]


def load_last_update_id(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = file.read().strip()
        return int(value) if value else None
    except (FileNotFoundError, ValueError):
        return None


def save_last_update_id(path: str, update_id: int) -> None:
    """Write atomically so a crash can never leave a half-written offset file."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        file.write(str(update_id))
    os.replace(tmp_path, path)
