from __future__ import annotations

import html
import math
import os
import re
from typing import Any, Iterable, TypeVar

import constants
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


# ---------------------------------------------------------------------------
# Clean (RICH_LAYOUT) rendering
# ---------------------------------------------------------------------------

THREAD_RICH_SEPARATOR = "──"
QUOTE_RICH_SEPARATOR = "──────────────"
ORIGINAL_RICH_LABEL = "<b>متن اصلی:</b>"


def _rich() -> bool:
    """Read the knob at call time: .env changes and mock.patch both take effect."""
    return bool(constants.RICH_LAYOUT)


def _keep_original() -> bool:
    return bool(constants.SHOW_ORIGINAL)


def translation_pair(tweet: dict[str, Any]) -> tuple[str, str]:
    """(source text, real translation) — an echo of the source counts as none."""
    original = normalize_tweet_text(tweet.get("text"))
    translation = tweet.get("translation")
    translated = (
        normalize_tweet_text(translation.get("text"))
        if isinstance(translation, dict)
        else ""
    )
    if translated == original:
        translated = ""
    return original, translated


def esc(text: str) -> str:
    """Escape upstream text for HTML.

    Every character in a tweet is attacker-shaped data: one unescaped ``<`` or
    ``&`` makes Telegram answer 400 for the whole message, so nothing reaches
    the output unescaped when ``parse_mode`` is set.
    """
    return html.escape(text or "", quote=False)


def rich_byline(author: Any) -> str:
    """One short HTML line: bold name plus a linked handle.

    Tags live only on short standalone lines so ``chunk_text`` (which splits on
    line boundaries) can never cut an opened tag in half.
    """
    data = author if isinstance(author, dict) else {}
    name = (data.get("name") or "").strip()
    screen = (data.get("screen_name") or "").strip()
    url = (data.get("url") or "").strip()

    handle = ""
    if screen:
        handle = f"@{esc(screen)}"
        if url.startswith("https://"):
            handle = f'<a href="{html.escape(url, quote=True)}">{handle}</a>'

    if name and handle:
        return f"<b>{esc(name)}</b> {handle}"
    label = esc(name) if name else handle
    return f"<b>{label}</b>" if label else ""


def _tweet_body_rich(tweet: dict[str, Any], include_original: bool | None = None) -> str:
    """Translation first, the source text only when asked to keep it."""
    original, translated = translation_pair(tweet)
    if not original and not translated:
        return ""

    keep = _keep_original() if include_original is None else include_original

    if not translated:
        lang = (tweet.get("lang") or "").strip().lower()
        if TRANSLATE_TO and lang and lang != TRANSLATE_TO:
            # Same honesty rule as the plain layout: say the layer failed.
            return f"متن اصلی (ترجمه در دسترس نبود):\n\n{esc(original)}"
        return esc(original)

    parts = [esc(translated)]
    if keep and original:
        parts += [ORIGINAL_RICH_LABEL, esc(original)]
    return "\n\n".join(parts)


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


def _tweet_body(tweet: dict[str, Any], include_original: bool | None = None) -> str:
    """Tweet text, plus the Persian translation when there is a real one."""
    if _rich():
        return _tweet_body_rich(tweet, include_original)

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
    if _rich():
        byline = rich_byline(tweet.get("author"))
        return f"{byline}\n\n{body}" if byline else body
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
        if _rich():
            return (
                f"\n\n{QUOTE_RICH_SEPARATOR}\n"
                f"↩️ توییت نقل‌قول‌شده در دسترس نیست ({unavailable})."
            )
        return f"\n\n┄┄┄┄┄┄┄┄┄┄┄┄\n↩️ توییت نقل‌قول‌شده در دسترس نیست ({unavailable})."
    if not isinstance(quote_tweet, dict):
        return ""

    # A quote card is a summary: its own source text would double the block.
    body = _tweet_body(quote_tweet, include_original=False)
    if not body:
        return ""

    if _rich():
        head = rich_byline(quote_tweet.get("author"))
        lines = [QUOTE_RICH_SEPARATOR]
        if head:
            lines.append(f"↩️ نقل‌قول از {head}")
        lines += ["", body]
        return "\n\n" + "\n".join(lines)

    parts = ["┄┄┄┄┄┄┄┄┄┄┄┄", f"↩️ نقل‌قول از {quote_author_label(quote_tweet)}:", "", body]
    url = (quote_tweet.get("url") or "").strip()
    if url:
        parts += ["", f"🔗 {url}"]
    return "\n\n" + "\n".join(parts)


