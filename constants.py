from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --- Configuration (from .env) -------------------------------------------------
# The token is validated in main() rather than at import time, so the modules
# can be imported (e.g. by unit tests) without a real token.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
POLL_INTERVAL = _float_env("POLL_INTERVAL", 1.0)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

# Target language for tweet translation (FxTwitter language code).
# Set TRANSLATE_TO= (empty) to disable translation entirely.
_translate = os.getenv("TRANSLATE_TO")
TRANSLATE_TO = "fa" if _translate is None else _translate.strip().lower()

# Edited messages are ignored by default; otherwise every edit of a message that
# contains a link would make the bot reply again.
PROCESS_EDITED_MESSAGES = _bool_env("PROCESS_EDITED_MESSAGES", False)

# Optional logged-in Instagram session (recommended on servers / VPS).
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "").strip()
INSTAGRAM_SESSION_FILE = os.getenv("INSTAGRAM_SESSION_FILE", "").strip()

# Second translation layer when FxTwitter's own translate backend drops the
# (tweet, language) pair: "google" = keyless web endpoint, "off" = never call it.
TRANSLATE_FALLBACK = os.getenv("TRANSLATE_FALLBACK", "google").strip().lower()

# Extra FxTwitter translate lookups when the first answer carries no
# ``translation``. The old rule ("note tweets fail deterministically, so a
# re-fetch is wasted motion") was measured on ONE 1032-char note; a second
# case on 2026-11-09 proved the opposite for that shape: a 2956-char note
# (id 2119479031611507061 -> fa) came back empty on the first attempt and
# translated on all six following attempts. Notes therefore get MORE attempts
# than short tweets, not zero, and only fall back after they are exhausted.
TRANSLATE_REFETCH_TRIES = int(os.getenv("TRANSLATE_REFETCH_TRIES", "1"))
TRANSLATE_REFETCH_TRIES_NOTE = int(os.getenv("TRANSLATE_REFETCH_TRIES_NOTE", "2"))
# Seconds of exponential wait before each retry (0 disables it).
TRANSLATE_REFETCH_BACKOFF_S = float(os.getenv("TRANSLATE_REFETCH_BACKOFF_S", "0.4"))

# --- X threads (a tweet that is part of a self-reply chain) -------------------
# FxTwitter exposes the whole chain at GET /2/thread/{id}, from any link inside
# it. Delivering an unbounded thread would flood a chat, so the number of items
# rendered is capped; the rest is announced with a link to the continuation.
THREAD_MAX_TWEETS = int(os.getenv("THREAD_MAX_TWEETS", "20"))
# A thread probe doubles as a translation source: /2/thread/{id}?lang=xx returns
# inline ``translation`` for items X could translate. When the focal tweet came
# back without one, the (id, lang) pair is often flaky rather than dead, so a
# thread item that still has no translation gets this many plain refetches
# before the keyless fallback — fewer than a lone tweet gets, because the cost
# multiplies by the thread length.
THREAD_TRANSLATE_REFETCH_TRIES = int(os.getenv("THREAD_TRANSLATE_REFETCH_TRIES", "1"))
# Wall-clock budget for ONE thread delivery. Every upstream call carries its own
# REQUEST_TIMEOUT, so a thread whose items all need a translation retry could
# otherwise burn tens of them: 20 items x 2 tries x 30s is not a download, it is a
# hang. When the budget runs out the thread is cut short and announced.
THREAD_TIME_BUDGET_S = float(os.getenv("THREAD_TIME_BUDGET_S", "60"))
# Kill switch: "off" restores the pre-thread behaviour (focal tweet only) without
# a deploy, in case the thread endpoint starts misbehaving upstream.
THREAD_FETCH = os.getenv("THREAD_FETCH", "on").strip().lower()

# YouTube downloader proxy (Cloudflare WARP default on VPS to avoid datacenter IP bot detection)
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "socks5://127.0.0.1:40000").strip()

# --- Fixed settings ------------------------------------------------------------
TELEGRAM_API_ROOT = "https://api.telegram.org"
OFFSET_FILE = os.getenv("OFFSET_FILE", "offset.txt")
ALLOWED_CHAT_TYPES = {"private", "group", "supergroup"}
FXTWITTER_API_BASE = "https://api.fxtwitter.com"
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
# The keyless web endpoint rejects very long `q=` values; blank-line-aware
# chunking keeps every request under this many characters.
TRANSLATE_CHUNK_CHARS = 4500

REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 120
LONG_POLL_TIMEOUT = 30

MAX_MESSAGE_LENGTH = 4000  # Telegram hard limit is 4096
CAPTION_LIMIT = 1024  # Telegram caption limit
MEDIA_GROUP_MAX = 10  # Telegram album limit
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # Bot API upload limit
MAX_LINKS_PER_MESSAGE = 5  # protects against link-spam in groups
WORKER_THREADS = _int_env("WORKER_THREADS", 3)  # concurrent update workers
