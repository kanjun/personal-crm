import os
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx
from loguru import logger
from pydantic import Field, PrivateAttr

from people_archive.errors import (
    CredentialsExpiredError,
    GatewayRateLimitedError,
    GatewayUnreachableError,
    UpstreamApiError,
)
from people_archive.interfaces import GoogleApiGatewayInterface
from people_archive.primitives import AccountEmail

# A healthy Google call through the gateway is well under a second; these are the two
# thresholds of the style guide's timeout pattern: "definitely broken" and "suspiciously slow".
_HARD_TIMEOUT_SECONDS: Final[float] = 60.0
_SLOW_CALL_WARNING_SECONDS: Final[float] = 15.0
_RATE_LIMIT_STATUSES: Final[frozenset[int]] = frozenset({403, 429})


def gateway_settings_from_environment() -> tuple[str, dict[str, str]]:
    """The gateway URL and the auth headers latchkey itself sends, read from the env the Mind app sets.

    The permissions override header is included only when supplied by the environment.
    """
    base = os.environ["LATCHKEY_GATEWAY"].rstrip("/")
    headers = {"x-latchkey-gateway-password": os.environ["LATCHKEY_GATEWAY_PASSWORD"]}
    permissions_override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if permissions_override:
        headers["x-latchkey-gateway-permissions-override"] = permissions_override
    return base, headers


class LatchkeyGateway(GoogleApiGatewayInterface):
    """Calls Google APIs through the local latchkey gateway, which injects the account's credentials.

    This speaks the gateway's HTTP protocol directly (``GET <gateway>/gateway/<full url>`` plus
    the password and permissions headers) instead of spawning ``latchkey curl`` per call, which
    is what makes a hundred-thousand-message pull feasible.
    """

    gateway_base_url: str = Field(frozen=True, description="The gateway's base URL")
    auth_headers: dict[str, str] = Field(frozen=True, description="Headers that authenticate to the gateway")
    _client: httpx.Client = PrivateAttr()

    def model_post_init(self, context: Any) -> None:
        self._client = httpx.Client(timeout=_HARD_TIMEOUT_SECONDS, headers=self.auth_headers)

    @classmethod
    def from_environment(cls) -> "LatchkeyGateway":
        base, headers = gateway_settings_from_environment()
        return cls(gateway_base_url=base, auth_headers=headers)

    def get_json(
        self, account: AccountEmail, url: str, params: Mapping[str, str] | Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params)
        full_url = f"{url}?{urllib.parse.urlencode(pairs)}" if pairs else url
        started = time.monotonic()
        try:
            response = self._client.get(
                f"{self.gateway_base_url}/gateway/{full_url}",
                headers={"x-latchkey-gateway-account": str(account)},
            )
        except httpx.HTTPError as e:
            raise GatewayUnreachableError(f"Gateway call failed for {url}: {e}") from e
        elapsed = time.monotonic() - started
        if elapsed > _SLOW_CALL_WARNING_SECONDS:
            logger.warning("Gateway call took {:.1f}s: {}", elapsed, url)
        return _parse_gateway_response(response, url)


def _parse_gateway_response(response: httpx.Response, url: str) -> dict[str, Any]:
    if response.status_code in _RATE_LIMIT_STATUSES:
        raise GatewayRateLimitedError(f"{response.status_code} from {url}: {response.text[:200]}")
    try:
        body = response.json()
    except ValueError as e:
        raise UpstreamApiError(f"Non-JSON {response.status_code} from {url}: {response.text[:200]}") from e
    # latchkey reports its own failures (expired credentials, unknown service) as a top-level
    # string `error` with `latchkeyError: true`; Google's errors are a nested object.
    if isinstance(body, dict) and body.get("latchkeyError"):
        message = str(body.get("error", ""))
        if "expired" in message.lower() or "revoked" in message.lower():
            raise CredentialsExpiredError(message)
        raise GatewayUnreachableError(message)
    if response.status_code >= 400:
        raise UpstreamApiError(f"{response.status_code} from {url}: {response.text[:300]}")
    if not isinstance(body, dict):
        raise UpstreamApiError(f"Unexpected JSON shape from {url}: {type(body).__name__}")
    return body
