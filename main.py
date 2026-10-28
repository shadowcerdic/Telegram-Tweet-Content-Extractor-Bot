from __future__ import annotations

import logging
import signal
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

import constants
from constants import (
    ALLOWED_CHAT_TYPES,
    CAPTION_LIMIT,
    LOG_LEVEL,
    MAX_LINKS_PER_MESSAGE,
    OFFSET_FILE,
    POLL_INTERVAL,
    PROCESS_EDITED_MESSAGES,
    TRANSLATE_TO,
)
from downloader import download_media
from helpers import (
    build_quote_section,
    build_reply_text,
    chunk_media_items,
    chunk_text,
    extract_instagram_shortcodes,
    extract_tweet_ids,
    extract_youtube_ids,
    load_last_update_id,
    quote_author_label,
    save_last_update_id,
)
from instagram_client import MediaFetchError, downloaded_instagram_media, purge_temp_dir
from models import MediaItem
from telegram_api import (
    TelegramError,
    answer_callback_query,
    delete_message,
    edit_message_text,
    get_me,
    get_updates,
    send_audio,
    send_chat_action,
    send_media,
    send_media_group,
    send_message,
)
from x_client import get_tweet_payload
from youtube_client import (
    YoutubeError,
    YoutubeOversizedError,
    download_youtube_media,
    get_video_info,
)

log = logging.getLogger("bot")

_stop = False


# ---------------------------------------------------------------------------
# Per-message context and status message
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    """Where to send replies for one incoming message."""

    chat_id: int
    message_id: int | None
    thread_id: int | None = None  # only set for forum topics

    def target(self, reply: bool = True) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if reply and self.message_id is not None:
            kwargs["reply_to_message_id"] = self.message_id
        if self.thread_id is not None:
            kwargs["message_thread_id"] = self.thread_id
        return kwargs

    def action(self, name: str) -> None:
        """Send a 'typing' / 'uploading' indicator. Purely cosmetic."""
        try:
            send_chat_action(self.chat_id, name, self.thread_id)
        except TelegramError as exc:
            log.debug("chat action failed: %s", exc)


class Status:
    """A progress message that edits itself. All failures are non-fatal:
    a status message that can't be edited must never abort the real work."""

    def __init__(self, ctx: Ctx, text: str) -> None:
        self.ctx = ctx
        self.message_id: int | None = None
        self._text: str | None = None
        try:
            message = send_message(ctx.chat_id, text, **ctx.target())
            self.message_id = message["message_id"]
            self._text = text
        except (TelegramError, KeyError, TypeError) as exc:
            log.warning("could not send status message: %s", exc)

    def update(self, text: str) -> None:
        if self.message_id is None or text == self._text:
            return
        try:
            edit_message_text(self.ctx.chat_id, self.message_id, text)
            self._text = text
        except TelegramError as exc:
            log.debug("status update failed: %s", exc)

    def finish(self) -> None:
        if self.message_id is None:
            return
        try:
            delete_message(self.ctx.chat_id, self.message_id)
        except TelegramError:
            self.update("Done.")

    def error(self, text: str) -> None:
        if self.message_id is not None:
            try:
                edit_message_text(self.ctx.chat_id, self.message_id, text)
                return
            except TelegramError:
                pass
        try:
            send_message(self.ctx.chat_id, text, **self.ctx.target())
        except TelegramError as exc:
            log.warning("could not report error to chat: %s", exc)


# ---------------------------------------------------------------------------
# Sending text + media
# ---------------------------------------------------------------------------


def send_text(ctx: Ctx, text: str, reply: bool = True) -> None:
    for index, part in enumerate(chunk_text(text)):
        send_message(ctx.chat_id, part, **ctx.target(reply and index == 0))


def _send_chunk_once(
    ctx: Ctx, chunk: list[MediaItem], caption: str | None, reply: bool
) -> None:
    if len(chunk) == 1:
        send_media(ctx.chat_id, chunk[0], caption=caption, **ctx.target(reply))
    else:
        send_media_group(ctx.chat_id, chunk, caption=caption, **ctx.target(reply))


def send_chunk(ctx: Ctx, chunk: list[MediaItem], caption: str | None, reply: bool) -> None:
    ctx.action("upload_video" if any(i.kind == "video" for i in chunk) else "upload_photo")
    try:
        _send_chunk_once(ctx, chunk, caption, reply)
    except TelegramError as exc:
        # 400 = Telegram couldn't fetch the URL itself (too large, blocked, ...).
        # Download the files ourselves and upload them instead.
        if exc.error_code != 400 or not any(item.is_url for item in chunk):
            raise
        log.info("Telegram rejected media URL (%s); retrying as upload", exc.description)
        with tempfile.TemporaryDirectory(prefix="tgbot_") as tmp:
            local = [download_media(i, tmp) if i.is_url else i for i in chunk]
            _send_chunk_once(ctx, local, caption, reply)


