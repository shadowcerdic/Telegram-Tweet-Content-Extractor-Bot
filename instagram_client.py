from __future__ import annotations

import logging
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from constants import INSTAGRAM_SESSION_FILE, INSTAGRAM_USERNAME
from models import MediaItem

log = logging.getLogger(__name__)

# Absolute, so the bot behaves the same regardless of the working directory.
TEMP_DIR = Path(__file__).resolve().parent / "temp_instagram"

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {".mp4"}

_loader: Any = None


class MediaFetchError(Exception):
    """The post couldn't be fetched; the message is safe to show to users."""


def _get_loader() -> Any:
    """Create the Instaloader instance lazily (and optionally log in)."""
    global _loader
    if _loader is not None:
        return _loader

    import instaloader

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    loader = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        # One sub-directory per request ("target"), so concurrent/leftover files
        # from other posts can never be mixed up.
        dirname_pattern=str(TEMP_DIR / "{target}"),
        filename_pattern="{shortcode}",
        quiet=True,
    )

    if INSTAGRAM_USERNAME:
        try:
            loader.load_session_from_file(INSTAGRAM_USERNAME, INSTAGRAM_SESSION_FILE or None)
            log.info("Loaded Instagram session for %s", INSTAGRAM_USERNAME)
        except Exception as exc:  # noqa: BLE001 - fall back to anonymous access
            log.warning("Could not load Instagram session (%s); continuing anonymously", exc)

    _loader = loader
    return _loader


def _natural_key(path: Path) -> list[Any]:
    """Sort 'x_2' before 'x_10' (plain sorting would put _10 before _2)."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def collect_media(directory: Path) -> list[MediaItem]:
    items: list[MediaItem] = []
    for path in sorted((p for p in directory.iterdir() if p.is_file()), key=_natural_key):
        suffix = path.suffix.lower()
        if suffix in PHOTO_SUFFIXES:
            items.append(MediaItem("photo", str(path)))
        elif suffix in VIDEO_SUFFIXES:
            items.append(MediaItem("video", str(path)))
    return items


def _download(shortcode: str, workdir: Path) -> str | None:
    """Download a post into ``workdir`` and return its caption."""
    import instaloader
    from instaloader import exceptions as ex

    loader = _get_loader()
    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        loader.download_post(post, target=workdir.name)
    except (ex.LoginRequiredException, ex.PrivateProfileNotFollowedException) as exc:
        raise MediaFetchError(
            "That Instagram post is private or needs a login to view."
        ) from exc
    except ex.QueryReturnedNotFoundException as exc:
        raise MediaFetchError("That Instagram post could not be found.") from exc
    except ex.TooManyRequestsException as exc:
        raise MediaFetchError("Instagram is rate limiting the bot. Try again later.") from exc
    except Exception as exc:  # noqa: BLE001 - instaloader raises many types
        log.warning("Instagram download failed for %s: %s", shortcode, exc)
        raise MediaFetchError("Could not fetch that Instagram media.") from exc

    return (post.caption or "").strip() or None


@contextmanager
def downloaded_instagram_media(shortcode: str) -> Iterator[tuple[list[MediaItem], str | None]]:
    """Download a post to a private temp directory and always clean it up.

    Usage::

        with downloaded_instagram_media(code) as (items, caption):
            ...
    """
    workdir = TEMP_DIR / uuid.uuid4().hex
    try:
        caption = _download(shortcode, workdir)
        items = collect_media(workdir) if workdir.is_dir() else []
        yield items, caption
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def purge_temp_dir() -> None:
    """Remove leftovers from a previous run that was killed mid-download."""
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
