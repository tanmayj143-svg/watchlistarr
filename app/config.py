"""Watchlistarr configuration and paths.

Every setting can be overridden by environment variable so the container can be
configured without touching the WebUI. The WebUI/settings page writes into the
database; env vars act as defaults only (DB wins once set).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Watchlistarr"
# Single source of truth for the version: kept in step with the Docker tag by
# tests/test_version_consistency.py, which reads this literal out of the file.
APP_VERSION = "1.6.5"
APP_SLUG = "watchlistarr"


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


@dataclass
class Settings:
    """Runtime configuration."""

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("WATCHLISTARR_DATA", "/data"))
    )
    host: str = os.getenv("WATCHLISTARR_HOST", "0.0.0.0")
    port: int = _env_int("WATCHLISTARR_PORT", 7777)

    # --- integrations -------------------------------------------------
    seerr_url: str = os.getenv("SEERR_URL", "")
    seerr_api_key: str = os.getenv("SEERR_API_KEY", "")
    radarr_url: str = os.getenv("RADARR_URL", "")
    radarr_api_key: str = os.getenv("RADARR_API_KEY", "")

    # --- behaviour ----------------------------------------------------
    scheduler_enabled: bool = _env_bool("WATCHLISTARR_SCHEDULER", True)
    sync_interval_minutes: int = _env_int("WATCHLISTARR_SYNC_INTERVAL", 30)
    request_interval_minutes: int = _env_int("WATCHLISTARR_REQUEST_INTERVAL", 30)
    metadata_cache_hours: int = _env_int("WATCHLISTARR_METADATA_TTL", 168)

    # --- scraping -----------------------------------------------------
    http_timeout: int = _env_int("WATCHLISTARR_HTTP_TIMEOUT", 30)
    user_agent: str = os.getenv(
        "WATCHLISTARR_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    )
    max_pages_per_list: int = _env_int("WATCHLISTARR_MAX_PAGES", 10)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "watchlistarr.db"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "watchlistarr.log"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def seerr_configured(self) -> bool:
        return bool(self.seerr_url and self.seerr_api_key)

    @property
    def radarr_configured(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key)


settings = Settings()
settings.ensure_dirs()