from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter


class FakeTime:
    def __init__(self):
        self.now = 1_700_000_000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def timer():
    return FakeTime()


@pytest.fixture
def limiter(tmp_path, timer):
    return RateLimiter(
        tmp_path / "rates.sqlite3",
        interval_seconds=1,
        max_wait_seconds=30,
        clock=timer.clock,
        monotonic=timer.clock,
        sleeper=timer.sleep,
    )


def test_throttling_retries_and_captures_only_success(limiter, timer):
    calls = []

    def handler(request):
        calls.append(timer.now)
        return (
            httpx.Response(429, headers={"retry-after": "1"})
            if len(calls) == 1
            else httpx.Response(200, json={"ok": True})
        )

    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client:
        assert client.get("https://www.nomisweb.co.uk/test").json() == {"ok": True}
        assert len(client.resources) == 1
        assert client.attempts == 2
    assert calls == [1_700_000_000, 1_700_000_001]


def test_limits_and_provider_allowlist(limiter):
    with HttpClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"1234")),
        max_bytes=3,
        rate_limiter=limiter,
    ) as client:
        with pytest.raises(RetrievalError, match="allowlist"):
            client.get("https://example.com/private")
        with pytest.raises(RetrievalError, match="exceeds"):
            client.get("https://www.nomisweb.co.uk/test")


def test_long_retry_after_is_not_capped_or_retried_early(limiter, timer, tmp_path):
    calls = []

    def handler(request):
        calls.append(timer.now)
        return httpx.Response(429, headers={"retry-after": "120"})

    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client:
        with pytest.raises(RetrievalError, match="required wait 120.000s"):
            client.get("https://www.nomisweb.co.uk/test")
        assert client.attempts == 1
    assert timer.sleeps == []

    # A new client/database connection cannot bypass the provider's cooldown.
    other = RateLimiter(
        tmp_path / "rates.sqlite3",
        interval_seconds=1,
        max_wait_seconds=30,
        clock=timer.clock,
        monotonic=timer.clock,
        sleeper=timer.sleep,
    )
    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=other) as client:
        with pytest.raises(RetrievalError, match="cooldown remains persisted"):
            client.get("https://www.nomisweb.co.uk/test")
        assert client.attempts == 0
    assert len(calls) == 1


def test_final_429_persists_cooldown(limiter, timer):
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "120"}))
    with HttpClient(transport=transport, retries=0, rate_limiter=limiter) as client:
        with pytest.raises(RetrievalError, match="429"):
            client.get("https://api.stats.gov.wales/test")
        assert client.attempts == 1
    with pytest.raises(RetrievalError, match="required wait 120.000s"):
        limiter.acquire("api.stats.gov.wales")
    assert timer.sleeps == []


def test_retry_after_http_date(limiter, timer):
    calls = []
    after = format_datetime(datetime.fromtimestamp(timer.now + 8, UTC), usegmt=True)

    def handler(request):
        calls.append(timer.now)
        return (
            httpx.Response(429, headers={"retry-after": after})
            if len(calls) == 1
            else httpx.Response(200, json={"ok": True})
        )

    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client:
        client.get("https://api.stats.gov.wales/test")
    assert calls[1] - calls[0] == 8
    assert timer.sleeps == [8]


def test_wait_budget_is_shared_by_all_retries(limiter, timer):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "20"})

    with (
        HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client,
        pytest.raises(RetrievalError, match="remaining wait budget 10.000s"),
    ):
        client.get("https://www.nomisweb.co.uk/test")
    assert calls == 2
    assert timer.sleeps == [20]


def test_transient_faults_are_retried_through_the_same_limiter(limiter, timer):
    calls = []

    def handler(request):
        calls.append(timer.now)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client:
        assert client.get("https://www.nomisweb.co.uk/test").json() == {"ok": True}
    assert calls == [1_700_000_000, 1_700_000_001, 1_700_000_002]


def test_default_client_uses_persisted_throttle(monkeypatch, tmp_path):
    monkeypatch.setenv("SSDU_RATE_DB", str(tmp_path / "default.sqlite3"))
    monkeypatch.setenv("SSDU_RATE_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("SSDU_RATE_MAX_WAIT_SECONDS", "0")
    calls = []
    transport = httpx.MockTransport(
        lambda request: calls.append(request.url) or httpx.Response(200, content=b"ok")
    )
    with HttpClient(transport=transport) as client:
        client.get("https://www.nomisweb.co.uk/first")
    with HttpClient(transport=transport) as client:
        with pytest.raises(RetrievalError, match="rate limit"):
            client.get("https://www.nomisweb.co.uk/second")
        # Pacing is per provider host, not across unrelated providers.
        client.get("https://api.stats.gov.wales/test")
    assert len(calls) == 2


def test_provider_request_budget_prevents_additional_attempts(limiter):
    calls = []
    transport = httpx.MockTransport(
        lambda request: calls.append(request.url) or httpx.Response(429)
    )
    with (
        HttpClient(transport=transport, max_requests=1, rate_limiter=limiter) as client,
        pytest.raises(RetrievalError, match="request budget"),
    ):
        client.get("https://www.nomisweb.co.uk/test")
    assert len(calls) == 1


def test_stream_closes_before_slot_release_between_retries(limiter, monkeypatch):
    events = []
    original_slot = limiter.slot

    @contextmanager
    def tracked_slot(*args, **kwargs):
        with original_slot(*args, **kwargs):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

    class TrackedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"ok"

        def close(self):
            events.append("close")

    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429 if calls == 1 else 200, stream=TrackedStream())

    monkeypatch.setattr(limiter, "slot", tracked_slot)
    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client:
        assert client.get("https://www.nomisweb.co.uk/test").body == b"ok"
    assert events == ["enter", "close", "exit", "enter", "close", "exit"]


def test_inflight_wait_budget_blocks_network_attempts(limiter, timer, tmp_path):
    other = RateLimiter(
        tmp_path / "rates.sqlite3",
        interval_seconds=0,
        max_wait_seconds=0.2,
        clock=timer.clock,
        monotonic=timer.clock,
        sleeper=timer.sleep,
    )
    calls = []
    transport = httpx.MockTransport(
        lambda request: calls.append(request.url) or httpx.Response(200, content=b"ok")
    )
    with (
        limiter.slot("www.nomisweb.co.uk"),
        HttpClient(transport=transport, rate_limiter=other) as client,
        pytest.raises(RetrievalError, match="one request is already in flight"),
    ):
        client.get("https://www.nomisweb.co.uk/test")
    assert calls == []
    assert all(seconds <= 0.05 for seconds in timer.sleeps)
    assert sum(timer.sleeps) == pytest.approx(0.2, abs=1e-6)
