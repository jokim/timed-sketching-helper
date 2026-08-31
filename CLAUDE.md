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

There is **no frontend build step or JS tooling** — `static/` is three hand-written
files (`index.html`, `styles.css`, `app.js`, vanilla ES modules-free) served as-is
and bundled into the wheel by `uv build`. Edit them and reload the browser.

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

- **The frontend is one page, three views** (`#view-start` / `#view-session` /
  `#view-done`) toggled by `show()`, which also stamps `document.body.dataset.view`
  — `styles.css` keys the full-viewport, non-scrolling session layout off
  `body[data-view="session"]`. `app.js` finds form fields and controls by fixed
  ids / `data-*` attributes (`#url`, `#count`, `#duration`, `#force-refresh`,
  toolbar buttons via `[data-action]`, dock buttons via `[data-dock]`), so keep
  those when editing `index.html`. **End** returns to the start screen;
  `#view-done` is only reached when the timer runs out on the last image.

- **Saved reference URLs live only in `localStorage`, by design (privacy).**
  `app.js` keeps two stores — `tsh:favorites` and `tsh:recent` (capped at 10,
  favorites excluded) — each an array of `{url, title, kind, thumb}`, where
  `thumb` is the `source_id` of the list's first image (from the `/api/lists`
  result). The start screen is a `.start-layout` flex row — the `.card` plus an
  `#saved` side panel (`renderSaved()`) that wraps below the card under ~900px;
  each row leads with a `.saved-thumb` icon (`savedThumb()` — `<img>` at
  `/api/images/{thumb}`, falling back to the kind's initial letter while it
  loads or on error), then picks / stars / deletes via `data-act` on delegated
  buttons. `#view-done`'s `#fav-btn` stars
  the just-finished list. A fetched list is pushed to `tsh:recent` on the
  `/api/lists` success path. The datalist on `#url` is fed from these stores.
  The backend `GET /api/recent` endpoint still exists but nothing calls it.

- **Session layout is CSS-grid driven by `#view-session[data-dock=top|left|right|bottom]`.**
  The toolbar repositions around the image stage; the choice persists in
  `localStorage` (`tsh:dock`), never the backend. Grid tracks are `minmax(0, 1fr)`
  and `body` / `#view-session` are `overflow: hidden` so the reference image can
  never grow the page. Zoom/pan lives entirely in `app.js` (`zoomView` state,
  `#stage-img` `transform`): wheel / `+` `-` keys / the `#zoom` buttons (shown
  only past fit-scale), reset on every image change. Theme is dark-only and
  fully self-contained — no web fonts, no external assets; tokens in `:root`.

- **Source-provider seam.** `sources/base.py` defines the `SourceProvider` protocol
  and a `REGISTRY`; `resolve(url)` returns the first provider whose `matches()` is
  true. Adding Pinterest/etc. = one new module implementing the protocol + a registry
  entry; nothing else changes. Note: `main._default_resolver` does *not* use the
  registry — it builds a `DeviantArtProvider` with credentials from config, because
  the registry instance is credential-less.

- **List caching in `lists.get_list`.** Fetch-or-load against `image_lists`
  with a TTL (`LIST_TTL_HOURS`, default 24h); a cache hit skips the provider
  entirely. `force_refresh=True` re-fetches (rotating the short-lived signed
  `content.src` URLs); `clear_image_cache=True` *also* drops the list's
  `image_cache` rows (the "Re-download images" / un-blur path).

- **List-fetch progress streaming.** `POST /api/lists` with
  `Accept: application/x-ndjson` streams newline-delimited JSON: a
  `{"type":"progress","requests":N,"images":M}` line after every upstream API
  request, then a final `{"type":"result",…}` or `{"type":"error",…}` line.
  Without that header it returns the same JSON dict as before. `get_list` /
  `SourceProvider.list_images` take an optional `on_progress(requests, images)`
  callback (`sources/base.ProgressCallback`); the DeviantArt provider fires it
  from `_Progress` per page. `main.create_list` runs the fetch as a task and
  drains an `asyncio.Queue` the callback feeds. `static/app.js`
  `fetchListStreaming` reads the stream and drives the `#fetch-progress` bar
  (eases toward — never reaches — 100%, since total request count is unknown).

- **Fetch cap.** `DeviantArtProvider._collect` stops paginating once a list
  reaches `max_images` (from `MAX_IMAGES`, default and hard ceiling
  `config.HARD_MAX_IMAGES` = 1000) — a session shows a handful, so fetching
  thousands is wasted work. `main._default_resolver` passes `cfg.max_images`;
  the constructor re-clamps to the ceiling.

- **Image bytes are cached lazily, not up front.** `get_list` no longer
  downloads anything. `GET /api/images/{id}` downloads on demand via
  `create_app.ensure_image`, which — if the stored signed URL has expired —
  re-fetches the list once (`force_refresh=True`, under a per-list
  `asyncio.Lock`) to rotate the URLs and retries. `POST /api/sessions` kicks
  off a `BackgroundTasks` job (`create_app.precache`) that pre-downloads just
  the session's shown images; `static/app.js` `preloadAhead()` warms the next
  `PRELOAD_AHEAD` (3) images in the browser cache and retains the `Image`
  objects (in `preloaded`) so the bytes stay decoded.

