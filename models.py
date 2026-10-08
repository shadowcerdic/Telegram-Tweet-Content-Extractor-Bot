from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaItem:
    """A photo or video, referenced either by URL or by a local file path."""

    kind: str  # "photo" or "video"
    source: str

    @property
    def is_url(self) -> bool:
        return self.source.lower().startswith(("http://", "https://"))
