from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from constants import INSTAGRAM_SESSION_FILE, INSTAGRAM_USERNAME, MAX_UPLOAD_BYTES
from models import MediaItem

log = logging.getLogger(__name__)

# Absolute, so the bot behaves the same regardless of the working directory.
TEMP_DIR = Path(__file__).resolve().parent / "temp_instagram"

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {".mp4"}
WEBP_SUFFIXES = {".webp"}

_loader_local = threading.local()


class MediaFetchError(Exception):
    """The post couldn't be fetched; the message is safe to show to users."""


def _get_loader() -> Any:
    """Create a thread-local Instaloader instance lazily (and optionally log in)."""
    loader = getattr(_loader_local, "loader", None)
    if loader is not None:
        return loader

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

    _loader_local.loader = loader
    return loader


def _natural_key(path: Path) -> list[Any]:
    """Sort 'x_2' before 'x_10' (plain sorting would put _10 before _2)."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name)]


def _convert_webp_to_jpeg(path: Path) -> Path:
    """Convert a .webp image to .jpg using ffmpeg and remove the original."""
    target = path.with_suffix(".jpg")
    # If the target filename already exists, make it unique
    if target.exists():
        target = path.with_name(f"{path.stem}_{uuid.uuid4().hex[:6]}.jpg")
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-q:v", "2", str(target)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0 and target.exists() and target.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return target
        log.warning("ffmpeg webp conversion failed for %s (exit %s): %s", path.name, res.returncode, res.stderr.decode(errors="ignore")[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("webp conversion error for %s: %s", path.name, exc)
    return path


def collect_media(
    directory: Path, max_bytes: int = MAX_UPLOAD_BYTES
) -> tuple[list[MediaItem], int]:
    """Collect media items under ``max_bytes`` and count skipped oversized files."""
    items: list[MediaItem] = []
    oversized_count = 0
    # Convert any webp files in-place to jpg before collecting
    for path in sorted(directory.iterdir(), key=_natural_key):
        if path.is_file() and path.suffix.lower() in WEBP_SUFFIXES:
            _convert_webp_to_jpeg(path)

    for path in sorted((p for p in directory.iterdir() if p.is_file()), key=_natural_key):
        suffix = path.suffix.lower()
        if suffix not in PHOTO_SUFFIXES and suffix not in VIDEO_SUFFIXES:
            continue
        if path.stat().st_size > max_bytes:
            log.info(
                "Instagram file %s (%.1f MB) exceeds 50 MB cap; skipping",
                path.name,
                path.stat().st_size / (1024 * 1024),
            )
            oversized_count += 1
            continue
        kind = "photo" if suffix in PHOTO_SUFFIXES else "video"
        items.append(MediaItem(kind, str(path)))
    return items, oversized_count


# PolarisLoggedOutDesktopWWWPostRootContentQuery: distinguishes age-restricted /
# audience-gated posts from deleted/non-existent posts when _obtain_metadata()
# fails anonymously with a generic BadResponseException.
_POLARIS_RULING_DOC_ID = "27130156389949648"


def _diagnose_metadata_failure(context: Any, shortcode: str) -> str | None:
    """Inspect Polaris gating_ruling when metadata fetch fails anonymously.

    Returns a specific user-facing message when the failure reason is proven,
    or ``None`` to keep the generic fallback message.
    """
    import instaloader

    if getattr(context, "is_logged_in", False):
        return None
    if len(shortcode) > 11:
        # Private-account share links carry a 39-char token; shortcode_to_mediaid rejects >11.
        return "🔒 این پست در یک پیج خصوصی (Private) است و بدون لاگین قابل دانلود نیست."

    try:
        media_id = str(instaloader.Post.shortcode_to_mediaid(shortcode))
        resp = context.doc_id_graphql_query(
            _POLARIS_RULING_DOC_ID,
            {"media_id": media_id},
            web_api=True,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort diagnostic only
        log.debug("Polaris ruling check failed for %s: %s", shortcode, exc)
        return None

    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        return None

    polaris = data.get("xig_polaris_media")
    if polaris is None:
        return "❌ این پست در اینستاگرام یافت نشد (حذف شده یا پیج خصوصی است)."

    if isinstance(polaris, dict):
        ruling = polaris.get("gating_ruling")
        if isinstance(ruling, dict):
            title = (ruling.get("title") or "").strip()
            desc = (ruling.get("description") or "").strip()
            combined = f"{title} {desc}".lower()
            if "age-restricted" in combined or "age" in combined:
                return "🔞 این پست اینستاگرام دارای محدودیت سنی (+۱۸) است و بدون لاگین اکانت قابل دانلود نیست."
            detail = f" ({title})" if title else ""
            return f"🚫 این پست توسط اینستاگرام محدود شده است{detail} و بدون لاگین اکانت در دسترس نیست."

    return None


def _download(shortcode: str, workdir: Path) -> str | None:
    """Download a post into ``workdir`` and return its caption."""
    import instaloader
    from instaloader import exceptions as ex

    loader = _get_loader()
    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        loader.download_post(post, target=workdir.name)
    except (ex.LoginRequiredException, ex.PrivateProfileNotFollowedException) as exc:
        log.info("Instagram login/private gate for %s: %s", shortcode, exc)
        raise MediaFetchError(
            "🔒 این پست خصوصی است یا برای مشاهده به لاگین اینستاگرام نیاز دارد."
        ) from exc
    except ex.QueryReturnedNotFoundException as exc:
        log.info("Instagram post not found for %s: %s", shortcode, exc)
        raise MediaFetchError("❌ این پست در اینستاگرام یافت نشد.") from exc
    except ex.TooManyRequestsException as exc:
        log.warning("Instagram rate limit hit for %s: %s", shortcode, exc)
        raise MediaFetchError("⏳ اینستاگرام موقتاً درخواست‌های ربات را محدود (Rate-limit) کرده است؛ کمی بعد دوباره تلاش کنید.") from exc
    except ex.BadResponseException as exc:
        specific = _diagnose_metadata_failure(loader.context, shortcode)
        if specific:
            log.info("Instagram gated/missing post %s: %s", shortcode, specific)
            raise MediaFetchError(specific) from exc
        log.warning("Instagram download failed for %s: %s", shortcode, exc)
        raise MediaFetchError("❌ دریافت این مدیا از اینستاگرام ممکن نشد.") from exc
    except Exception as exc:  # noqa: BLE001 - instaloader raises many types
        log.warning("Instagram download failed for %s: %s", shortcode, exc)
        raise MediaFetchError("❌ دریافت این مدیا از اینستاگرام ممکن نشد.") from exc

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
        items, oversized_count = collect_media(workdir) if workdir.is_dir() else ([], 0)
        if oversized_count and not items:
            raise MediaFetchError(
                "⚠️ حجم این مدیای اینستاگرام بیشتر از سقف ۵۰ مگابایت تلگرام است و مستقیماً قابل ارسال نیست."
            )
        if oversized_count:
            note = f"⚠️ {oversized_count} فایل از این پست بزرگتر از سقف ۵۰ مگابایت تلگرام بود و ارسال نشد."
            caption = f"{caption}\n\n{note}".strip() if caption else note
        yield items, caption
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def purge_temp_dir() -> None:
    """Remove leftovers from a previous run that was killed mid-download."""
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
