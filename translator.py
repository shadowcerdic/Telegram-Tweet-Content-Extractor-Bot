"""Keyless fallback translator for the FxTwitter API's own translation gaps.

Why this exists (measured on the VPS, 2026-10-28): FxTwitter's translate backend
(provider "grok") drops specific (tweet, target-language) pairs deterministically.
A 1032-char note tweet returned an empty ``translation`` for ``fa``/``de``/``hi``
on every one of six attempts while ``ar``/``tr``/``es``/``fr``/``ja``/``pt``/``ru``/
``ko``/``zh-CN`` all succeeded on the same tweet. Short tweets also fail
occasionally and recover on a second try.

`translate()` is the second layer: Google's public web endpoint
(`translate_a/single`, the one the translate web UI calls, no API key). It never
raises — a ``None`` return means "no translation available", and the caller is
required to say so in the reply instead of silently sending the original.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from constants import GOOGLE_TRANSLATE_URL, REQUEST_TIMEOUT, TRANSLATE_CHUNK_CHARS

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

# The endpoint returns the translated text as a list of [chunk, original, ...] rows.
_MIN_SEGMENT_FIELDS = 1


def _segments(payload: Any) -> list[str]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        return []
    return [
        row[0]
        for row in payload[0]
        if isinstance(row, list) and len(row) >= _MIN_SEGMENT_FIELDS and isinstance(row[0], str)
    ]


def _chunks(text: str, limit: int) -> list[str]:
    """Split on blank/normal lines, then hard-cut anything still over the limit.

    Sentences are kept whole wherever possible: the endpoint translates each
    request independently, so a mid-word split is what makes output unreadable.
    """
    pieces: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # pathological single line (rare: one long URL/text note)
            if current:
                pieces.append(current)
                current = ""
            cut = line.rfind(" ", 0, limit + 1)
            cut = limit if cut <= 0 else cut + 1
            pieces.append(line[:cut])
            line = line[cut:]
        if len(current) + len(line) > limit and current:
            pieces.append(current)
            current = line
        else:
            current += line
    if current:
        pieces.append(current)
    return pieces


def translate(text: str, target: str, source: str = "auto") -> str | None:
    """Translate ``text`` into ``target``; ``None`` on any failure.

    Never raises: a broken fallback must not stop a tweet from being delivered.
    """
    text = (text or "").strip()
    if not text or not target:
        return None

    out: list[str] = []
    try:
        for piece in _chunks(text, TRANSLATE_CHUNK_CHARS):
            response = _session.get(
                GOOGLE_TRANSLATE_URL,
                params={
                    "client": "gtx",
                    "sl": source,
                    "tl": target,
                    "dt": "t",
                    "q": piece,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                log.info("fallback translate refused: HTTP %s", response.status_code)
                return None
            segments = _segments(response.json())
            if not segments:
                log.info("fallback translate returned no segments")
                return None
            out.append("".join(segments))
    except (requests.RequestException, ValueError) as exc:
        # ValueError = non-JSON body (captive portal, rate-limit HTML page, ...).
        log.info("fallback translate failed: %s", exc)
        return None

    result = "".join(out).strip()
    if not result or result == text.strip():
        return None  # endpoint echoed the input = it did not translate
    return result
