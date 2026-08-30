"""FastAPI application: wiring, routes, and the dev entrypoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from timed_sketching_helper import db
from timed_sketching_helper.config import Config, get_config
from timed_sketching_helper.imagecache import CacheFetchError, ImageCache
from timed_sketching_helper.lists import get_list
from timed_sketching_helper.sessions import build_session
from timed_sketching_helper.sources.base import SourceProvider, UnknownSourceError
from timed_sketching_helper.sources.deviantart import (
    DeviantArtAuthError,
    DeviantArtProvider,
)

STATIC_DIR = Path(__file__).parent / "static"


class ListRequest(BaseModel):
    url: str
    force_refresh: bool = False


class SessionRequest(BaseModel):
    list_id: int
    count: int = Field(ge=1, le=500)
    duration: int = Field(ge=1, le=3600)


class PrefsRequest(BaseModel):
    default_count: int = Field(ge=1, le=500)
    default_duration: int = Field(ge=1, le=3600)


def _default_resolver(cfg: Config):
    provider = DeviantArtProvider(
        cfg.deviantart_client_id, cfg.deviantart_client_secret
    )

    def resolver(url: str) -> SourceProvider:
        if provider.matches(url):
            return provider
        raise UnknownSourceError(f"No source provider handles this URL: {url!r}")

    return resolver


def _item_dto(item) -> dict:
    return {
        "source_id": item.source_id,
        "title": item.title,
        "author": item.author,
        "page_url": item.page_url,
    }


def create_app(
    *,
    conn: sqlite3.Connection | None = None,
    cache: ImageCache | None = None,
    resolver=None,
    cfg: Config | None = None,
) -> FastAPI:
    cfg = cfg or get_config()
    conn = conn or db.connect(cfg.db_path)
    db.init_db(conn)
    cache = cache or ImageCache(conn, cfg.cache_dir)
    resolver = resolver or _default_resolver(cfg)

    app = FastAPI(title="Timed Sketching Helper")

    @app.exception_handler(UnknownSourceError)
    @app.exception_handler(ValueError)
    async def _bad_request(_request, exc):  # noqa: ANN001
        return _json_error(400, str(exc))

    @app.exception_handler(DeviantArtAuthError)
    async def _auth_error(_request, exc):  # noqa: ANN001
        return _json_error(
            502, f"{exc} Check your DeviantArt credentials in .env."
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/lists")
    async def create_list(body: ListRequest) -> dict:
        image_list = await get_list(
            conn,
            db.current_account(),
            body.url,
            force_refresh=body.force_refresh,
            ttl_hours=cfg.list_ttl_hours,
            resolver=resolver,
            download_images=cache.ensure_many,
        )
        return {
            "list_id": image_list.id,
            "title": image_list.title,
            "kind": image_list.kind,
            "count": len(image_list.items),
            "fetched_at": image_list.fetched_at,
        }

    @app.get("/api/lists/{list_id}")
    async def read_list(list_id: int) -> dict:
        image_list = db.load_list(conn, list_id)
        if image_list is None:
            raise HTTPException(404, "List not found.")
        return {
            "list_id": image_list.id,
            "title": image_list.title,
            "kind": image_list.kind,
            "count": len(image_list.items),
            "items": [_item_dto(i) for i in image_list.items],
        }

    @app.post("/api/sessions")
    async def create_session(body: SessionRequest) -> dict:
        image_list = db.load_list(conn, body.list_id)
        if image_list is None:
            raise HTTPException(404, "List not found.")
        by_id = {i.source_id: i for i in image_list.items}
        selected, pool = build_session(list(by_id), body.count)
        return {
            "duration": body.duration,
            "items": [_item_dto(by_id[s]) for s in selected],
            "reroll_pool": [_item_dto(by_id[s]) for s in pool],
        }

    @app.get("/api/images/{source_id}")
    async def read_image(source_id: str) -> FileResponse:
        image_url = db.find_image_url(conn, source_id)
        if image_url is None:
            raise HTTPException(404, "Unknown image.")
        try:
            path, content_type = await cache.ensure(source_id, image_url)
        except CacheFetchError:
            raise HTTPException(
                502, "Image link expired. Refresh the list to fetch it again."
            )
        return FileResponse(
            path,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/recent")
    async def recent() -> list[dict]:
        rows = db.recent_lists(conn, db.current_account())
        return [
            {
                "list_id": r["id"],
                "url": r["source_url"],
                "title": r["title"],
                "kind": r["kind"],
                "fetched_at": r["fetched_at"],
            }
            for r in rows
        ]

    @app.get("/api/prefs")
    async def read_prefs() -> dict:
        prefs = db.get_preferences(conn, db.current_account())
        return {
            "default_count": int(prefs["default_count"]),
            "default_duration": int(prefs["default_duration"]),
        }

    @app.put("/api/prefs")
    async def write_prefs(body: PrefsRequest) -> dict:
        db.set_preferences(
            conn,
            db.current_account(),
            {
                "default_count": body.default_count,
                "default_duration": body.default_duration,
            },
        )
        return {
            "default_count": body.default_count,
            "default_duration": body.default_duration,
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _json_error(status: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status, content={"error": detail})


def main() -> None:
    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.has_deviantart_credentials:
        print(
            "Warning: DeviantArt credentials are not set. Copy .env.example to "
            ".env and fill them in.\n"
        )
    uvicorn.run(create_app(cfg=cfg), host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
