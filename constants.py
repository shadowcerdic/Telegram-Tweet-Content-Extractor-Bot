from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
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
