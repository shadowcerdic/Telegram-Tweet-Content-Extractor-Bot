from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Iterator

import yt_dlp
from yt_dlp.utils import DownloadError

from constants import MAX_UPLOAD_BYTES, YOUTUBE_PROXY

log = logging.getLogger(__name__)


class YoutubeError(Exception):
    """Base error for YouTube operations."""


class YoutubeOversizedError(YoutubeError):
    """Media exceeds the 50MB upload limit."""


@dataclass(frozen=True)
class YoutubeInfo:
    video_id: str
    title: str
    duration: int | None
    thumbnail: str | None


def get_video_info(video_id: str) -> YoutubeInfo | None:
    """Fetch video metadata quickly without downloading."""
    ydl_opts: dict = {
        "quiet": True,
        "skip_download": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if YOUTUBE_PROXY:
        ydl_opts["proxy"] = YOUTUBE_PROXY

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            data = ydl.extract_info(url, download=False)
            if not data:
                return None
            return YoutubeInfo(
                video_id=video_id,
                title=data.get("title") or "YouTube Video",
                duration=data.get("duration"),
                thumbnail=data.get("thumbnail"),
            )
    except Exception as exc:
        log.warning("Failed to fetch YouTube info for %s: %s", video_id, exc)
        return None


@contextlib.contextmanager
def download_youtube_media(video_id: str, fmt_key: str) -> Iterator[tuple[str, str, str]]:
    """Download the requested format to a temporary directory.

    Yields (file_path, title, media_type) where media_type is "audio" or "video".
    Zero residue: deletes the directory on exit.
    """
    tmp_dir = tempfile.mkdtemp(prefix="tweetbot_yt_")
    url = f"https://www.youtube.com/watch?v={video_id}"

    # Format selectors targeting under MAX_UPLOAD_BYTES:
    # high: 1080p/720p mp4 video+audio
    # low: <=480p mp4 video+audio
    # audio: bestaudio m4a or converted
    if fmt_key == "audio":
        format_spec = "bestaudio[ext=m4a]/bestaudio/best"
        media_type = "audio"
        outtmpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")
    elif fmt_key == "low":
        format_spec = (
            "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=480][ext=mp4]/"
            "best[height<=480]/best"
        )
        media_type = "video"
        outtmpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")
    else:  # "high" or default
        format_spec = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=1080][ext=mp4]/"
            "best[height<=720][ext=mp4]/"
            "best"
        )
        media_type = "video"
        outtmpl = os.path.join(tmp_dir, "%(title).80s.%(ext)s")

    ydl_opts = {
        "format": format_spec,
        "outtmpl": outtmpl,
        "max_filesize": MAX_UPLOAD_BYTES,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4" if media_type == "video" else None,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if YOUTUBE_PROXY:
        ydl_opts["proxy"] = YOUTUBE_PROXY

    try:
        title = "YouTube Video"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = info.get("title") or title

        files = [
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if not f.endswith((".part", ".ytdl"))
        ]
        if not files:
            raise YoutubeError("No output file generated.")

        # Pick the largest downloaded file (in case of intermediate remnants)
        chosen_file = max(files, key=os.path.getsize)
        size = os.path.getsize(chosen_file)
        if size > MAX_UPLOAD_BYTES:
            raise YoutubeOversizedError(
                f"فایل دانلودی ({size // (1024 * 1024)}MB) بزرگتر از سقف ۵۰ مگابایت تلگرام است."
            )

        yield chosen_file, title, media_type
    except DownloadError as exc:
        msg = str(exc)
        if "larger than maximum file size" in msg.lower() or "filesize" in msg.lower():
            raise YoutubeOversizedError("حجم این مدیا بالاتر از ۵۰ مگابایت (سقف تلگرام) است.") from exc
        raise YoutubeError(f"خطا در دانلود از یوتیوب: {exc}") from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
