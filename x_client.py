from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from constants import FXTWITTER_API_BASE, REQUEST_TIMEOUT
from models import MediaItem

_session = requests.Session()
_session.headers["User-Agent"] = "TelegramTweetExtractorBot/2.0"


@dataclass
class TweetPayload:
    tweet: dict[str, Any]
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)

    def media_items(self) -> list[MediaItem]:
        return [MediaItem("photo", url) for url in self.photos] + [
            MediaItem("video", url) for url in self.videos
        ]


def _urls(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [item["url"] for item in items if isinstance(item, dict) and item.get("url")]


def get_tweet_payload(tweet_id: str, translate_to: str | None = None) -> TweetPayload | None:
    """Fetch a tweet via FxTwitter.

    Returns ``None`` if the tweet doesn't exist / isn't accessible. Network and
    server errors raise ``requests.RequestException``.
    """
    # FixTweet docs say screen_name is ignored, so /i/status/:id is fine.
    url = f"{FXTWITTER_API_BASE}/i/status/{tweet_id}"
    if translate_to:
        url = f"{url}/{translate_to}"

    response = _session.get(url, timeout=REQUEST_TIMEOUT)

    # Deleted / private / suspended: a normal outcome, not an error.
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
    return TweetPayload(
        tweet=tweet,
        photos=_urls(media.get("photos")),
        videos=_urls(media.get("videos")),
    )