- **The countdown is gated on the image.** `renderCurrent()` blanks the stage
  (`#stage.loading`, `#stage-img` → `opacity:0`, with a delayed spinner) and
  holds the timer at full. It loads the next image into a **detached** `Image`
  (reusing the `preloaded` entry when present) and only assigns `#stage-img.src`
  once that has decoded (`Image.decode()`, or `load`/`error`) — pointing the
  live element straight at a plain URL keeps it painting the *previous* frame
  for the whole download, and `img.decode()` on the live element can resolve
  against that stale frame. `beginImage()` then clears `#stage.loading` and
  starts the ticker. A monotonic `renderToken` guards against a slow load
  resolving after the user has already moved on (prev/skip/reroll).

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
- API `folderid`s are UUIDs. The numeric id in a `deviantart.com/<user>/{gallery,
  favourites}/<id>/<name-slug>` URL is a *legacy* id the API does not accept —
  `/collections/<numeric>` 400s ("Request field validation failed"), `/gallery/
  <numeric>` silently returns the wrong deviations. `parse()` keeps the trailing
  `<name-slug>` as `SourceRef.folder_slug`; `_target_folder_ids` treats any
  non-UUID `folder_id` (`_is_api_folder_id`) as a name to resolve through
  `/{endpoint}/folders`, matching `_slugify(folder_slug)` against
  `_slugify(name)` — the URL slug drops the punctuation the real folder name
  carries ("Confused, bi-product of a misinformed culture" →
  `confused-bi-product-of-a-misinformed-culture`), so both sides are slugified
  (lowercase, non-alphanumeric runs → `-`) rather than compared with a naive
  `-`→space swap.
- `mature_content` must be sent on every browse request; `_get()` always adds it.
  Its value is `"true"` only while a logged-in user token is active
  (`_get_token` sets `_token_is_user`), `"false"` anonymously. This param is
  unreliable in both directions (it can under- and over-filter), so the real
  gate on sensitive content is the blur check below, not this flag.
- **Sensitive deviations are filtered by whether their `content.src` is
  blurred, not by `is_mature`.** DeviantArt hands back a blurred rendition for
  any sensitive deviation the current viewer can't see (logged out, or an
  account with mature content disabled): the wixmp transform segment carries a
  `,blur_<n>` param (`/v1/fill/w_…,q_…,strp,blur_34/pretty_name.jpg`).
  `_is_blurred_src` matches that and `_collect` drops those deviations, so they
  never reach a list or session. A mature deviation an authorised account *can*
  see comes back un-blurred and is kept. Consequence: connect a DeviantArt
  account with mature content enabled to include sensitive images; otherwise
  they are absent. Already-cached lists keep their old contents until the TTL
  lapses or "Re-download images" (`force_refresh`) re-fetches them.
- `deviantart.com/tag/<tag>` maps to the site-wide `/browse/tags?tag=` feed, not a
  user. Its `SourceRef` has `kind="tag"`, `username=""`, and `tag` set; `list_images`
  branches on that before the gallery/collections path. (`deviantart.com/tag/foo`
  used to be misparsed as the user "tag", yielding "Account is inactive".)
- `deviantart.com/search?q=<query>` (and `/search/deviations?q=…`) maps to
  `/browse/home?q=` — the only browse endpoint that still takes a `q` (the old
  `/browse/{newest,popular}` 404 now). `SourceRef` has `kind="search"`,
  `username=""`, and `query` set; `list_images` branches on it like `tag`.
- `deviantart.com/morelikethis/<username>/<numeric-id>` maps to
  `/browse/morelikethis/preview?seed=<UUID>`. `SourceRef` has
  `kind="morelikethis"`, `username` set, and `seed` = the URL's *legacy numeric*
  deviation id. The paginated `/browse/morelikethis` endpoint was **removed**
  from the API (like `/browse/newest` / `/browse/popular`); only the
  non-paginated `.../preview` remains, and its `seed` must be the **UUID**
  `deviationid` — the numeric id 400s ("Request field validation failed", same
  as `/collections/<numeric>`). No API call maps numeric→UUID, so
  `_resolve_seed_uuid` fetches the deviation's own web page
  (`https://www.deviantart.com/<username>/art/x-<numeric-id>` — any slug works,
  unauthenticated) and pulls the UUID out of the embedded Redux state
  (`_seed_uuid_from_page`: `\"<numeric-id>\":{\"deviationUuid\":\"<UUID>\"`).
  `_iter_morelikethis` then makes the preview request and yields the
  `more_from_da` (similar across DA) then `more_from_artist` (seed author's
  other work) arrays; `_collect` de-dupes the overlap. `parse()` requires both
  the `<username>` and `<numeric-id>` segments (the `/morelikethis/<numeric-id>`
  form without a username 404s on the site).
- The authorize endpoint now rejects requests without a PKCE `code_challenge`
  (`make_pkce_pair()` builds the S256 pair).
- Client-credentials tokens are treated as anonymous, so `content.src` for
  mature/sensitive deviations comes back blurred (a `,blur_<n>` transform in the
  signed URL; no URL munging undoes it). `_collect` filters those out entirely
  (see the blur-check point above) rather than serving them. To *see* sensitive
  images, use the user-login flow above with an account that has mature content
  enabled in its DeviantArt settings. The "Re-download images" checkbox sends
  `force_refresh`, which the `/api/lists` route maps to `clear_image_cache=True`
  — it re-fetches the list (re-running the blur filter) and drops that list's
  `image_cache` rows (`db.clear_cache_entries`) so any already-downloaded
  blurred bytes are replaced.
