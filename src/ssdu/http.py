import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from ssdu.models import RetrievalError
from ssdu.providers import allowed_hosts
from ssdu.rate_limit import RateLimiter, retry_after_seconds


class ProviderRetry(RetrievalError):
    """Retryable transport failure with an observed shared cooldown deadline."""

    def __init__(self, message, retry_at):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(frozen=True)
class Resource:
    body: bytes
    url: str
    method: str
    request_body: Any
    content_type: str
    headers: dict[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RetrievalError(f"Invalid JSON from {self.url}") from exc


class HttpClient:
    """Bounded HTTPS transport recording successful responses for provenance."""

    HOSTS = allowed_hosts()

    def __init__(
        self,
        *,
        transport=None,
        max_bytes=25_000_000,
        retries=2,
        max_requests=128,
        max_total_bytes=100_000_000,
        rate_limiter: RateLimiter | None = None,
        allowed_hosts: frozenset[str] | None = None,
        before_attempt=None,
        on_bytes=None,
    ):
        self.resources: list[Resource] = []
        self.max_bytes = max_bytes
        self.retries = retries
        self.max_requests = max_requests
        self.max_total_bytes = max_total_bytes
        self.attempts = 0
        self.total_bytes = 0
        self.before_attempt = before_attempt
        self.on_bytes = on_bytes
        self.rate_limiter = rate_limiter or RateLimiter()
        self.allowed_hosts = self.HOSTS if allowed_hosts is None else frozenset(allowed_hosts)
        if not self.allowed_hosts or any(
            not isinstance(host, str) or not host or host != host.lower() or "/" in host
            for host in self.allowed_hosts
        ):
            raise ValueError("HTTP allowlist must contain lowercase hostnames")
        self.client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": "ssdu-retrieval-poc/0.1 (bounded public-data research)"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def get(self, url: str, params: dict | None = None) -> Resource:
        return self.request("GET", url, params=params)

    def post(self, url: str, *, json: Any) -> Resource:
        return self.request("POST", url, json=json)

    def request(self, method: str, url: str, *, params=None, json=None) -> Resource:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise RetrievalError(f"Provider URL is outside the PoC allowlist: {url}")
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise RetrievalError("Provider URLs must not contain credentials or custom ports")
        deadline = self.rate_limiter.deadline()
        for attempt in range(self.retries + 1):
            if self.attempts >= self.max_requests:
                raise RetrievalError("Provider request budget exhausted")
            with self.rate_limiter.slot(parsed.hostname, deadline=deadline):
                self.rate_limiter.acquire(parsed.hostname, deadline=deadline)
                if self.before_attempt is not None:
                    self.before_attempt(method, url)
                self.attempts += 1
                try:
                    try:
                        with self.client.stream(method, url, params=params, json=json) as response:
                            if response.status_code == 429 or response.status_code >= 500:
                                delay = retry_after_seconds(
                                    response.headers.get("retry-after"),
                                    now=self.rate_limiter.clock(),
                                )
                                self.rate_limiter.defer(
                                    parsed.hostname,
                                    delay if delay is not None else min(0.5 * 2**attempt, 5.0),
                                )
                                if attempt < self.retries:
                                    continue
                            response.raise_for_status()
                            body = bytearray()
                            for chunk in response.iter_bytes():
                                body.extend(chunk)
                                self.total_bytes += len(chunk)
                                if self.on_bytes is not None:
                                    self.on_bytes(len(chunk))
                                if len(body) > self.max_bytes:
                                    raise RetrievalError(
                                        f"Response exceeds {self.max_bytes} bytes: {url}"
                                    )
                                if self.total_bytes > self.max_total_bytes:
                                    raise RetrievalError("Provider download budget exhausted")
                            resource = Resource(
                                body=bytes(body),
                                url=str(response.url),
                                method=method,
                                request_body=json,
                                content_type=response.headers.get("content-type", ""),
                                headers={
                                    k: v
                                    for k, v in response.headers.items()
                                    if k
                                    in {
                                        "etag",
                                        "last-modified",
                                        "content-type",
                                        "x-ratelimit-limit",
                                        "x-ratelimit-remaining",
                                        "x-ratelimit-credits-used",
                                        "x-ratelimit-reset",
                                    }
                                },
                            )
                            self.resources.append(resource)
                            return resource
                    finally:
                        self.rate_limiter.complete_attempt(parsed.hostname)
                except httpx.HTTPError as exc:
                    if isinstance(exc, httpx.TransportError) and attempt < self.retries:
                        self.rate_limiter.defer(parsed.hostname, min(0.5 * 2**attempt, 5.0))
                        continue
                    if isinstance(exc, httpx.TransportError) or (
                        isinstance(exc, httpx.HTTPStatusError)
                        and (exc.response.status_code == 429 or exc.response.status_code >= 500)
                    ):
                        raise ProviderRetry(
                            f"Provider request failed: {method} {url}: {exc}",
                            self.rate_limiter.next_allowed(parsed.hostname),
                        ) from exc
                    raise RetrievalError(f"Provider request failed: {method} {url}: {exc}") from exc
        raise RetrievalError(f"Provider request failed after retries: {url}")
