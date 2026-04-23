"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Typed container for all runtime configuration values."""

    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    database_url: str
    redis_url: str
    discord_webhook_url: str | None


def _require(name: str) -> str:
    """Return the value of *name* from the environment or raise ValueError."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(name)
    return value


def _load_settings() -> Settings:
    """Validate and assemble Settings from the environment.

    Raises:
        ValueError: If one or more required variables are absent or empty.
    """
    missing: list[str] = []

    def get(name: str) -> str:
        try:
            return _require(name)
        except ValueError:
            missing.append(name)
            return ""

    alpaca_api_key = get("ALPACA_API_KEY")
    alpaca_secret_key = get("ALPACA_SECRET_KEY")
    alpaca_base_url = get("ALPACA_BASE_URL")
    database_url = get("DATABASE_URL")
    redis_url = get("REDIS_URL")

    # Optional — present but empty is treated as absent.
    discord_webhook_url: str | None = os.getenv("DISCORD_WEBHOOK_URL") or None

    if missing:
        bullet_list = "\n  - ".join(missing)
        raise ValueError(
            f"The following required environment variables are not set:\n"
            f"  - {bullet_list}\n\n"
            f"Copy .env.example to .env and fill in the missing values."
        )

    return Settings(
        alpaca_api_key=alpaca_api_key,
        alpaca_secret_key=alpaca_secret_key,
        alpaca_base_url=alpaca_base_url,
        database_url=database_url,
        redis_url=redis_url,
        discord_webhook_url=discord_webhook_url,
    )


settings: Settings = _load_settings()
