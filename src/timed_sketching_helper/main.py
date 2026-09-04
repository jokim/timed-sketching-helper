"""FastAPI application: wiring, routes, and the dev entrypoint."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
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
    DeviantArtRateLimitError,
)
from timed_sketching_helper.sources.deviantart_oauth import (
    DeviantArtOAuth,
    make_pkce_pair,
)

logger = logging.getLogger(__name__)

OAUTH_STATE_COOKIE = "da_oauth_state"
OAUTH_VERIFIER_COOKIE = "da_oauth_verifier"

STATIC_DIR = Path(__file__).parent / "static"

# Host headers accepted by default (the app only ever binds to a loopback
# address unless --host says otherwise). "*" disables the check entirely.
DEFAULT_TRUSTED_HOSTS = ["localhost", "127.0.0.1"]

# Methods that never change state, so they need no cross-site-origin check.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# How many reroll-pool images to keep pre-downloaded as instant-swap backups.
BACKUP_POOL_SIZE = 3


def _is_cross_site_write(request: Request, origin_hosts: set[str] | None) -> bool:
    """Whether an unsafe-method request looks like it came from another site.

    The JSON endpoints are already CSRF-safe (a cross-origin ``fetch`` with a
    JSON body is preflighted and, with no CORS headers here, blocked). The
    plain ones — ``POST /auth/deviantart/logout`` especially — are "simple
    requests" a malicious page can fire without a preflight. Trust the
    browser's ``Sec-Fetch-Site`` label where present; otherwise fall back to
    the ``Origin`` header's host.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in {"same-origin", "none"}
    origin = request.headers.get("origin")
    if origin is None:
        # Non-browser client, or a same-origin request that sent no Origin.
        return False
    if origin_hosts is None:
        # Public bind: the operator opted out of host pinning (see main()).
        return False
    return (urlparse(origin).hostname or "").lower() not in origin_hosts


class ListRequest(BaseModel):
    url: str
    force_refresh: bool = False
    # Optional lower bound on how many images to fetch, to skip a long load.
    # The configured MAX_IMAGES ceiling still applies.
    max_images: int | None = Field(default=None, ge=1)
    # Optional override for the per-fetch API request cap (default MAX_REQUESTS,
    # 100). Raise it for a large album; the HARD_MAX_REQUESTS ceiling applies.
    max_requests: int | None = Field(default=None, ge=1)


class SessionRequest(BaseModel):
    list_id: int
    count: int = Field(ge=1, le=500)
    duration: int = Field(ge=1, le=3600)


class PrecacheRequest(BaseModel):
    source_ids: list[str]


class PrefsRequest(BaseModel):
    default_count: int = Field(ge=1, le=500)
    default_duration: int = Field(ge=1, le=3600)


