"""Runtime configuration, loaded from the environment / a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Never fetch more than this many images for a single list, whatever MAX_IMAGES
# says. A session only ever shows a handful; a few hundred is plenty of variety.
HARD_MAX_IMAGES = 1000

# Stop a single list fetch after this many upstream API requests. Unlike
# MAX_IMAGES this is a *default*, not a ceiling — a big album that is mostly
# sensitive images (all filtered out) can page forever without the image count
# moving, so cap the work up front. The "Max API requests" advanced option
# raises it, up to HARD_MAX_REQUESTS.
DEFAULT_MAX_REQUESTS = 100
HARD_MAX_REQUESTS = 1000


def _as_max_images(value: str | None) -> int:
    try:
        n = int(value) if value is not None else HARD_MAX_IMAGES
    except ValueError:
        n = HARD_MAX_IMAGES
    return max(1, min(n, HARD_MAX_IMAGES))


def _as_max_requests(value: str | None) -> int:
    try:
        n = int(value) if value is not None else DEFAULT_MAX_REQUESTS
    except ValueError:
        n = DEFAULT_MAX_REQUESTS
    return max(1, min(n, HARD_MAX_REQUESTS))


def _as_positive_int(value: str | None, default: int) -> int:
    """Parse a positive integer, falling back to ``default`` for anything
    missing, non-numeric, or below 1 (a zero/negative TTL would make every
    cached list instantly stale)."""
    try:
        n = int(value) if value is not None else default
    except ValueError:
        return default
    return max(1, n)


@dataclass(frozen=True)
class Config:
    deviantart_client_id: str
    deviantart_client_secret: str
    deviantart_redirect_uri: str
    mature_content: bool
    data_dir: Path
    list_ttl_hours: int
    max_images: int = HARD_MAX_IMAGES
    max_requests: int = DEFAULT_MAX_REQUESTS

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def has_deviantart_credentials(self) -> bool:
        return bool(self.deviantart_client_id and self.deviantart_client_secret)


def load_config() -> Config:
    load_dotenv()
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()
    return Config(
        deviantart_client_id=os.environ.get("DEVIANTART_CLIENT_ID", ""),
        deviantart_client_secret=os.environ.get("DEVIANTART_CLIENT_SECRET", ""),
        deviantart_redirect_uri=os.environ.get(
            "DEVIANTART_REDIRECT_URI",
            "http://127.0.0.1:8765/auth/deviantart/callback",
        ),
        mature_content=_as_bool(os.environ.get("DA_MATURE_CONTENT"), default=True),
        data_dir=data_dir,
        list_ttl_hours=_as_positive_int(os.environ.get("LIST_TTL_HOURS"), 24),
        max_images=_as_max_images(os.environ.get("MAX_IMAGES")),
        max_requests=_as_max_requests(os.environ.get("MAX_REQUESTS")),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
