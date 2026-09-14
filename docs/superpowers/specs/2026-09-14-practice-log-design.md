# Practice log — design spec

Date: 2026-09-14

## Purpose

Give the (single, local) user a persistent log of every drawing practice
they've run: which source URL, which images, how long, and how it ended.
From the main page they can browse it, delete a single entry, restart a
past practice with a fresh random pull from the same source URL, and clear
the whole log from Settings. The stored data doubles as the basis for
future statistics (not built in this pass — see Non-goals).

Named "practice" rather than "session" to avoid clashing with the existing
`tsh_session` browser-identity cookie and the `session`/`/api/sessions`
drawing-session machinery already in the codebase — this feature is purely
additive and does not rename any of that existing code.

## Data model

Two new tables in `db.py`, following the existing `account_id`-scoped
pattern (`current_account()`, hard-coded to `1`):

```sql
CREATE TABLE practice_log (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    source_url  TEXT NOT NULL,
    list_title  TEXT NOT NULL,
    list_kind   TEXT NOT NULL,
    count       INTEGER NOT NULL,      -- requested image count
    duration    INTEGER NOT NULL,      -- seconds per image
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress | completed | ended_early
    shown_count INTEGER
);

CREATE TABLE practice_log_items (
    id              INTEGER PRIMARY KEY,
    practice_log_id INTEGER NOT NULL REFERENCES practice_log(id) ON DELETE CASCADE,
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL,
    author          TEXT NOT NULL,
    page_url        TEXT NOT NULL,
    position        INTEGER NOT NULL
);
```

`practice_log_items` rows are a **copy** of the selected images at practice
start, not a foreign key into `list_items` — that table is wiped and
replaced (`db.save_list`) on every re-fetch of a list, so a log entry needs
its own snapshot to keep meaning "what was actually shown" regardless of
what happens to the list later. This mirrors how `tsh:favorites`/`tsh:recent`
already store a `thumb` `source_id` independent of the live list.

`status` starts `in_progress` at practice creation and is updated once,
when the practice ends (`completed` from reaching `#view-done`, or
`ended_early` from `endSession()`/quitting mid-practice). A practice that's
abandoned without either firing (browser closed, tab crash) simply stays
`in_progress` forever — acceptable; no cleanup job for this pass.

## Backend API (`main.py`)

- **`POST /api/sessions`** (existing, unchanged request/behavior) — after
  building the selected/pool split, best-effort inserts one `practice_log`
  row (status `in_progress`) + its `practice_log_items`, using the already-
  loaded `image_list`'s `source_url`/`title`/`kind`. A logging failure
  (caught and logged, never raised) must not block the practice from
  starting. Response gains `"practice_id": int | None`.

- **`PATCH /api/practice-log/{practice_id}`** — body
  `{"status": "completed" | "ended_early", "shown_count": int}`. Sets
  `ended_at = now()`, `status`, `shown_count`. 404 if the id doesn't exist
  or doesn't belong to `db.current_account()`. Called fire-and-forget from
  the frontend; its failure must never block navigation away from a
  practice.

- **`GET /api/practice-log?limit=200`** — newest-first (`started_at DESC`)
  list of entries for the account: `id, source_url, list_title, list_kind,
  count, duration, started_at, ended_at, status, shown_count, thumb` (thumb
  = the `source_id` of the item at `position = 0`, via a join/subquery —
  same shape as the existing list-thumb pattern in `_fetch_list`).

- **`GET /api/practice-log/{practice_id}`** — one entry (same fields) plus
  `items: [{source_id, title, author, page_url}]` in position order. 404 if
  not found / not owned. Used to lazily expand a row's thumbnail strip.

- **`DELETE /api/practice-log/{practice_id}`** — deletes one entry
  (`ON DELETE CASCADE` removes its items). 404 if not found/owned, else 204.

- **`DELETE /api/practice-log`** — clears every entry for the account,
  204. This is what the Settings "Clear practice log" action calls.

## Frontend (`static/`)

**New view.** `#view-practice-log`, added to `app.js`'s `views` map
(`show("practiceLog")`) alongside start/session/done, following the same
`show()`/`data-view` convention. Reachable only from the start screen and
returns to it (no deep session-view access).