def deliver(
    ctx: Ctx,
    items: list[MediaItem],
    text: str | None,
    extra_items: list[MediaItem] | None = None,
    extra_caption: str | None = None,
) -> None:
    """Send ``items`` with ``text``.

    The text is used as the caption when it fits Telegram's limit; otherwise it
    is sent first as normal message(s) and the media follows.

    ``extra_items`` is the media of a quoted post: it goes out after the outer
    tweet's media and wears ``extra_caption`` (the quoted author) on its first
    chunk, so it is always obvious which file came from which tweet. When the
    outer tweet has no media of its own, the full text rides on the first quote
    chunk instead — same single-message feel as Twitter.
    """
    text = (text or "").strip()
    items = list(items or [])
    extra_items = list(extra_items or [])

    if not items and not extra_items:
        if text:
            send_text(ctx, text)
        return

    reply_to_original = True
    text_caption = text if (text and len(text) <= CAPTION_LIMIT) else None
    if text and text_caption is None:
        send_text(ctx, text)
        reply_to_original = False

    pending_extra_caption = extra_caption
    for chunk, is_quote in [
        (c, False) for c in chunk_media_items(items)
    ] + [
        (c, True) for c in chunk_media_items(extra_items)
    ]:
        if not is_quote:
            caption, text_caption = text_caption, None
        else:
            caption, text_caption, pending_extra_caption = (
                text_caption or pending_extra_caption,
                None,
                None,
            )
        send_chunk(ctx, chunk, caption=caption, reply=reply_to_original)
        reply_to_original = False


# ---------------------------------------------------------------------------
# Link handlers
# ---------------------------------------------------------------------------


def _oversized_note(url: Any, label: str) -> str:
    return f"⚠️ {label} ({url}) بزرگتر از سقف ۵۰ مگابایت تلگرام است و مستقیماً ارسال نشد."


def handle_instagram(ctx: Ctx, shortcode: str, _siblings: frozenset[str] = frozenset()) -> None:
    status = Status(ctx, "Processing Instagram link...")
    try:
        status.update("Downloading media...")
        ctx.action("upload_document")

        with downloaded_instagram_media(shortcode) as (items, caption):
            if not items:
                status.error("Could not fetch that Instagram media.")
                return
            status.update("Sending media...")
            deliver(ctx, items, caption)

        status.finish()
    except MediaFetchError as exc:
        status.error(str(exc))
    except Exception:  # noqa: BLE001 - one bad link must not stop the bot
        log.exception("Instagram link failed: %s", shortcode)
        status.error("Failed to process Instagram link.")


