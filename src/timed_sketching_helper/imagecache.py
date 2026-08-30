"""On-disk cache of image bytes, plus the logic behind the image proxy endpoint.

DeviantArt's ``content.src`` URLs are time-limited signed links, so images are
downloaded once (right after a list is fetched, while the links are valid) and
served from disk afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from timed_sketching_helper import db
from timed_sketching_helper.models import ListItem

USER_AGENT = (
    "Mozilla/5.0 (compatible; timed-sketching-helper/0.1; personal drawing practice)"
)
DEFAULT_CONTENT_TYPE = "image/jpeg"


class CacheFetchError(RuntimeError):
    """An image could not be downloaded (e.g. the signed URL expired)."""


class ImageCache:
    def __init__(self, conn: sqlite3.Connection, cache_dir: Path | str) -> None:
        self._conn = conn
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, source_id: str) -> Path:
        digest = hashlib.sha256(source_id.encode()).hexdigest()
        return self._dir / digest

    def open_cached(self, source_id: str) -> tuple[Path, str] | None:
        entry = db.get_cache_entry(self._conn, source_id)
        if entry is None:
            return None
        path = self._path(source_id)
        if not path.exists():
            db.clear_cache_entry(self._conn, source_id)
            return None
        return path, entry["content_type"]

    async def ensure(
        self,
        source_id: str,
        image_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[Path, str]:
        cached = self.open_cached(source_id)
        if cached is not None:
            return cached

        async with _borrow_client(client) as http:
            try:
                response = await http.get(image_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise CacheFetchError(
                    f"Could not download image {source_id}: {exc}"
                ) from exc

        content_type = (
            response.headers.get("Content-Type", DEFAULT_CONTENT_TYPE)
            .split(";")[0]
            .strip()
            or DEFAULT_CONTENT_TYPE
        )
        path = self._path(source_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(response.content)
        tmp.replace(path)
        db.record_cache_entry(self._conn, source_id, content_type)
        return path, content_type

    async def ensure_many(
        self, items: list[ListItem], *, concurrency: int = 6
    ) -> None:
        semaphore = asyncio.Semaphore(concurrency)

        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        ) as http:

            async def worker(item: ListItem) -> None:
                async with semaphore:
                    try:
                        await self.ensure(
                            item.source_id, item.image_url, client=http
                        )
                    except CacheFetchError:
                        pass  # a single broken image should not fail the batch

            await asyncio.gather(*(worker(item) for item in items))


@asynccontextmanager
async def _borrow_client(client: httpx.AsyncClient | None):
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as owned:
        yield owned