def source_link_tail(tweet: dict[str, Any], text: str) -> str:
    """The linked tweet's own URL as a delivery-level tail line.

    DELETE_SOURCE_LINK removes the message that carried the link, so the
    delivery itself must keep the source reachable. The handler appends this
    AFTER all composition (body, quote card, oversized notes) — never inside
    the body renderers, whose bytes are pinned by contract tests.

    Returns "" when the payload has no url, or the url is already VISIBLE in
    the text: tags are stripped first (a rich continuation anchor hides its
    url behind a label, which is tappable but not copyable) and entities
    unescaped. The plain layout's per-item 🔗 lines therefore dedupe, while a
    hidden href never suppresses the visible tail.
    """
    url = (tweet.get("url") or "").strip() if isinstance(tweet, dict) else ""
    if not url:
        return ""
    visible = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    if url in visible:
        return ""
    if _rich():
        # HTML mode does not auto-link bare URLs: the visible URL text itself
        # rides on the anchor, so it stays tappable AND copyable.
        return f'🔗 <a href="{html.escape(url, quote=True)}">{esc(url)}</a>'
    return f"🔗 {url}"


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


def _thread_item_body_rich(item: Any, index: int, focal_id: str | None, show_author: bool) -> str:
    """One thread position in the clean layout: counter, optional author, text.

    No per-item ``🔗 https://x.com/...`` line: the handle in the author line is
    already the link, and repeating a raw URL 20 times is the noise this layout
    exists to remove.
    """
    label = f"{THREAD_RICH_SEPARATOR} {fa_number(index)} {THREAD_RICH_SEPARATOR}"
    if focal_id and str(getattr(item, "item_id", "") or "") == str(focal_id):
        label += "  پیام شما"

    payload = getattr(item, "payload", None)
    if payload is None:
        return f"{label}\n\n❌ توییت {unavailable_label(getattr(item, 'unavailable', None))}."

    tweet = payload.tweet if isinstance(payload.tweet, dict) else {}
    body = _tweet_body(tweet)
    if not body:
        body = "📎 (بدون متن — پیوست‌ها در ادامه ارسال می‌شوند)"

    lines = [label]
    if show_author:
        byline = rich_byline(tweet.get("author"))
        if byline:
            lines.append(byline)
    lines += ["", body]

    quote_tweet = getattr(payload, "quote", None)
    unavailable = getattr(payload, "quote_unavailable", None)
    quote_section = build_quote_section(
        quote_tweet.tweet if quote_tweet is not None else None, unavailable
    )
    if quote_section:
        lines.append(quote_section.lstrip("\n"))
    return "\n".join(lines)


def _author_key(item: Any) -> str:
    """What identifies 'the same author' between two thread positions."""
    payload = getattr(item, "payload", None)
    tweet = getattr(payload, "tweet", None) if payload is not None else None
    if not isinstance(tweet, dict):
        return str(getattr(item, "item_id", "") or "")
    author = tweet.get("author")
    if not isinstance(author, dict):
        return ""
    return (author.get("screen_name") or author.get("name") or "").strip().lower()


def _thread_item_body(item: Any, index: int, total: int, focal_id: str | None,
                      show_author: bool = True) -> str:
    """One thread position: counter, text (+translation), optional quote card, link."""
    if _rich():
        return _thread_item_body_rich(item, index, focal_id, show_author)

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

    if _rich():
        # A thread is usually one author: repeating the byline per item is noise,
        # so it appears on the first item and whenever the author changes.
        blocks = []
        previous_author = object()
        for index, item in enumerate(items, start=1):
            key = _author_key(item)
            show_author = index == 1 or key != previous_author
            previous_author = key
            blocks.append(_thread_item_body(item, index, shown, focal_id, show_author))
    else:
        blocks = [
            _thread_item_body(item, index, shown, focal_id)
            for index, item in enumerate(items, start=1)
        ]

    author = ""
    author_url = ""
    first = items[0]
    if first.payload is not None and isinstance(first.payload.tweet.get("author"), dict):
        first_author = first.payload.tweet["author"]
        author = (first_author.get("screen_name") or "").strip()
        author_url = (first_author.get("url") or "").strip()

    # "۲ توییت از ۵ توییت" reads like a machine join; "۲ از ۵ توییت" is what a
    # Persian writer would have typed, and it keeps the separator count at one.
    if total > shown:
        byline = f"🧵 ترد {fa_number(shown)} از {fa_number(total)} توییت"
    else:
        byline = f"🧵 ترد {fa_number(shown)} توییت"
    if _rich():
        handle = ""
        if author:
            handle = f"@{html.escape(author, quote=False)}"
            if author_url.startswith("https://"):
                handle = f'<a href="{html.escape(author_url, quote=True)}">{handle}</a>'
        title = byline.split(" ", 1)[1].strip() if byline.startswith("🧵") else byline
        byline = f"🧵 <b>{title}</b> {handle}".rstrip()
    elif author:
        byline = f"{byline} — @{author}"

    notes: list[str] = []
    if total > shown:
        root_url = _thread_continuation_url(result)
        if _rich() and root_url:
            tail = f'<a href="{html.escape(root_url, quote=True)}">ادامه را باز کنید</a>'
        elif root_url:
            tail = f"ادامه: {root_url}"
        else:
            tail = "برای ادامه، لینک ترد را باز کنید"
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
