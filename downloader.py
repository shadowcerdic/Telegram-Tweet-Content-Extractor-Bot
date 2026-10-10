from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

import requests

from constants import MAX_UPLOAD_BYTES, REQUEST_TIMEOUT
from models import MediaItem

_session = requests.Session()
_session.headers["User-Agent"] = "TelegramTweetExtractorBot/2.0"

_DEFAULT_SUFFIX = {"photo": ".jpg", "video": ".mp4"}


class DownloadError(Exception):
    pass


def download_media(item: MediaItem, directory: str) -> MediaItem:
    """Download a remote media item into ``directory`` and return a local one.

    Used as a fallback when Telegram can't fetch a URL itself (its limit for
    URL uploads is 5 MB for photos and 20 MB for everything else, while direct
    uploads allow 50 MB).
    """
    suffix = os.path.splitext(urlparse(item.source).path)[1] or _DEFAULT_SUFFIX[item.kind]
    path = os.path.join(directory, f"{uuid.uuid4().hex}{suffix}")

    try:
        with _session.get(item.source, stream=True, timeout=REQUEST_TIMEOUT) as response:
            response.raise_for_status()

            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_UPLOAD_BYTES:
                raise DownloadError("Media is larger than Telegram's 50 MB upload limit")

            written = 0
            with open(path, "wb") as out:
                for block in response.iter_content(chunk_size=256 * 1024):
                    written += len(block)
                    if written > MAX_UPLOAD_BYTES:
                        raise DownloadError("Media is larger than Telegram's 50 MB upload limit")
                    out.write(block)
    except requests.RequestException as exc:
        raise DownloadError(f"Could not download media ({type(exc).__name__})") from exc

    return MediaItem(item.kind, path)
