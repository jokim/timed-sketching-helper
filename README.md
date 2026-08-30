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

By default the app talks to DeviantArt anonymously (client-credentials), and
DeviantArt serves *blurred* images for anything marked mature or sensitive. To
see them un-blurred, click **Connect DeviantArt** on the start screen and log in
with an account that has mature content enabled in its settings. For lists you
already fetched while logged out, tick **Re-download images** when starting the
next session so the blurred copies are replaced.

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
  (`app.db` + `cache/`). Repeat sessions on the same list start instantly and
  work offline. A list older than `LIST_TTL_HOURS` (24 by default) is re-fetched.
- The backend only supplies the list and a randomized selection; the timer and
  all session controls run in the browser.
- Sources sit behind a small provider interface (`sources/base.py`), so other
  sites (e.g. Pinterest) can be added later without touching the rest.

## Development

```bash
uv run pytest
```

`respx` mocks the DeviantArt API, so the tests need no network or credentials.
