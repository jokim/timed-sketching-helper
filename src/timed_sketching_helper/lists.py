"""Fetch-or-load a list of images for a source URL."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from timed_sketching_helper import db
from timed_sketching_helper.models import ImageList, ImageMeta
from timed_sketching_helper.sources.base import SourceProvider, resolve


def _is_fresh(fetched_at: str, ttl_hours: int) -> bool:
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < timedelta(hours=ttl_hours)


def _dedupe(images: list[ImageMeta]) -> list[ImageMeta]:
    seen: set[str] = set()
    unique: list[ImageMeta] = []
    for image in images:
        if image.source_id in seen:
            continue
        seen.add(image.source_id)
        unique.append(image)
    return unique


def _title_for(ref) -> str:
    if ref.kind == "tag":
        return f"#{ref.tag}"
    label = "favourites" if ref.kind == "favourites" else "gallery"
    if ref.folder_id:
        return f"{ref.username} · {label} · {ref.folder_id}"
    return f"{ref.username} · {label}"


async def get_list(
    conn: sqlite3.Connection,
    account_id: int,
    url: str,
    *,
    force_refresh: bool = False,
    clear_image_cache: bool = False,
    ttl_hours: int = 24,
    resolver: Callable[[str], SourceProvider] = resolve,
) -> ImageList:
    provider = resolver(url)
    ref = provider.parse(url)

    meta = db.get_list_meta(conn, account_id, ref.raw_url)
    if (
        meta is not None
        and not force_refresh
        and _is_fresh(meta["fetched_at"], ttl_hours)
    ):
        cached = db.load_list(conn, meta["id"])
        if cached is not None and cached.items:
            return cached

    images = _dedupe(await provider.list_images(ref))
    if not images:
        raise ValueError(f"No images found for {url!r}.")

    list_id = db.save_list(conn, account_id, ref, _title_for(ref), images)
    result = db.load_list(conn, list_id)
    assert result is not None

    if clear_image_cache:
        # After a DeviantArt login the signed URLs may now resolve to
        # un-blurred bytes. Drop the cache index so the bytes get
        # re-downloaded rather than served stale. (A plain force_refresh
        # only rotates the signed URLs; the downloaded bytes stay valid.)
        db.clear_cache_entries(conn, [item.source_id for item in result.items])

    return result
