from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from constants import FXTWITTER_API_BASE, MAX_UPLOAD_BYTES, REQUEST_TIMEOUT
from models import MediaItem

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = "TelegramTweetExtractorBot/2.0"
_retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retries)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


@dataclass
class TweetPayload:
    tweet: dict[str, Any]
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    # Videos that could not fit Telegram's upload ceiling (even at lowest variant).
    # Each entry is (label, url, est_bytes) so caller can offer a direct link.
    oversized_videos: list[dict[str, Any]] = field(default_factory=list)

    def media_items(self) -> list[MediaItem]:
        return [MediaItem("photo", url) for url in self.photos] + [
            MediaItem("video", url) for url in self.videos
        ]


def _urls(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [item["url"] for item in items if isinstance(item, dict) and item.get("url")]


def _pick_best_fitting_video(video: dict[str, Any], max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[str | None, dict[str, Any] | None]:
    """Choose the highest-quality MP4 URL that fits Telegram's upload limit.

    FxTwitter provides multiple formats/variants per video, usually sorted from
    lowest bitrate/resolution to highest. We evaluate them from highest to lowest
    and pick the first that either:
      1) has an estimated size (bitrate * duration / 8) within max_bytes, OR
      2) has a verified Content-Length <= max_bytes (via a cheap HEAD request).

    Returns:
      (chosen_url, None) if a variant fits, OR
      (None, oversized_info) if all variants exceed max_bytes.
    """
    duration = float(video.get("duration") or 0.0)
    # Collect candidate MP4 variants with any bitrate/resolution hints
    raw_formats = video.get("formats") or []
    mp4_candidates: list[dict[str, Any]] = []

    for f in raw_formats:
        if not isinstance(f, dict):
            continue
        url = f.get("url") or ""
        container = (f.get("container") or "").lower()
        if not url or ("mp4" not in container and not url.lower().split("?")[0].endswith(".mp4")):
            continue
        bitrate = int(f.get("bitrate") or 0)
        mp4_candidates.append({"url": url, "bitrate": bitrate, "raw": f})

    # If `formats` had none, try `variants`
    if not mp4_candidates:
        for v in video.get("variants") or []:
            if not isinstance(v, dict):
                continue
            url = v.get("url") or ""
            ctype = (v.get("content_type") or "").lower()
            if not url or ("mp4" not in ctype and not url.lower().split("?")[0].endswith(".mp4")):
                continue
            bitrate = int(v.get("bitrate") or 0)
            mp4_candidates.append({"url": url, "bitrate": bitrate, "raw": v})

    # If neither list existed, fallback to video['url']
    if not mp4_candidates:
        main_url = video.get("url")
        return (main_url, None) if main_url else (None, None)

    # Sort descending by bitrate so we check the BEST quality first
    mp4_candidates.sort(key=lambda x: x["bitrate"], reverse=True)

    # Fast path: evaluate estimated size (bitrate in bps * duration in s / 8)
    # Include a 15% safety margin for audio/container overhead
    for cand in mp4_candidates:
        bitrate = cand["bitrate"]
        if bitrate > 0 and duration > 0:
            est_bytes = int((bitrate * duration / 8) * 1.15)
            if est_bytes <= max_bytes:
                log.info(
                    "Selected video variant: %s bps (~%.1f MB, dur=%.1fs, cap=%.1f MB)",
                    bitrate,
                    est_bytes / 1048576,
                    duration,
                    max_bytes / 1048576,
                )
                return cand["url"], None
        elif bitrate == 0:
            # No bitrate metadata — do a cheap HEAD check to know size
            try:
                head = _session.head(cand["url"], timeout=5, allow_redirects=True)
                cl = int(head.headers.get("Content-Length") or 0)
                if 0 < cl <= max_bytes:
                    return cand["url"], None
            except Exception:
                pass

    # If all candidates exceeded estimation, probe the lowest variant directly
    # with a HEAD request — sometimes estimation with 15% margin was overly conservative
    lowest = mp4_candidates[-1]
    try:
        head = _session.head(lowest["url"], timeout=5, allow_redirects=True)
        cl = int(head.headers.get("Content-Length") or 0)
        if 0 < cl <= max_bytes:
            return lowest["url"], None
    except Exception:
        cl = 0

    # Truly oversized even at lowest variant
    top_url = video.get("url") or mp4_candidates[0]["url"]
    return None, {
        "url": top_url,
        "duration": duration,
        "bitrate": mp4_candidates[0]["bitrate"],
    }


def get_tweet_payload(tweet_id: str, translate_to: str | None = None) -> TweetPayload | None:
    """Fetch a tweet via FxTwitter.

    Returns ``None`` if the tweet doesn't exist / isn't accessible. Network and
    server errors raise ``requests.RequestException``.
    """
    url = f"{FXTWITTER_API_BASE}/i/status/{tweet_id}"
    if translate_to:
        url = f"{url}/{translate_to}"

    response = _session.get(url, timeout=REQUEST_TIMEOUT)

    if response.status_code in (401, 403, 404):
        return None
    response.raise_for_status()

    data: dict[str, Any] = response.json()
    if data.get("code") != 200:
        return None

    tweet = data.get("tweet")
    if not isinstance(tweet, dict):
        return None

    media = tweet.get("media") or {}
    photos = _urls(media.get("photos"))

    videos: list[str] = []
    oversized: list[dict[str, Any]] = []

    for v in media.get("videos") or []:
        if not isinstance(v, dict):
            continue
        chosen_url, over_info = _pick_best_fitting_video(v)
        if chosen_url:
            videos.append(chosen_url)
        elif over_info:
            oversized.append(over_info)

    return TweetPayload(
        tweet=tweet,
        photos=photos,
        videos=videos,
        oversized_videos=oversized,
    )