def _default_resolver(provider: DeviantArtProvider):
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
    trusted_hosts: list[str] | None = None,
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

    deviantart_provider = DeviantArtProvider(
        cfg.deviantart_client_id,
        cfg.deviantart_client_secret,
        user_token=_user_token,
        max_images=cfg.max_images,
        max_requests=cfg.max_requests,
    )
    resolver = resolver or _default_resolver(deviantart_provider)

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

    async def precache_ids(source_ids: list[str]) -> None:
        """Best-effort background download for ids the frontend names directly
        (topping up the reroll backup pool); no session state is looked up."""
        for source_id in source_ids:
            if cache.open_cached(source_id) is not None:
                continue
            try:
                await ensure_image(source_id)
            except Exception:  # noqa: BLE001 - a background nicety, never fatal
                logger.warning("Pre-cache: could not fetch %s", source_id)

    app = FastAPI(title="Timed Sketching Helper")

    # DNS-rebinding guard. The app is unauthenticated and binds to loopback, so
    # the only thing standing between it and a web page you happen to visit is
    # the same-origin policy — and that falls away if an attacker's domain is
    # rebound to 127.0.0.1. Rejecting requests whose Host header we don't
    # recognise closes that hole: the browser still sends the attacker's
    # hostname in Host even after the DNS swap. See main() for how the list is
    # chosen from --host.
    allowed_hosts = trusted_hosts or DEFAULT_TRUSTED_HOSTS
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    # CSRF guard for the non-JSON write endpoints (see _is_cross_site_write).
    origin_hosts = (
        None if "*" in allowed_hosts else {h.lower() for h in allowed_hosts}
    )

    @app.middleware("http")
    async def _reject_cross_site_writes(request: Request, call_next):  # noqa: ANN001
        if request.method not in _SAFE_METHODS and _is_cross_site_write(
            request, origin_hosts
        ):
            return _json_error(403, "Cross-site request blocked.")
        return await call_next(request)

    @app.exception_handler(UnknownSourceError)
    @app.exception_handler(ValueError)
    async def _bad_request(_request, exc):  # noqa: ANN001
        return _json_error(400, str(exc))

    @app.exception_handler(DeviantArtAuthError)
    async def _auth_error(_request, exc):  # noqa: ANN001
        return _json_error(502, str(exc))

    @app.exception_handler(DeviantArtRateLimitError)
    async def _rate_limited(_request, exc):  # noqa: ANN001
        return _json_error(429, str(exc))

    @app.exception_handler(DeviantArtApiError)
    async def _api_error(_request, exc):  # noqa: ANN001
        return _json_error(502, str(exc))

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    async def _fetch_list(body: ListRequest, on_progress=None):
        image_list = await get_list(
            conn,
            db.current_account(),
            body.url,
            force_refresh=body.force_refresh,
            clear_image_cache=body.force_refresh,
            ttl_hours=cfg.list_ttl_hours,
            max_images=body.max_images,
            max_requests=body.max_requests,
            resolver=resolver,
            on_progress=on_progress,
        )
        return {
            "list_id": image_list.id,
            "title": image_list.title,
            "kind": image_list.kind,
            "count": len(image_list.items),
            "fetched_at": image_list.fetched_at,
            # First image, used as the saved-list icon in the browser.
            "thumb": image_list.items[0].source_id if image_list.items else None,
        }

    @app.post("/api/lists")
    async def create_list(body: ListRequest, request: Request):
        wants_stream = "application/x-ndjson" in request.headers.get("accept", "")
        if not wants_stream:
            return await _fetch_list(body)

        # The fetch runs as its own task; progress callbacks (fired synchronously
        # from inside the provider) drop messages onto a queue that the response
        # generator drains and streams out as newline-delimited JSON.
        queue: asyncio.Queue = asyncio.Queue()

        def on_progress(requests: int, images: int) -> None:
            queue.put_nowait(
                {"type": "progress", "requests": requests, "images": images}
            )

        async def run() -> None:
            try:
                result = await _fetch_list(body, on_progress)
                await queue.put({"type": "result", **result})
            except (UnknownSourceError, ValueError) as exc:
                await queue.put({"type": "error", "error": str(exc)})
            except (DeviantArtAuthError, DeviantArtApiError) as exc:
                await queue.put({"type": "error", "error": str(exc)})
            except Exception:  # noqa: BLE001 - headers are already sent
                logger.exception("List fetch failed mid-stream")
                await queue.put(
                    {"type": "error", "error": "Fetching the list failed."}
                )
            finally:
                await queue.put(None)

        async def body_stream():
            task = asyncio.create_task(run())
            try:
                while True:
                    message = await queue.get()
                    if message is None:
                        return
                    yield json.dumps(message) + "\n"
            finally:
                await task

        return StreamingResponse(
            body_stream(), media_type="application/x-ndjson"
        )

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
        backup = pool[:BACKUP_POOL_SIZE]
        background_tasks.add_task(precache, [by_id[s] for s in selected + backup])
        return {
            "duration": body.duration,
            "items": [_item_dto(by_id[s]) for s in selected],
            "reroll_pool": [_item_dto(by_id[s]) for s in pool],
        }

    @app.post("/api/precache")
    async def precache_backup(
        body: PrecacheRequest, background_tasks: BackgroundTasks
    ) -> dict:
        """Top up the reroll backup pool after a swap; best-effort, fire-and-forget."""
        background_tasks.add_task(precache_ids, body.source_ids)
        return {"status": "queued"}

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

    @app.get("/api/deviantart/collections")
    async def deviantart_collections() -> dict:
        status = oauth.status(db.current_account())
        if not status["connected"]:
            raise HTTPException(401, "Connect DeviantArt to list collections.")
        collections = await deviantart_provider.list_collections(status["username"])
        return {"username": status["username"], "collections": collections}

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


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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

    if _is_loopback(host):
        trusted_hosts = DEFAULT_TRUSTED_HOSTS
    else:
        # A public bind can be reached under any number of hostnames/IPs, so
        # there is no sensible allow-list to enforce — disable the Host check
        # and make the exposure loud instead.
        trusted_hosts = ["*"]
        print(
            f"Warning: binding to {host!r}, a non-loopback address. This app "
            "has NO authentication — anyone who can reach this port can use "
            "your connected DeviantArt account and read/change your data. The "
            "Host-header (DNS-rebinding) check is disabled in this mode. Only "
            "do this on a network you fully trust.\n"
        )

    app = create_app(cfg=cfg, trusted_hosts=trusted_hosts)
    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
