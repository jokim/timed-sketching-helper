# Timed Sketching Helper

A small local web app for timed drawing practice. Give it a DeviantArt gallery
or favourites URL; it fetches the images, then shows a random subset one at a
time for a fixed number of seconds each (default 90s).

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
```

Register a DeviantArt application at <https://www.deviantart.com/developers/>,
put its client id and secret into `.env`, and add
`http://127.0.0.1:8765/auth/deviantart/callback` to the app's **OAuth2 Redirect
URI Whitelist**:

```
DEVIANTART_CLIENT_ID=your-id
DEVIANTART_CLIENT_SECRET=your-secret
```

### Sensitive / mature content

Images DeviantArt marks mature or sensitive come back *blurred* unless you're
signed in with an account allowed to see them. Blurred images are useless as
references, so the app drops them — they never appear in a list or session. To
include sensitive images, click **Connect DeviantArt** on the start screen and
log in with an account that has mature content enabled in its settings. Lists
you already fetched keep their old contents until they expire (24h) or you tick
**Re-download images** when starting the next session.

## Run

```bash
uv run timed-sketching-helper
```

Then open <http://127.0.0.1:8765>.

Paste a URL such as:

- `https://www.deviantart.com/<user>/gallery/all`
- `https://www.deviantart.com/<user>/gallery/<folderid>/<name>`
- `https://www.deviantart.com/<user>/favourites/all`
- `https://www.deviantart.com/<user>/favourites/<folderid>/<name>`
- `https://www.deviantart.com/tag/<tag>`

Set the image count and seconds-per-image, then **Start practice**.

During a session: **Pause** (space), **Prev** (←), **Skip** (→),
**Reroll** (r) to swap the current image for another random one from the list.

## How it works

- Fetched lists and downloaded image bytes are cached under `./data/`
  (`app.db` + `cache/`). A list older than `LIST_TTL_HOURS` (24 by default) is
  re-fetched.
- A list fetch stops at `MAX_IMAGES` deviations (1000 by default, and the hard
  ceiling) — a session only ever shows a handful.
- Starting a session no longer waits for the whole gallery to download: each
  image is fetched the first time it's shown, and the session's images are
  pre-downloaded in the background. If a signed image link has expired by the
  time you view it, the list is silently re-fetched and the image retried.
- The backend only supplies the list and a randomized selection; the timer and
  all session controls run in the browser.
- Sources sit behind a small provider interface (`sources/base.py`), so other
  sites (e.g. Pinterest) can be added later without touching the rest.

## Development

```bash
uv run pytest
```

`respx` mocks the DeviantArt API, so the tests need no network or credentials.
