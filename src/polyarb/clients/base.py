"""Shared async HTTP plumbing for the read-only REST clients.

Each concrete client (Gamma, CLOB, Data) sets ``service`` and ``base_url`` and calls
``_get_json``, which paces requests through the per-service token bucket and retries
retryable failures (HTTP 429, 5xx, transport errors) with jittered backoff.

Read-only: only GET is exposed. There is intentionally no POST/PUT/DELETE here.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, ClassVar, Self

import httpx

from polyarb.clients.ratelimit import ServiceLimiter, with_backoff

_DEFAULT_TIMEOUT = httpx.Timeout(20.0)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.TransportError)


class BaseHTTPClient:
    """Base for read-only REST clients. Subclasses set ``service`` and ``base_url``."""

    service: ClassVar[str]
    base_url: ClassVar[str]

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        limiter: ServiceLimiter | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self._limiter = limiter or ServiceLimiter()

    async def _get_json(
        self,
        path: str,
        *,
        endpoint: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET ``base_url + path`` as JSON, rate-limited and retried.

        ``endpoint`` selects the token bucket (e.g. "/events"); defaults to ``path``.
        """
        bucket_key = endpoint or path

        async def do_request() -> Any:
            await self._limiter.acquire(self.service, bucket_key)
            response = await self._client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()

        return await with_backoff(do_request, is_retryable=_is_retryable)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
