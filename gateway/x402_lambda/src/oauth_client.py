"""Client-credentials OAuth with deliberately small, safe warm caches."""
from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024


class OAuthUnavailable(RuntimeError):
    def __init__(self, code: str = "oauth_token_unavailable") -> None:
        super().__init__(code)
        self.code = code


class OAuthClient:
    def __init__(
        self,
        secret_reader: Callable[[], str],
        token_transport: Callable[..., Mapping[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._secret_reader = secret_reader
        self._token_transport = token_transport
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._secret: dict[str, str] | None = None
        self._token: str | None = None
        self._refresh_at = 0.0

    def authorization_header(self) -> str:
        if self._token is not None and self._clock() < self._refresh_at:
            return f"Bearer {self._token}"
        secret = self._load_secret()
        token, expires_in = self._fetch_token(secret)
        self._token = token
        self._refresh_at = self._clock() + max(0, expires_in - 30)
        return f"Bearer {token}"

    def _load_secret(self) -> dict[str, str]:
        if self._secret is not None:
            return self._secret
        try:
            raw = self._secret_reader()
            parsed = json.loads(raw)
        except Exception as exc:
            raise OAuthUnavailable("oauth_secret_invalid") from exc
        required = {"client_id", "client_secret", "token_url", "scope"}
        if (
            not isinstance(parsed, dict) or set(parsed) != required
            or any(not isinstance(parsed[name], str) or not parsed[name].strip() for name in required)
            or not _https_url(parsed["token_url"])
        ):
            raise OAuthUnavailable("oauth_secret_invalid")
        self._secret = {name: parsed[name] for name in required}
        return self._secret

    def _fetch_token(self, secret: Mapping[str, str]) -> tuple[str, int]:
        basic = base64.b64encode(
            f"{secret['client_id']}:{secret['client_secret']}".encode("utf-8")
        ).decode("ascii")
        body = urlencode({"grant_type": "client_credentials", "scope": secret["scope"]}).encode("ascii")
        try:
            response = self._token_transport(
                url=secret["token_url"],
                headers={
                    "authorization": f"Basic {basic}",
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                },
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
            status = response.get("status") if isinstance(response, Mapping) else None
            raw_body = response.get("body") if isinstance(response, Mapping) else None
            if (
                type(status) is not int or not 200 <= status <= 299
                or not isinstance(raw_body, bytes) or len(raw_body) > _MAX_TOKEN_RESPONSE_BYTES
            ):
                raise OAuthUnavailable()
            parsed = json.loads(raw_body.decode("utf-8"))
            token = parsed.get("access_token") if isinstance(parsed, dict) else None
            expires_in = parsed.get("expires_in") if isinstance(parsed, dict) else None
            token_type = parsed.get("token_type") if isinstance(parsed, dict) else None
            if (
                not isinstance(token, str) or not token or type(expires_in) is not int
                or expires_in <= 0 or not isinstance(token_type, str)
                or token_type.lower() != "bearer"
            ):
                raise OAuthUnavailable()
            return token, expires_in
        except OAuthUnavailable:
            raise
        except Exception as exc:
            raise OAuthUnavailable() from exc


def default_token_transport(*, url: str, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> dict[str, Any]:
    """Perform the only external OAuth operation; errors never expose response bodies."""
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: validated secret URL
            return {
                "status": response.status,
                "headers": dict(response.headers.items()),
                "body": response.read(_MAX_TOKEN_RESPONSE_BYTES + 1),
            }
    except HTTPError as exc:
        return {"status": exc.code, "headers": dict(exc.headers.items()) if exc.headers else {}, "body": b""}
    except (URLError, TimeoutError, OSError) as exc:
        raise OAuthUnavailable() from exc


def _https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https" and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False
