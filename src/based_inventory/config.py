"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Shopify (used by atc_audit job for storefront crawl + auth-token reads)
    shopify_store: str
    shopify_client_id: str
    shopify_client_secret: str
    shopify_api_version: str
    # ShipHero (canonical inventory source; used by quantity_alerts + weekly_snapshot)
    shiphero_access_token: str | None
    shiphero_refresh_token: str | None
    shiphero_api_url: str
    # Slack + Telegram + state
    slack_bot_token: str
    slack_channel: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    dry_run: bool
    env: str
    state_path: str
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        def required(name: str) -> str:
            value = os.getenv(name)
            if not value:
                raise ValueError(f"Missing required env var: {name}")
            return value

        def optional(name: str, default: str | None = None) -> str | None:
            value = os.getenv(name)
            return value if value else default

        return cls(
            shopify_store=required("SHOPIFY_STORE"),
            shopify_client_id=required("SHOPIFY_CLIENT_ID"),
            shopify_client_secret=required("SHOPIFY_CLIENT_SECRET"),
            shopify_api_version=optional("SHOPIFY_API_VERSION", "2026-01") or "2026-01",
            shiphero_access_token=optional("SHIPHERO_ACCESS_TOKEN"),
            shiphero_refresh_token=optional("SHIPHERO_REFRESH_TOKEN"),
            shiphero_api_url=optional(
                "SHIPHERO_API_URL", "https://public-api.shiphero.com/graphql"
            )
            or "https://public-api.shiphero.com/graphql",
            slack_bot_token=required("SLACK_BOT_TOKEN"),
            slack_channel=required("SLACK_CHANNEL"),
            telegram_bot_token=optional("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=optional("TELEGRAM_CHAT_ID"),
            dry_run=optional("DRY_RUN", "0") == "1",
            env=optional("ENV", "dev") or "dev",
            state_path=optional("STATE_PATH", "./data/alert-state.json")
            or "./data/alert-state.json",
            log_level=optional("LOG_LEVEL", "INFO") or "INFO",
        )
