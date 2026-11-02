import httpx
import pytest

from people_archive.errors import (
    CredentialsExpiredError,
    GatewayRateLimitedError,
    GatewayUnreachableError,
    UpstreamApiError,
)
from people_archive.gateway import (
    _parse_gateway_response,
    gateway_settings_from_environment,
)


def _response(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request("GET", "http://gateway/gateway/https://x"))


def test_parse_gateway_response_returns_json_object() -> None:
    assert _parse_gateway_response(_response(200, '{"a": 1}'), "https://x") == {"a": 1}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, '{"error": {"code": 403, "message": "Quota exceeded"}}', GatewayRateLimitedError),
        (429, "slow down", GatewayRateLimitedError),
        (400, '{"error": "Error: Credentials for google-gmail are expired.", "latchkeyError": true}', CredentialsExpiredError),
        (400, '{"error": "No service matches URL", "latchkeyError": true}', GatewayUnreachableError),
        (404, '{"error": {"code": 404, "message": "Not Found"}}', UpstreamApiError),
        (200, "<html>not json</html>", UpstreamApiError),
        (200, "[1, 2]", UpstreamApiError),
    ],
)
def test_parse_gateway_response_maps_failures_to_specific_errors(status: int, body: str, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        _parse_gateway_response(_response(status, body), "https://x")


def test_gateway_settings_send_the_permissions_override_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://gateway:1989/")
    monkeypatch.setenv("LATCHKEY_GATEWAY_PASSWORD", "secret")
    monkeypatch.delenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", raising=False)
    base, headers = gateway_settings_from_environment()
    assert base == "http://gateway:1989"
    assert headers == {"x-latchkey-gateway-password": "secret"}

    monkeypatch.setenv("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "/perm.json")
    _base, headers = gateway_settings_from_environment()
    assert headers["x-latchkey-gateway-permissions-override"] == "/perm.json"