def handle_tweet(ctx: Ctx, tweet_id: str, siblings: frozenset[str] = frozenset()) -> None:
    """Deliver a tweet, and the post it quotes (quote tweet) as a Twitter-style card."""
    status = Status(ctx, "Processing X link...")
    try:
        status.update("Fetching post content...")
        ctx.action("typing")

        payload = get_tweet_payload(tweet_id, translate_to=TRANSLATE_TO or None)
        if payload is None:
            log.info("Tweet %s: fetch returned nothing", tweet_id)
            status.error("Could not fetch that tweet text.")
            return

        # The translation backend occasionally fails transiently: the tweet comes
        # back fine but without the `translation` field, and the reply silently
        # loses its Persian part. Retry once after a short delay when that happens.
        def _has_translation(p: Any) -> bool:
            return bool((p.tweet.get("translation") or {}).get("text"))

        if TRANSLATE_TO and not _has_translation(payload):
            lang = (payload.tweet.get("lang") or "").strip().lower()
            if lang and lang != TRANSLATE_TO:
                log.info("Tweet %s: translation missing (lang=%r); retrying", tweet_id, lang)
                time.sleep(2)
                retry = get_tweet_payload(tweet_id, translate_to=TRANSLATE_TO or None)
                if retry is not None and _has_translation(retry):
                    payload = retry

        quote = payload.quote
        if quote is not None and str(quote.tweet.get("id") or "") in siblings:
            # The quoted post was pasted as its own link too, so it already gets a
            # full delivery of its own; rendering the card here would post it twice.
            log.info(
                "Tweet %s: quoted tweet %s is also linked directly; skipping inline card",
                tweet_id,
                quote.tweet.get("id"),
            )
            quote = None
            payload.quote_unavailable = None

        text = build_reply_text(payload.tweet) or ""
        text = (
            text
            + build_quote_section(quote.tweet if quote is not None else None, payload.quote_unavailable)
        ).strip()

        notes = [_oversized_note(ov.get("url", ""), "ویدیو") for ov in payload.oversized_videos]
        if quote is not None:
            notes += [
                _oversized_note(ov.get("url", ""), "ویدیوی توییت نقل‌قول‌شده")
                for ov in quote.oversized_videos
            ]
        if notes:
            text = (text + "\n\n" + "\n".join(notes)).strip()

        items = payload.media_items()
        quote_items = quote.media_items() if quote is not None else []
        quote_caption = (
            f"📎 پیوست توییت نقل‌قول‌شده — {quote_author_label(quote.tweet)}"
            if quote is not None
            else None
        )
        log.info(
            "Tweet %s: lang=%r translated=%r same_as_original=%r reply_has_translation=%r quote=%r",
            tweet_id,
            payload.tweet.get("lang"),
            (payload.tweet.get("translation") or {}).get("text", "")[:50],
            (payload.tweet.get("translation") or {}).get("text", "") == payload.tweet.get("text"),
            "ترجمه" in text,
            quote.tweet.get("id") if quote is not None else payload.quote_unavailable,
        )
        if not items and not quote_items and not text:
            status.error("Tweet content was empty.")
            return

        status.update("Sending content...")
        deliver(ctx, items, text, extra_items=quote_items, extra_caption=quote_caption)
        status.finish()
    except (requests.RequestException, TelegramError) as exc:
        log.warning("X link failed (%s): %s", tweet_id, exc)
        status.error("Failed to process X link.")
    except Exception:  # noqa: BLE001
        log.exception("X link failed: %s", tweet_id)
        status.error("Failed to process X link.")


def handle_youtube_prompt(
    ctx: Ctx, video_id: str, _siblings: frozenset[str] = frozenset()
) -> None:
    """Prompt the user with format choices when a YouTube link is detected."""
    ctx.action("typing")
    info = get_video_info(video_id)
    title = info.title if info else "YouTube Video"

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎬 ویدیو باکیفیت (1080p/720p)", "callback_data": f"yt:{video_id}:high"},
            ],
            [
                {"text": "📱 ویدیو کم‌حجم (480p/360p)", "callback_data": f"yt:{video_id}:low"},
            ],
            [
                {"text": "🎵 فقط صوت (MP3/M4A)", "callback_data": f"yt:{video_id}:audio"},
            ],
            [
                {"text": "❌ انصراف", "callback_data": f"yt:{video_id}:cancel"},
            ],
        ]
    }
    text = (
        f"📹 <b>{title}</b>\n\n"
        "لطفاً کیفیت یا فرمت مورد نظر برای دانلود را انتخاب کنید:"
    )
    send_message(
        ctx.chat_id,
        text,
        reply_to_message_id=ctx.message_id,
        message_thread_id=ctx.thread_id,
        reply_markup=keyboard,
    )


