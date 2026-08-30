# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                   # install deps (runtime + dev)
uv run pytest                             # run all tests
uv run pytest tests/test_lists.py::test_force_refresh_refetches   # single test
uv run timed-sketching-helper             # serve on http://127.0.0.1:8765
uv run timed-sketching-helper --help      # flags: --host / --port / --log-level / --debug
uv run timed-sketching-helper --debug     # verbose logging (httpx DeviantArt calls + uvicorn debug)
uv run uvicorn --factory timed_sketching_helper.main:create_app --reload   # dev server w/ reload
uv build                                  # build wheel/sdist (includes static/ assets)
```

Tests need no network and no credentials — `respx` mocks the DeviantArt API. Runtime
config comes from `.env` (see `.env.example`); `DEVIANTART_CLIENT_ID` is the numeric
client_id from the DeviantArt app page, and `DEVIANTART_REDIRECT_URI` must be added
verbatim to the app's OAuth2 Redirect URI Whitelist for the "Connect DeviantArt"
login to work.

The CLI seam: `__init__.py` owns the `argparse` front end (`main()`, the console-script
entry point); `main.main(host, port, log_level)` — also exported as `serve` — is the
actual server launcher and configures `logging` before handing off to `uvicorn.run`.

## Architecture

A local FastAPI app that fetches an image list from a DeviantArt URL and drives a
timed drawing session in the browser. Data (SQLite + downloaded image bytes) lives
in `./data/` (gitignored).

**Request flow:** `static/app.js` → JSON endpoints in `main.py` → `lists.py` →
`sources/` → DeviantArt API; image bytes flow through `imagecache.py` to disk and
back out via `GET /api/images/{source_id}`.

Key design points, each spanning several files:

- **The backend holds no session state.** `POST /api/sessions` returns a random
  subset of the list plus the leftover ids as `reroll_pool` (`sessions.build_session`).
  The countdown timer, pause/prev/skip, and reroll all live in `static/app.js`.

- **Source-provider seam.** `sources/base.py` defines the `SourceProvider` protocol
  and a `REGISTRY`; `resolve(url)` returns the first provider whose `matches()` is
  true. Adding Pinterest/etc. = one new module implementing the protocol + a registry
  entry; nothing else changes. Note: `main._default_resolver` does *not* use the
  registry — it builds a `DeviantArtProvider` with credentials from config, because
  the registry instance is credential-less.

- **Two-stage caching in `lists.get_list`.** Fetch-or-load against `image_lists`
  with a TTL (`LIST_TTL_HOURS`, default 24h); a cache hit skips the provider
  entirely. On a fetch it immediately calls `imagecache.ensure_many` to download
  every image, because DeviantArt's `content.src` links are short-lived signed
  URLs — storing the URL alone would rot. `force_refresh=True` re-fetches.

- **`db.py` threads `account_id` through every query.** `current_account()` is
  hard-coded to `1`; it is the single seam where real multi-user auth would plug in.

- **DeviantArt user login lives in `sources/deviantart_oauth.py` + the
  `/auth/deviantart/*` routes.** Authorization Code + PKCE grant.
  `/login` sets short-lived `state`/`verifier` cookies and 302s to DeviantArt;
  `/callback` checks the state, exchanges the code, calls `/user/whoami` for the
  display name, stores tokens in `deviantart_oauth`, and redirects to
  `/?da_auth=connected|failed` (the front end turns that into a start-screen
  message). `create_app` closes a `_user_token` callable over
  `DeviantArtOAuth.access_token` and passes it into `_default_resolver` →
  `DeviantArtProvider`, whose `_get_token` tries the user token first and falls
  back to client-credentials. Token refresh rotates the refresh token; a rejected
  refresh token is deleted so the UI re-prompts for login. `_get_token(force=True)`
  (after a 401) bypasses both caches.

- **`create_app(*, conn=None, cache=None, resolver=None, cfg=None)`** is a factory
  with injectable dependencies — tests pass an in-memory SQLite connection, a
  temp-dir `ImageCache`, and a fake resolver.

## DeviantArt API gotchas

- OAuth2 token endpoint is `https://www.deviantart.com/oauth2/token` — *outside*
  the `/api/v1/oauth2` tree that everything else uses (`API_BASE`).
- The token endpoint reports failures as a 302 redirect to a `redirect_error` page,
  not a JSON error body; `_error_detail()` parses the reason out of the redirect
  URL's query string.
- There is no `/collections/all`. For `.../favourites/all`, the provider lists every
  collection folder via `/collections/folders` and aggregates them.
- `mature_content` must be sent on every browse request; `_get()` always adds it.
- `deviantart.com/tag/<tag>` maps to the site-wide `/browse/tags?tag=` feed, not a
  user. Its `SourceRef` has `kind="tag"`, `username=""`, and `tag` set; `list_images`
  branches on that before the gallery/collections path. (`deviantart.com/tag/foo`
  used to be misparsed as the user "tag", yielding "Account is inactive".)
- The authorize endpoint now rejects requests without a PKCE `code_challenge`
  (`make_pkce_pair()` builds the S256 pair).
- Client-credentials tokens are treated as anonymous, so `content.src` for
  mature/sensitive deviations comes back with a `blur` claim baked into the
  signed URL — the downloaded bytes are blurred, and no URL munging undoes it.
  The fix is the user-login flow above (the linked account must have mature
  content enabled in its DeviantArt settings). Because the blur is in the bytes
  on disk, `force_refresh=True` on a list also clears that list's `image_cache`
  rows (`db.clear_cache_entries`) so `imagecache` re-downloads them un-blurred —
  this is the "Re-download images" checkbox on the start screen.
