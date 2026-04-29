"""ShipHero access-token refresh.

ShipHero's Public API has its own refresh endpoint at
`https://public-api.shiphero.com/auth/refresh`. POST a refresh_token in
JSON; response is a fresh access_token + new expires_in (~28 days).
No client_id needed (the refresh token itself binds to the account).

We don't trust whatever SHIPHERO_ACCESS_TOKEN is in env (Render env vars
get set once and may go stale). At job start, if a refresh token is
configured, we exchange it for a fresh access token. The fallback access
token from env is used only if no refresh token is available.

NOTE: The Auth0 endpoint at login.shiphero.com/oauth/token rejects the
ShipHero Public API refresh token with 401 access_denied. The
public-api.shiphero.com/auth/refresh endpoint is the correct one for
this token type.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_REFRESH_URL = "https://public-api.shiphero.com/auth/refresh"


def refresh_access_token(refresh_token: str, timeout: int = 15) -> str:
    """Exchange a ShipHero refresh token for a fresh access token.

    Returns the new access_token. Raises RuntimeError on any failure
    (HTTP error, missing access_token in response, network issue) so the
    calling cron job fails loudly rather than silently using stale data.
    """
    response = requests.post(
        _REFRESH_URL,
        json={"refresh_token": refresh_token},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"ShipHero token refresh failed: HTTP {response.status_code} "
            f"{response.text[:200]}"
        )
    payload = response.json()
    new_token = payload.get("access_token")
    if not new_token:
        raise RuntimeError(
            f"ShipHero refresh response missing access_token: {payload}"
        )
    return new_token


def resolve_access_token(
    refresh_token: str | None,
    fallback_access_token: str | None,
) -> str:
    """Get a usable ShipHero access token at job start.

    Priority:
    1. If refresh_token is set, refresh it (production path; resilient
       to access-token expiry).
    2. Otherwise return the fallback access token from env (dev / debug
       path; fails fast at job start if the env token is also expired).
    """
    if refresh_token:
        logger.info("ShipHero auth: refreshing access token")
        return refresh_access_token(refresh_token)

    if not fallback_access_token:
        raise RuntimeError(
            "Neither SHIPHERO_REFRESH_TOKEN nor SHIPHERO_ACCESS_TOKEN is set; "
            "cannot authenticate to ShipHero."
        )
    logger.warning(
        "ShipHero auth: no refresh token; using fallback SHIPHERO_ACCESS_TOKEN. "
        "Token may be expired."
    )
    return fallback_access_token