**Entry points on the start screen** (`index.html`, near the existing
`.brand` header): a "Practice log" button opening `#view-practice-log`, and
a separate gear "Settings" button opening a new small app-wide modal
(distinct DOM from the existing in-session `#settings-modal`, which stays
scoped to dock/beep and is opened only from inside a running session).
The new modal's only content for this pass is a "Clear practice log"
button gated behind `window.confirm(...)` (destructive, matches the
existing vanilla-JS-only approach — no added dependencies).

**Log view rows.** Each `practice_log` entry renders similarly to the
existing `savedRow()`/`.saved-row` pattern (reusing `savedThumb`-style
thumbnail-with-fallback-letter): title (`list_title`), kind, image count,
duration, a human-formatted `started_at`, a status badge
(in progress/completed/ended early), a **Restart** button, and a
**Delete** button. Thumbnails for the full item list are lazy-loaded on
row expand via `GET /api/practice-log/{id}` rather than shipped up front
in the list response, to keep `GET /api/practice-log` light. An image
thumbnail can fail to load if the source image later fell out of the
30-day image cache — same tolerated failure mode as `savedThumb()`
(falls back to the kind's initial letter on `<img>` error).

**Restart flow.** The existing start-form submit handler's body (fetch
list → save prefs → `startSession()`) is factored out into a shared
`beginFromUrl(url, { count, duration, forceRefresh, maxImages, maxRequests })`
function. The submit handler calls it with the form's current values;
a log row's Restart button calls it with that entry's `source_url` /
`count` / `duration` (`forceRefresh: false`, no image/request caps),
closing the log view first so the existing start-screen progress bar and
error handling (`setStartStatus`) are visible during the re-fetch. This
starts a **new** practice (and thus writes a new `practice_log` row) with
a fresh random image selection from `build_session` — no special-casing
needed since it's the same code path as any other practice start.

**Wiring practice_id through a running practice.**
- `startSession()` stores `state.practiceId = data.practice_id ?? null`
  from the `POST /api/sessions` response.
- `finishSession()` (reaching `#view-done`) fires
  `PATCH /api/practice-log/{state.practiceId}`
  `{status: "completed", shown_count: session.items.length}` — fire-and-
  forget, skipped entirely if `state.practiceId` is null.
- `endSession()` (Esc / End button / `q`) fires the same PATCH with
  `{status: "ended_early", shown_count: session.index}` (images already
  passed by the time of quitting), same fire-and-forget/null-guard.

## Error handling & edge cases

- Practice-log writes are best-effort everywhere they touch the hot path
  (`POST /api/sessions` creation, the finish `PATCH`): a DB error is caught,
  logged, and never surfaces to the user or blocks the actual
  practice/timer flow, consistent with "the backend holds no [required]
  session state" — the log is an observability/history layer on top, not
  a dependency of the practice mechanics.
- Deleting a `practice_log` entry never touches `image_lists`/`list_items`/
  `image_cache` — those are independent, TTL-managed stores.
- Restarting a practice whose source list has since been deleted upstream
  (DeviantArt) or whose URL now 404s surfaces the same error path as a
  normal failed fetch (`setStartStatus(err.message, true)`).

## Non-goals (this pass)

- No statistics UI/aggregates — the data model supports it later (a
  `practice_log` row + its `practice_log_items` is enough for "images per
  week", "time spent", "most-practiced source", etc.) but nothing is built
  now, per explicit scope decision.
- No pagination beyond a flat `limit` (default 200) on `GET
  /api/practice-log` — acceptable for a single local user's history size.
- No retention/expiry policy on `practice_log` — it grows until the user
  clears it themselves.

## Testing

New `tests/test_practice_log.py`, API-level via `TestClient` (matching
`test_main.py`'s style):
- `POST /api/sessions` creates a `practice_log` row + items; response
  includes `practice_id`.
- `PATCH /api/practice-log/{id}` updates `status`/`shown_count`/`ended_at`;
  404 for an unknown id.
- `GET /api/practice-log` returns newest-first with the expected fields
  and thumb.
- `GET /api/practice-log/{id}` returns the full item list.
- `DELETE /api/practice-log/{id}` removes the row and cascades its items;
  404 for an unknown id.
- `DELETE /api/practice-log` clears all entries.
