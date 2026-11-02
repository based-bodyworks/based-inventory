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
import time

import requests

logger = logging.getLogger(__name__)

_REFRESH_URL = "https://public-api.shiphero.com/auth/refresh"
_REFRESH_RETRY_DELAYS = (5, 15, 30)


def refresh_access_token(refresh_token: str, timeout: int = 15) -> str:
    """Exchange a ShipHero refresh token for a fresh access token.

    Temporary network/provider failures get four attempts over at most
    50 seconds of backoff. A non-JSON 403 can come from an upstream proxy;
    a JSON 403 is an authentication rejection and fails immediately.
    Errors never include provider response bodies or transport details.
    """
    attempts = len(_REFRESH_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            response = requests.post(
                _REFRESH_URL,
                json={"refresh_token": refresh_token},
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout):
            reason = "network failure"
            retryable = True
        else:
            try:
                payload = response.json()
                is_json = True
            except ValueError:
                payload = None
                is_json = False

            status = response.status_code
            if status == 200 and is_json:
                token = payload.get("access_token") if isinstance(payload, dict) else None
                if not isinstance(token, str) or not token.strip():
                    raise RuntimeError("ShipHero refresh response missing or invalid access_token")
                return token

            reason = f"HTTP {status}" + (" (non-JSON response)" if not is_json else "")
            retryable = (
                status in (408, 429)
                or 500 <= status <= 599
                or (status in (200, 403) and not is_json)
            )

        if not retryable:
            raise RuntimeError(f"ShipHero token refresh failed: {reason}") from None
        if attempt == attempts - 1:
            raise RuntimeError(
                f"ShipHero token refresh failed: {reason} after {attempts} attempts"
            ) from None

        delay = _REFRESH_RETRY_DELAYS[attempt]
        logger.warning(
            "ShipHero token refresh temporary failure: %s; retrying in %ds (attempt %d/%d)",
            reason,
            delay,
            attempt + 1,
            attempts,
        )
        time.sleep(delay)

    raise RuntimeError("ShipHero token refresh attempts exhausted")


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