def handle_youtube_callback(callback_query: dict[str, Any]) -> None:
    cq_id = callback_query["id"]
    data = callback_query.get("data") or ""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None

    if not isinstance(chat_id, int) or not message_id:
        answer_callback_query(cq_id)
        return

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "yt":
        answer_callback_query(cq_id)
        return

    _, video_id, fmt_key = parts

    if fmt_key == "cancel":
        answer_callback_query(cq_id, "عملیات لغو شد.")
        delete_message(chat_id, message_id)
        return

    answer_callback_query(cq_id, "در حال شروع دانلود...")
    edit_message_text(chat_id, message_id, "⏳ در حال دانلود از یوتیوب... لطفاً شکیبا باشید.")

    ctx = Ctx(chat_id=chat_id, message_id=message_id, thread_id=thread_id)
    try:
        if fmt_key == "audio":
            ctx.action("upload_voice")
        else:
            ctx.action("upload_video")

        with download_youtube_media(video_id, fmt_key) as (file_path, title, media_type):
            edit_message_text(chat_id, message_id, "📤 در حال ارسال به تلگرام...")
            if media_type == "audio":
                send_audio(
                    chat_id,
                    file_path=file_path,
                    title=title,
                    caption=f"🎵 {title}",
                    message_thread_id=thread_id,
                )
            else:
                item = MediaItem(kind="video", source=file_path)
                send_media(
                    chat_id,
                    item=item,
                    caption=f"🎬 {title}",
                    message_thread_id=thread_id,
                )
        delete_message(chat_id, message_id)
    except YoutubeOversizedError as exc:
        edit_message_text(chat_id, message_id, f"⚠️ {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("YouTube download failed for %s (%s)", video_id, fmt_key)
        edit_message_text(chat_id, message_id, f"❌ خطا در پردازش یوتیوب: {exc}")


# ---------------------------------------------------------------------------
# Update handling
# ---------------------------------------------------------------------------


def get_message_text(message: dict[str, Any]) -> str:
    """Message text/caption, plus the targets of hidden links ("text_link")."""
    if message.get("text"):
        text, entities = message["text"], message.get("entities")
    else:
        text, entities = message.get("caption") or "", message.get("caption_entities")

    hidden = [
        e["url"] for e in entities or [] if e.get("type") == "text_link" and e.get("url")
    ]
    return "\n".join([text.strip(), *hidden]).strip()


def should_process_chat(chat_type: str | None) -> bool:
    return chat_type in ALLOWED_CHAT_TYPES


def process_message(message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or not should_process_chat(chat.get("type")):
        return
    if (message.get("from") or {}).get("is_bot"):
        return  # avoid bot-to-bot loops

    text = get_message_text(message)
    if not text:
        return

    ctx = Ctx(
        chat_id=chat_id,
        message_id=message.get("message_id"),
        thread_id=message.get("message_thread_id") if message.get("is_topic_message") else None,
    )

    tweet_ids = extract_tweet_ids(text)
    # Every tweet id in this message, so a handler can tell that a quoted post is
    # already being delivered on its own instead of posting it a second time.
    siblings = frozenset(tweet_ids)

    jobs: list[tuple[Callable[[Ctx, str, frozenset[str]], None], str]] = [
        (handle_instagram, code) for code in extract_instagram_shortcodes(text)
    ] + [
        (handle_tweet, tweet_id) for tweet_id in tweet_ids
    ] + [
        (handle_youtube_prompt, yt_id) for yt_id in extract_youtube_ids(text)
    ]

    for handler, link_id in jobs[:MAX_LINKS_PER_MESSAGE]:
        handler(ctx, link_id, siblings)


def process_update(update: dict[str, Any]) -> None:
    cq = update.get("callback_query")
    if cq:
        handle_youtube_callback(cq)
        return

    message = update.get("message")
    if message is None and PROCESS_EDITED_MESSAGES:
        message = update.get("edited_message")
    if message:
        process_message(message)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _RedactingFormatter(logging.Formatter):
    """Safety net: never let the bot token reach the logs."""

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        token = constants.BOT_TOKEN
        return output.replace(token, "***") if token else output


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=[handler])
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _request_stop(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    log.info("Signal %s received, shutting down after the current poll...", signum)


def main() -> None:
    configure_logging()

    if not constants.BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing in .env")

    # SIGTERM (systemd / Docker) stops gracefully; Ctrl+C interrupts immediately.
    signal.signal(signal.SIGTERM, _request_stop)

    purge_temp_dir()

    try:
        me = get_me()
    except TelegramError as exc:
        raise SystemExit(f"Cannot connect to Telegram: {exc}")
    log.info("Bot is running as @%s", me.get("username", "unknown"))

    try:
        run_loop()
    except KeyboardInterrupt:
        pass
    log.info("Bye.")


def run_loop() -> None:
    offset = load_last_update_id(OFFSET_FILE)
    failures = 0

    while not _stop:
        try:
            updates = get_updates(offset, allow_edited=PROCESS_EDITED_MESSAGES)
            failures = 0
        except Exception as exc:  # noqa: BLE001 - network hiccups, 409 conflicts, ...
            failures += 1
            delay = min(3 * 2 ** (failures - 1), 60)  # 3s, 6s, 12s ... max 60s
            log.warning("getUpdates failed (%s); retrying in %ss", exc, delay)
            time.sleep(delay)
            continue

        for update in updates:
            update_id = update["update_id"]
            log.info("Processing update_id=%s", update_id)
            try:
                process_update(update)
            except Exception:  # noqa: BLE001
                # Log and move on. The offset is advanced regardless, otherwise
                # one failing update would be re-fetched and re-fail forever.
                log.exception("Update %s failed", update_id)
            offset = update_id + 1
            save_last_update_id(OFFSET_FILE, offset)

        if not updates:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
