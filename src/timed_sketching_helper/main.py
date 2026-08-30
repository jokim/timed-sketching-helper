"""FastAPI application: wiring, routes, and the dev entrypoint."""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from timed_sketching_helper import db
from timed_sketching_helper.config import Config, get_config
from timed_sketching_helper.imagecache import CacheFetchError, ImageCache
from timed_sketching_helper.lists import get_list
from timed_sketching_helper.sessions import build_session
from timed_sketching_helper.sources.base import SourceProvider, UnknownSourceError
from timed_sketching_helper.sources.deviantart import (
    DeviantArtApiError,
    DeviantArtAuthError,
    DeviantArtProvider,
)
from timed_sketching_helper.sources.deviantart_oauth import (
    DeviantArtOAuth,
    make_pkce_pair,
)

logger = logging.getLogger(__name__)

OAUTH_STATE_COOKIE = "da_oauth_state"
OAUTH_VERIFIER_COOKIE = "da_oauth_verifier"

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


def _default_resolver(cfg: Config, user_token=None):
    provider = DeviantArtProvider(
        cfg.deviantart_client_id,
        cfg.deviantart_client_secret,
        user_token=user_token,
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

    oauth = DeviantArtOAuth(
        conn,
        cfg.deviantart_client_id,
        cfg.deviantart_client_secret,
        cfg.deviantart_redirect_uri,
    )

    async def _user_token(*, force: bool = False) -> str | None:
        return await oauth.access_token(db.current_account(), force=force)

    resolver = resolver or _default_resolver(cfg, _user_token)

    # One lock per list source URL so a burst of image requests for the same
    # list triggers at most one signed-URL refresh.
    refresh_locks: dict[str, asyncio.Lock] = {}

    async def ensure_image(source_id: str) -> tuple[Path, str]:
        """Return the on-disk path + content type for an image, downloading it
        now if needed. If the stored signed URL has expired, re-fetch the list
        once to rotate the URLs and retry."""
        image_url = db.find_image_url(conn, source_id)
        if image_url is None:
            raise CacheFetchError(f"Unknown image {source_id}.")
        try:
            return await cache.ensure(source_id, image_url)
        except CacheFetchError:
            pass

        source_url = db.find_image_list_url(conn, source_id)
        if source_url is None:
            raise CacheFetchError(f"Image {source_id} is not part of any list.")
        lock = refresh_locks.setdefault(source_url, asyncio.Lock())
        async with lock:
            # Another request may have refreshed the URLs while we waited; if
            # so, the stored URL will have changed — try it before re-fetching.
            fresh_url = db.find_image_url(conn, source_id)
            if fresh_url is not None and fresh_url != image_url:
                try:
                    return await cache.ensure(source_id, fresh_url)
                except CacheFetchError:
                    pass
            await get_list(
                conn,
                db.current_account(),
                source_url,
                force_refresh=True,
                ttl_hours=cfg.list_ttl_hours,
                resolver=resolver,
            )
            newest_url = db.find_image_url(conn, source_id)
            if newest_url is None:
                raise CacheFetchError(
                    f"Image {source_id} is no longer in the source list."
                )
            return await cache.ensure(source_id, newest_url)

    async def precache(items: list) -> None:
        """Best-effort background download of a session's images."""
        try:
            await cache.ensure_many(items)
        except Exception:  # noqa: BLE001 - a background nicety, never fatal
            logger.exception("Pre-cache batch failed")
        for item in items:
            if cache.open_cached(item.source_id) is not None:
                continue
            try:
                await ensure_image(item.source_id)
            except Exception:  # noqa: BLE001
                logger.warning("Pre-cache: could not fetch %s", item.source_id)

    app = FastAPI(title="Timed Sketching Helper")

    @app.exception_handler(UnknownSourceError)
    @app.exception_handler(ValueError)
    async def _bad_request(_request, exc):  # noqa: ANN001
        return _json_error(400, str(exc))

    @app.exception_handler(DeviantArtAuthError)
    async def _auth_error(_request, exc):  # noqa: ANN001
        return _json_error(502, str(exc))

    @app.exception_handler(DeviantArtApiError)
    async def _api_error(_request, exc):  # noqa: ANN001
        return _json_error(502, str(exc))

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
            clear_image_cache=body.force_refresh,
            ttl_hours=cfg.list_ttl_hours,
            resolver=resolver,
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
    async def create_session(
        body: SessionRequest, background_tasks: BackgroundTasks
    ) -> dict:
        image_list = db.load_list(conn, body.list_id)
        if image_list is None:
            raise HTTPException(404, "List not found.")
        by_id = {i.source_id: i for i in image_list.items}
        selected, pool = build_session(list(by_id), body.count)
        background_tasks.add_task(precache, [by_id[s] for s in selected])
        return {
            "duration": body.duration,
            "items": [_item_dto(by_id[s]) for s in selected],
            "reroll_pool": [_item_dto(by_id[s]) for s in pool],
        }

    @app.get("/api/images/{source_id}")
    async def read_image(source_id: str) -> FileResponse:
        if db.find_image_url(conn, source_id) is None:
            raise HTTPException(404, "Unknown image.")
        try:
            path, content_type = await ensure_image(source_id)
        except CacheFetchError:
            raise HTTPException(
                502, "Image link expired. Refresh the list to fetch it again."
            )
        return FileResponse(
            path,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/auth/deviantart/status")
    async def deviantart_status() -> dict:
        return oauth.status(db.current_account())

    @app.get("/auth/deviantart/login")
    async def deviantart_login() -> RedirectResponse:
        state = secrets.token_urlsafe(24)
        verifier, challenge = make_pkce_pair()
        response = RedirectResponse(
            oauth.authorize_url(state, challenge), status_code=302
        )
        for name, value in (
            (OAUTH_STATE_COOKIE, state),
            (OAUTH_VERIFIER_COOKIE, verifier),
        ):
            response.set_cookie(
                name, value, max_age=600, httponly=True, samesite="lax"
            )
        return response

    @app.get("/auth/deviantart/callback")
    async def deviantart_callback(
        request: Request, code: str = "", state: str = ""
    ) -> RedirectResponse:
        expected = request.cookies.get(OAUTH_STATE_COOKIE)
        verifier = request.cookies.get(OAUTH_VERIFIER_COOKIE)
        if not expected or not secrets.compare_digest(expected, state):
            raise HTTPException(400, "OAuth state mismatch. Try connecting again.")
        if not verifier:
            raise HTTPException(400, "OAuth session expired. Try connecting again.")
        try:
            await oauth.exchange(db.current_account(), code, verifier)
        except DeviantArtAuthError:
            target = "/?da_auth=failed"
        else:
            target = "/?da_auth=connected"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(OAUTH_STATE_COOKIE)
        response.delete_cookie(OAUTH_VERIFIER_COOKIE)
        return response

    @app.post("/auth/deviantart/logout")
    async def deviantart_logout() -> Response:
        oauth.logout(db.current_account())
        return Response(status_code=204)

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


def main(
    host: str = "127.0.0.1", port: int = 8765, log_level: str = "info"
) -> None:
    log_level = log_level.lower()
    logging.basicConfig(
        level=logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    if log_level == "debug":
        # httpx logs every DeviantArt request/response at DEBUG.
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("httpcore").setLevel(logging.INFO)

    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if not cfg.has_deviantart_credentials:
        print(
            "Warning: DeviantArt credentials are not set. Copy .env.example to "
            ".env and fill them in.\n"
        )
    uvicorn.run(create_app(cfg=cfg), host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
