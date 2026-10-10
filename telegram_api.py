from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
from typing import Any

import requests

import constants
from constants import (
    CAPTION_LIMIT,
    LONG_POLL_TIMEOUT,
    MEDIA_GROUP_MAX,
    REQUEST_TIMEOUT,
    TELEGRAM_API_ROOT,
    UPLOAD_TIMEOUT,
)
from models import MediaItem

log = logging.getLogger(__name__)

_session = requests.Session()

# Don't sleep longer than this when Telegram asks us to back off (429).
_MAX_RETRY_AFTER = 30


class TelegramError(Exception):
    """A failed Bot API call. Never contains the bot token."""

    def __init__(
        self,
        method: str,
        description: str,
        error_code: int | None = None,
    ) -> None:
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Low-level helper
# ---------------------------------------------------------------------------


def _api_url(method: str) -> str:
    return f"{TELEGRAM_API_ROOT}/bot{constants.BOT_TOKEN}/{method}"


def _form_value(value: Any) -> Any:
    """Encode a value for a multipart/form-data request."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _rewind(files: dict[str, Any] | None) -> None:
    for value in (files or {}).values():
        handle = value[1] if isinstance(value, tuple) else value
        handle.seek(0)


def _call(
    method: str,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float = REQUEST_TIMEOUT,
    retries: int = 1,
) -> Any:
    """POST to the Bot API and return ``result``.

    * Raises ``TelegramError`` with Telegram's own error description (the old
      ``raise_for_status`` only said "400 Bad Request").
    * Network errors are re-raised *without* the request URL, because the URL
      contains the bot token.
    * A 429 (flood control) is retried once after the requested delay.
    """
    data = {k: v for k, v in (data or {}).items() if v is not None}

    for attempt in range(retries + 1):
        try:
            if files:
                _rewind(files)
                response = _session.post(
                    _api_url(method),
                    data={k: _form_value(v) for k, v in data.items()},
                    files=files,
                    timeout=timeout,
                )
            else:
                response = _session.post(_api_url(method), json=data, timeout=timeout)
        except requests.RequestException as exc:
            raise TelegramError(method, f"network error ({type(exc).__name__})") from None

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if payload.get("ok"):
            return payload.get("result")

        description = payload.get("description") or f"HTTP {response.status_code}"
        code = payload.get("error_code") or response.status_code
        retry_after = (payload.get("parameters") or {}).get("retry_after")

        if code == 429 and retry_after and attempt < retries and retry_after <= _MAX_RETRY_AFTER:
            log.warning("%s rate limited, retrying in %ss", method, retry_after)
            time.sleep(retry_after + 1)
            continue

        raise TelegramError(method, description, code)

    raise TelegramError(method, "unreachable")  # pragma: no cover


def _target(
    chat_id: int,
    reply_to_message_id: int | None,
    message_thread_id: int | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"chat_id": chat_id}
    if reply_to_message_id is not None:
        # allow_sending_without_reply: don't fail if the original was deleted.
        data["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }
    if message_thread_id is not None:
        data["message_thread_id"] = message_thread_id
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_me() -> dict[str, Any]:
    return _call("getMe") or {}


def get_updates(offset: int | None = None, allow_edited: bool = False) -> list[dict[str, Any]]:
    allowed = ["message", "edited_message"] if allow_edited else ["message"]
    result = _call(
        "getUpdates",
        {"timeout": LONG_POLL_TIMEOUT, "offset": offset, "allowed_updates": allowed},
        timeout=LONG_POLL_TIMEOUT + 5,
    )
    return result or []


def send_message(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict[str, Any]:
    data = _target(chat_id, reply_to_message_id, message_thread_id)
    data["text"] = text
    data["link_preview_options"] = {"is_disabled": True}
    return _call("sendMessage", data)


def edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    _call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        },
    )


def delete_message(chat_id: int, message_id: int) -> None:
    _call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def send_chat_action(chat_id: int, action: str, message_thread_id: int | None = None) -> None:
    _call(
        "sendChatAction",
        {"chat_id": chat_id, "action": action, "message_thread_id": message_thread_id},
    )


def _guess_mime(path: str, kind: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or ("image/jpeg" if kind == "photo" else "video/mp4")


def send_media(
    chat_id: int,
    item: MediaItem,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> dict[str, Any]:
    """Send one photo/video, from a URL or a local file."""
    if item.kind not in {"photo", "video"}:
        raise ValueError(f"Unsupported media type: {item.kind}")
    if not item.source:
        raise ValueError("Media source is empty")

    method, field = ("sendPhoto", "photo") if item.kind == "photo" else ("sendVideo", "video")

    data = _target(chat_id, reply_to_message_id, message_thread_id)
    if caption:
        data["caption"] = caption[:CAPTION_LIMIT]
    if item.kind == "video":
        data["supports_streaming"] = True

    if item.is_url:
        data[field] = item.source
        return _call(method, data)

    with open(item.source, "rb") as handle:
        files = {field: (os.path.basename(item.source), handle, _guess_mime(item.source, item.kind))}
        return _call(method, data, files=files, timeout=UPLOAD_TIMEOUT)


def send_media_group(
    chat_id: int,
    items: list[MediaItem],
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> list[dict[str, Any]]:
    """Send an album of 2-10 items. URL and local-file items can be mixed."""
    if not 2 <= len(items) <= MEDIA_GROUP_MAX:
        raise ValueError(f"A media group needs 2-{MEDIA_GROUP_MAX} items, got {len(items)}")

    media: list[dict[str, Any]] = []
    files: dict[str, Any] = {}
    handles = []

    try:
        for index, item in enumerate(items):
            if item.kind not in {"photo", "video"}:
                raise ValueError(f"Unsupported media type: {item.kind}")

            entry: dict[str, Any] = {"type": item.kind}
            if item.is_url:
                entry["media"] = item.source
            else:
                attach_name = f"file{index}"
                handle = open(item.source, "rb")
                handles.append(handle)
                files[attach_name] = (
                    os.path.basename(item.source),
                    handle,
                    _guess_mime(item.source, item.kind),
                )
                entry["media"] = f"attach://{attach_name}"

            if item.kind == "video":
                entry["supports_streaming"] = True
            if index == 0 and caption:
                entry["caption"] = caption[:CAPTION_LIMIT]
            media.append(entry)

        data = _target(chat_id, reply_to_message_id, message_thread_id)
        data["media"] = media
        return _call(
            "sendMediaGroup",
            data,
            files=files or None,
            timeout=UPLOAD_TIMEOUT if files else REQUEST_TIMEOUT * 3,
        )
    finally:
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
