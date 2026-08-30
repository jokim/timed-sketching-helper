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


@dataclass(frozen=True)
class Config:
    deviantart_client_id: str
    deviantart_client_secret: str
    deviantart_redirect_uri: str
    mature_content: bool
    data_dir: Path
    list_ttl_hours: int

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
        list_ttl_hours=int(os.environ.get("LIST_TTL_HOURS", "24")),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
