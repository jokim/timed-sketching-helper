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
actual server launcher: it configures `logging`, derives the `TrustedHostMiddleware`
allow-list from `--host` (loopback → locked to localhost, otherwise off + a warning),
and hands off to `uvicorn.run`.

There is **no frontend build step or JS tooling** — `static/` is three hand-written
files (`index.html`, `styles.css`, `app.js`; plain browser JS, no ES modules)
served as-is and bundled into the wheel by `uv build`. Edit them and reload the
browser.

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
  ids / `data-*` attributes (`#url`, `#count`, `#duration`; `#force-refresh` and
  `#max-images` inside the collapsed `<details class="advanced">` "Advanced
  options" panel; toolbar buttons via `[data-action]`, dock buttons via
  `[data-dock]`), so keep
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
  with a TTL (`LIST_TTL_HOURS`, default 24h; `config._as_positive_int` floors it
  at 1 and falls back to 24 for non-numeric values); a cache hit skips the provider
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
  reaches `max_images` (from `MAX_IMAGES`, default `config.DEFAULT_MAX_IMAGES`
  = 300, hard ceiling `config.HARD_MAX_IMAGES` = 1000) — a session shows a
  handful, so fetching thousands is wasted work. `main._default_resolver`
  passes `cfg.max_images`;
  the constructor re-clamps to the ceiling. `list_images(..., max_images=N)`
  (threaded from the `max_images` field on `POST /api/lists` / `get_list`, set
  by the "Limit images fetched" advanced option) lowers the cap for one fetch
  only — `min(N, self._max_images)`, so the config ceiling always wins. A cache
  hit otherwise ignores `max_images` (no long load to skip), *except* when an
  explicit `N` exceeds the cached item count — `get_list` reads that as the user
  raising a previously-lower limit and re-fetches to pull the extra images in.

- **Request cap.** `_Progress` also counts upstream API requests and exposes
  `.exhausted`; `_iter_pages` / `_folders` / `_iter_folders` stop paginating
  once it trips. Unlike `max_images`, this is a *default* (`MAX_REQUESTS`,
  `config.DEFAULT_MAX_REQUESTS` = 100) with a separate hard ceiling
  (`HARD_MAX_REQUESTS` = 1000): the `max_requests` field on `POST /api/lists` /
  `get_list` (the "Max API requests" advanced option) *raises* it for one
  fetch — `min(N, HARD_MAX_REQUESTS)` — because a large mostly-sensitive album
  can page for hundreds of requests without the image count moving (every
  deviation is blur-filtered by `_collect`), running into DeviantArt's
  `user_api_threshold` limit. Stopping on the budget is silent, like the image
  cap; the group `/gallery/folders` fallback is skipped when `.exhausted`
  (an empty result there means "gave up early", not "empty gallery").

- **Rate-limit handling.** `_get` raises `DeviantArtRateLimitError` (a
  `DeviantArtApiError` subclass) when the error detail contains
  `user_api_threshold`, with a message keyed to the active token
  (`_rate_limit_message`: per-account vs per-app, the latter suggesting a
  DeviantArt login for a separate quota). `_collect` catches it: if images were
  already collected it logs and returns the partial list (same outcome as
  hitting a cap); with nothing collected it re-raises. `main` maps the
  exception to HTTP **429** (`_rate_limited` handler); the NDJSON stream path
  surfaces the message as an `{"type":"error"}` line.

- **Image bytes are cached lazily, not up front.** `get_list` no longer
  downloads anything. `GET /api/images/{id}` downloads on demand via
  `create_app.ensure_image`, which — if the stored signed URL has expired —
  re-fetches the list once (`force_refresh=True`, under a per-list
  `asyncio.Lock`) to rotate the URLs and retries. `POST /api/sessions` kicks
  off a `BackgroundTasks` job (`create_app.precache`) that pre-downloads the
  session's shown images *plus* the first `BACKUP_POOL_SIZE` (3) images of the
  reroll pool, so an instant reroll has a warm image ready; `static/app.js`
  `preloadAhead()` separately warms the next `PRELOAD_AHEAD` (3) images in the
  browser cache and retains the `Image` objects (in `preloaded`) so the bytes
  stay decoded.

