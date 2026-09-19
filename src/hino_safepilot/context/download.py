"""Historical-context HTTP downloaders for TDX and CWA.

Design constraints (Phase 3C-4):

- Credentials are read from environment variables only and are never logged,
  returned, or written to any artifact. Absent credentials raise before any
  network activity, so the module is import- and construction-safe offline.
- No network call happens at import or construction time. A request is issued
  only when a download method is invoked explicitly.
- The transport is injectable. Tests pass a local fixture transport, so the
  real network is never touched during automated runs.
- Nothing here resolves the HINO timezone or claims any empirical improvement.
  Downloaders only fetch raw payloads; interpretation stays downstream and
  provisional.

NCDR is intentionally not implemented this phase (capability-only).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class MissingCredentialError(RuntimeError):
    """Raised when a required credential environment variable is absent."""


class ContextDownloadError(RuntimeError):
    """Raised when a transport call fails or returns an unusable payload."""


@dataclass(frozen=True)
class HttpRequest:
    """A fully-formed request the transport must execute verbatim.

    Headers may carry secrets, so this object must never be logged or serialized
    into any snapshot artifact.
    """

    method: str
    url: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    """Executes an HttpRequest. Injectable so tests avoid real network calls."""

    def __call__(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse: ...


def urllib_transport(request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
    """Default transport backed by the standard library (no third-party deps)."""

    url = request.url
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme != "https":
        raise ContextDownloadError("only https endpoints are permitted")
    urllib_request = urllib.request.Request(url, method=request.method)
    for key, value in request.headers.items():
        urllib_request.add_header(key, value)
    try:
        with urllib.request.urlopen(urllib_request, timeout=timeout_seconds) as response:  # noqa: S310 (https enforced above)
            return HttpResponse(status=int(response.status), body=response.read())
    except Exception as error:  # transport-level failure is normalized, never leaks the request
        raise ContextDownloadError(f"transport call failed: {type(error).__name__}") from None


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MissingCredentialError(
            f"{name} is not set; export it in the environment or .env before downloading"
        )
    return value


def _execute(transport: Transport, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
    """Run a transport call, normalizing any failure without leaking the request.

    An injected transport (or the default urllib one) may raise arbitrary
    exceptions; callers should only ever see a ContextDownloadError so that
    request headers carrying secrets are never surfaced in a traceback message.
    """

    try:
        return transport(request, timeout_seconds=timeout_seconds)
    except ContextDownloadError:
        raise
    except Exception as error:
        raise ContextDownloadError(f"transport call failed: {type(error).__name__}") from None


def _decode_json(response: HttpResponse) -> Any:
    if response.status != 200:
        raise ContextDownloadError(f"unexpected HTTP status {response.status}")
    try:
        return json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ContextDownloadError(f"response body was not valid JSON: {type(error).__name__}") from None


class TDXHistoricalDownloader:
    """Fetches TDX historical traffic payloads using OAuth2 client credentials.

    Credentials (``TDX_CLIENT_ID`` / ``TDX_CLIENT_SECRET``) are read from the
    environment only. Historical Traffic v2 requires a member subscription and
    serves data after July 2021 excluding the current day; this class does not
    verify entitlement, it only performs the request the caller asks for.
    """

    provider_name = "tdx"
    default_auth_url = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        auth_url: str | None = None,
        timeout_seconds: float = 30.0,
        client_id_env: str = "TDX_CLIENT_ID",
        client_secret_env: str = "TDX_CLIENT_SECRET",
    ):
        self._transport = transport
        self._auth_url = auth_url or self.default_auth_url
        self._timeout_seconds = timeout_seconds
        self._client_id_env = client_id_env
        self._client_secret_env = client_secret_env

    def _access_token(self) -> str:
        client_id = _require_env(self._client_id_env)
        client_secret = _require_env(self._client_secret_env)
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8")
        # The token exchange is a POST with a form body; reuse the transport by
        # embedding the body only for this call via a dedicated request path.
        token_response = self._post_form(self._auth_url, body)
        payload = _decode_json(token_response)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise ContextDownloadError("TDX token response did not contain an access_token")
        return str(token)

    def _post_form(self, url: str, body: bytes) -> HttpResponse:
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme != "https":
            raise ContextDownloadError("only https endpoints are permitted")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                return HttpResponse(status=int(response.status), body=response.read())
        except Exception as error:
            raise ContextDownloadError(f"token exchange failed: {type(error).__name__}") from None

    def download_json(self, url: str) -> Any:
        """Fetch a JSON resource, injecting a bearer token from env credentials."""

        token = self._access_token()
        request = HttpRequest(
            method="GET",
            url=url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return _decode_json(_execute(self._transport, request, timeout_seconds=self._timeout_seconds))


class CWAHistoricalDownloader:
    """Fetches CWA Open Data observation payloads using an Authorization key.

    The key (``CWA_API_KEY``) is read from the environment only. The history API
    is exposed for O-A0001-001 / O-A0002-001; O-A0003-001 is current-only.
    """

    provider_name = "cwa"
    default_base_url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        api_key_env: str = "CWA_API_KEY",
    ):
        self._transport = transport
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._api_key_env = api_key_env

    def build_url(self, dataset_id: str, query: Mapping[str, str] | None = None) -> str:
        api_key = _require_env(self._api_key_env)
        params: dict[str, str] = {"Authorization": api_key}
        if query:
            params.update(query)
        return f"{self._base_url}/{dataset_id}?{urllib.parse.urlencode(params)}"

    def download_json(self, dataset_id: str, query: Mapping[str, str] | None = None) -> Any:
        """Fetch a dataset payload; the Authorization key stays in the query string only."""

        url = self.build_url(dataset_id, query)
        request = HttpRequest(method="GET", url=url, headers={"Accept": "application/json"})
        return _decode_json(_execute(self._transport, request, timeout_seconds=self._timeout_seconds))


def build_downloader(
    provider: str,
    *,
    transport: Transport = urllib_transport,
    timeout_seconds: float = 30.0,
) -> TDXHistoricalDownloader | CWAHistoricalDownloader:
    """Factory used by CLIs/tests. NCDR is rejected as out of scope this phase."""

    normalized = provider.strip().lower()
    if normalized == "tdx":
        return TDXHistoricalDownloader(transport=transport, timeout_seconds=timeout_seconds)
    if normalized == "cwa":
        return CWAHistoricalDownloader(transport=transport, timeout_seconds=timeout_seconds)
    if normalized == "ncdr":
        raise ContextDownloadError("NCDR downloader is capability-only this phase")
    raise ContextDownloadError(f"unknown provider: {provider!r}")


__all__ = [
    "ContextDownloadError",
    "CWAHistoricalDownloader",
    "HttpRequest",
    "HttpResponse",
    "MissingCredentialError",
    "TDXHistoricalDownloader",
    "Transport",
    "build_downloader",
    "urllib_transport",
]
