from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

import translator
from constants import (
    FXTWITTER_API_BASE,
    MAX_UPLOAD_BYTES,
    REQUEST_TIMEOUT,
    TRANSLATE_FALLBACK,
)
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
    # Quote post (retweet with comment): the quoted tweet parsed with the exact
    # same media rules. None when the tweet is not a quote post.
    quote: "TweetPayload | None" = None
    # When X reports the quoted post as unavailable, the tombstone reason
    # ("deleted", "protected", ...) instead of silently dropping it.
    quote_unavailable: str | None = None

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


def _extract_media(tweet: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """(photos, fitting videos, oversized videos) for one tweet object."""
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

    return photos, videos, oversized


def _build_payload(tweet: dict[str, Any]) -> TweetPayload:
    photos, videos, oversized = _extract_media(tweet)
    return TweetPayload(
        tweet=tweet,
        photos=photos,
        videos=videos,
        oversized_videos=oversized,
    )


def _fetch_tweet_raw(tweet_id: str, translate_to: str | None = None) -> dict[str, Any] | None:
    """One FxTwitter lookup -> the raw tweet object, or None when unavailable.

    Network/server errors raise ``requests.RequestException`` — callers decide how
    loudly to fail.
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
    return tweet if isinstance(tweet, dict) else None


def _needs_translation(tweet: dict[str, Any], translate_to: str | None) -> bool:
    if not translate_to:
        return False
    if not (tweet.get("text") or "").strip():
        return False
    lang = (tweet.get("lang") or "").strip().lower()
    if lang == translate_to:
        return False
    return not (tweet.get("translation") or {}).get("text")


def _scrub_echo(tweet: dict[str, Any], translate_to: str) -> None:
    """Clear an upstream ``translation`` that is just the source text repeated.

    Measured live on a quoted Japanese tweet (id 2114674323822883285, target
    ``fa``): FxTwitter answered ``provider="grok"`` with ``translation.text``
    byte-identical to ``tweet.text``. Presence of the field is therefore not
    proof of a translation; left alone it satisfies every "do we have one?" check
    while the user still sees untranslated Japanese. Clearing it here makes the
    recovery path below treat the tweet as untranslated.
    """
    trans = tweet.get("translation") or {}
    text = (trans.get("text") or "").strip()
    if not text:
        return
    if (tweet.get("lang") or "").strip().lower() == translate_to:
        return
    if text == (tweet.get("text") or "").strip():
        log.info(
            "Tweet %s: fxtwitter echoed the source text instead of translating to %s",
            tweet.get("id"),
            translate_to,
        )
        tweet["translation"] = None


def _ensure_translation(tweet: dict[str, Any], translate_to: str | None) -> None:
    """Fill ``tweet["translation"]`` for the three known FxTwitter failure modes.

    0. echo — ``translation.text`` equals the original (see ``_scrub_echo``), so
       the pair silently failed even though the field is populated.
    1. transient — the translate backend sometimes answers without a
       ``translation`` field on a short tweet; one re-fetch recovers it.
    2. deterministic — specific (tweet, target-language) pairs always come back
       untranslated. Measured on a 1032-char note tweet: ``fa``/``de``/``hi``
       empty on six consecutive attempts while ``ar``/``tr``/``es``/``fr``/
       ``ja``/``pt``/``ru``/``ko``/``zh-CN`` all succeeded for the same tweet.
       Long-form (note) tweets are the reliable signature, so those skip the
       pointless re-fetch and go straight to the keyless Google endpoint.

    Mutates ``tweet``; never raises. When no layer produces a real translation
    the text stays as-is and helpers render "ترجمه در دسترس نبود" — silence would
    look like the bot skipped the work.
    """
    if translate_to:
        _scrub_echo(tweet, translate_to)
    if TRANSLATE_FALLBACK == "off" or not translate_to:
        return
    if not _needs_translation(tweet, translate_to):
        return

    lang = (tweet.get("lang") or "").strip().lower()
    tweet_id = str(tweet.get("id") or "")

    if tweet_id and not tweet.get("is_note_tweet"):
        try:
            again = _fetch_tweet_raw(tweet_id, translate_to)
        except requests.RequestException as exc:
            log.info("translate retry for %s failed: %s", tweet_id, exc)
            again = None
        if again and (again.get("translation") or {}).get("text"):
            tweet["translation"] = again["translation"]
            _scrub_echo(tweet, translate_to)
            if not _needs_translation(tweet, translate_to):
                return

    translated = translator.translate(tweet.get("text") or "", translate_to)
    if translated:
        tweet["translation"] = {
            "text": translated,
            "source_lang": lang,
            "source_lang_en": lang,
            "target_lang": translate_to,
            "provider": "google-fallback",
        }
        log.info("Tweet %s: translated by the fallback (fxtwitter returned none)", tweet_id)
    else:
        log.info("Tweet %s: no translation from fxtwitter or the fallback (lang=%r)", tweet_id, lang)


def _parse_quote(raw: Any, translate_to: str | None) -> tuple[TweetPayload | None, str | None]:
    """Resolve ``tweet["quote"]`` into (payload, unavailable_reason).

    FxTwitter embeds the quoted tweet with full media (including per-variant
    bitrates, so the 50 MB picker works unchanged on it) but WITHOUT a
    ``translation`` — the translate endpoint only translates the focal tweet. So
    the nested object is parsed as-is and then run through the same translation
    layers as a top-level tweet.

    Depth is capped at 1: a quote inside the quoted post is not rendered.
    """
    if not isinstance(raw, dict):
        return None, None

    if (raw.get("type") or "").lower() == "tombstone":
        return None, str(raw.get("reason") or "unavailable")

    quote_id = str(raw.get("id") or "")
    if not quote_id or not (raw.get("text") or raw.get("media")):
        return None, None

    payload = _build_payload(raw)
    _ensure_translation(payload.tweet, translate_to)
    payload.quote = None
    payload.quote_unavailable = None
    return payload, None


def get_tweet_payload(tweet_id: str, translate_to: str | None = None) -> TweetPayload | None:
    """Fetch a tweet (and the tweet it quotes, if any) via FxTwitter.

    Returns ``None`` if the tweet doesn't exist / isn't accessible. Network and
    server errors raise ``requests.RequestException``.
    """
    tweet = _fetch_tweet_raw(tweet_id, translate_to)
    if tweet is None:
        return None

    payload = _build_payload(tweet)
    _ensure_translation(payload.tweet, translate_to)

    quote, unavailable = _parse_quote(tweet.get("quote"), translate_to)
    if quote is not None and str(quote.tweet.get("id") or "") == str(tweet_id):
        quote, unavailable = None, None  # self-referential quote: nothing to add
    payload.quote = quote
    payload.quote_unavailable = unavailable

    return payload