- **Reroll swaps in the pre-downloaded backup pool, never a used image.**
  `POST /api/sessions` returns `reroll_pool` already shuffled (it's a slice of
  the same shuffled list `items` came from), so `app.js` `reroll()` takes it
  from the front (`pool.shift()`) rather than a random index — identically
  random, but keeping the front of the array aligned with the images
  `BACKUP_POOL_SIZE` already precached server-side. The swapped-out image is
  **not** returned to the pool, so once shown it can never reappear later in
  the same session (`items` and `pool` stay disjoint and `pool` only shrinks).
  After each reroll `app.js` fires a fire-and-forget `POST /api/precache`
  with the current front `BACKUP_POOL_SIZE` pool ids to top the backup window
  back up; the backend's `precache_ids` (unlike `precache`, which needs full
  `ListItem`s) resolves each id through `ensure_image` directly, skipping ones
  already cached — no session state is tracked, it just warms the ids it's given.

- **Countdown beep.** `restartTicker()`'s per-second interval calls `playBeep()`
  for the final `BEEP_WINDOW` (5) seconds of an image's timer — a short sine
  tone synthesized with Web Audio (`playBeep`; no asset file, `AudioContext`
  created lazily on first use so it's built after the "Start practice" click
  satisfies the browser's autoplay-gesture requirement). Only the per-image
  timer beeps, not the "Get ready" black-screen countdown between images. The
  toolbar's speaker toggle (`data-action="audio"`) follows the same
  read/set-to-`localStorage` pattern as dock/compact (`tsh:audio`, default on).

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

- **`create_app(*, conn=None, cache=None, resolver=None, cfg=None, trusted_hosts=None)`**
  is a factory with injectable dependencies — tests pass an in-memory SQLite
  connection, a temp-dir `ImageCache`, and a fake resolver.

- **DNS-rebinding guard (`TrustedHostMiddleware`).** The app is unauthenticated
  and loopback-bound, so its only protection from a web page you happen to visit
  is the same-origin policy — which DNS rebinding defeats by re-pointing the
  attacker's hostname at `127.0.0.1`. `create_app` always installs
  `TrustedHostMiddleware`; requests whose `Host` header isn't in the allow-list
  get a flat `400`. `trusted_hosts` defaults to
  `main.DEFAULT_TRUSTED_HOSTS = ["localhost", "127.0.0.1"]` (also what the
  `uvicorn --factory` path and the `TestClient(base_url="http://localhost")`
  fixtures rely on). `main.main()` picks the list from `--host`: a loopback bind
  keeps the default; a non-loopback bind (`_is_loopback` is false) switches to
  `["*"]` — the check is off, because a public bind has no predictable
  hostname — and prints a warning that the app has no auth and is now exposed.

- **Cross-site write guard.** A second `@app.middleware("http")`
  (`_is_cross_site_write`) rejects unsafe-method requests (`403`) that a
  browser labels `Sec-Fetch-Site: cross-site`/`same-site`, or — when that
  header is absent — whose `Origin` host isn't in the trusted-host set. The
  JSON endpoints are already CSRF-safe via the preflight-then-blocked path;
  this is what protects the bodyless "simple request" ones, chiefly
  `POST /auth/deviantart/logout`. Non-browser clients (no `Sec-Fetch-Site`, no
  `Origin`) and a `["*"]` host set (public bind) skip the `Origin` fallback.

- **The "source ↗" link is scheme-checked.** `app.js` `externalHref()` only
  lets an `http(s)` `page_url` reach `#page-link.href` (a `javascript:` URL
  from the API would otherwise run in the app origin on click); anything else
  hides the link.

## DeviantArt API gotchas

- OAuth2 token endpoint is `https://www.deviantart.com/oauth2/token` — *outside*
  the `/api/v1/oauth2` tree that everything else uses (`API_BASE`).
- The token endpoint reports failures as a 302 redirect to a `redirect_error` page,
  not a JSON error body; `_error_detail()` parses the reason out of the redirect
  URL's query string.
- There is no `/collections/all`. For `.../favourites/all`, the provider lists every
  collection folder via `/collections/folders` and aggregates them.
- A DeviantArt **group**'s `/gallery/all` always returns an empty result set — a
  group's deviations are only reachable through its gallery folders. `list_images`
  handles this by falling back to aggregating `/gallery/folders` (like
  `favourites/all` does for collections) whenever a whole-gallery fetch
  (`folder_ids == ["all"]`) comes back with zero images. There is no reliable
  anonymous "is this a group?" signal — `/user/profile/<group>` 404s — so the
  empty-result fallback is the detection.
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
