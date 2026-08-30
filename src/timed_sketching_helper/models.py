"""Plain data structures shared across the app."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceRef:
    """A normalized reference to a list of images on some external site."""

    provider: str
    kind: str  # "gallery" | "favourites"
    username: str
    folder_id: str | None  # None means the provider's "all" folder
    raw_url: str


@dataclass(frozen=True)
class ImageMeta:
    """One image discovered in a source list."""

    source_id: str
    title: str
    author: str
    image_url: str
    page_url: str
    width: int | None = None
    height: int | None = None


@dataclass
class ListItem:
    """A persisted image, as stored on an ImageList."""

    source_id: str
    title: str
    author: str
    image_url: str
    page_url: str
    position: int
    width: int | None = None
    height: int | None = None
    cached_at: str | None = None
    content_type: str | None = None


@dataclass
class ImageList:
    """A fetched, persisted list of images for a source URL."""

    id: int
    account_id: int
    provider: str
    kind: str
    source_url: str
    title: str
    fetched_at: str
    items: list[ListItem] = field(default_factory=list)
