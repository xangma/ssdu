import multiprocessing
import sqlite3
import time
from datetime import UTC, datetime
from email.utils import format_datetime
from itertools import pairwise

import httpx
import pytest

from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter, retry_after_seconds


def _process_acquire(path, gate, output):
    try:
        limiter = RateLimiter(path, interval_seconds=0.15, max_wait_seconds=5)
        gate.wait(5)
        limiter.acquire("www.nomisweb.co.uk")
        output.put(("ok",))
    except (RetrievalError, OSError, ValueError) as exc:
        output.put(("error", str(exc)))


def _process_request(path, gate, output, interval):
    class RecordedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"ok"

        def close(self):
            output.put(("end", multiprocessing.current_process().pid, time.monotonic()))

    def handler(request):
        output.put(("start", multiprocessing.current_process().pid, time.monotonic()))
        time.sleep(0.15)
        return httpx.Response(200, stream=RecordedStream())

    limiter = RateLimiter(path, interval_seconds=interval, max_wait_seconds=5)
    gate.wait(5)
    with HttpClient(transport=httpx.MockTransport(handler), rate_limiter=limiter) as client:
        client.get("https://www.nomisweb.co.uk/test")


def _process_hold_slot(path, ready):
    limiter = RateLimiter(path, interval_seconds=0)
    with limiter.slot("www.nomisweb.co.uk"):
        ready.set()
        time.sleep(30)


def test_concurrent_processes_share_request_spacing(tmp_path):
    """Inspect reservations inside the transaction, before caller descheduling."""
    path = tmp_path / "rates.sqlite3"
    RateLimiter(path, interval_seconds=0.15)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE reservations(next_allowed REAL NOT NULL)")
        for operation in ("INSERT", "UPDATE"):
            connection.execute(
                f"CREATE TRIGGER record_{operation.lower()} AFTER {operation} ON provider_rate "
                "BEGIN INSERT INTO reservations VALUES (NEW.next_allowed); END"
            )
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    workers = [
        context.Process(target=_process_acquire, args=(path, gate, output)) for _ in range(3)
    ]
    try:
        for worker in workers:
            worker.start()
        gate.set()
        results = [output.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
        assert all(worker.exitcode == 0 for worker in workers)
        assert all(result[0] == "ok" for result in results), results
        with sqlite3.connect(path) as connection:
            reservations = [
                row[0]
                for row in connection.execute(
                    "SELECT next_allowed FROM reservations ORDER BY next_allowed"
                )
            ]
        assert len(reservations) == len(workers)
        assert all(right - left >= 0.15 - 1e-6 for left, right in pairwise(reservations)), (
            reservations
        )
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        output.close()
        output.join_thread()


@pytest.mark.parametrize("interval", [0, 0.15])
def test_concurrent_http_processes_preserve_spacing_after_response_streams(tmp_path, interval):
    path = tmp_path / "rates.sqlite3"
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    workers = [
        context.Process(target=_process_request, args=(path, gate, output, interval))
        for _ in range(3)
    ]
    try:
        for worker in workers:
            worker.start()
        gate.set()
        events = [output.get(timeout=10) for _ in range(6)]
        for worker in workers:
            worker.join(timeout=10)
        assert all(worker.exitcode == 0 for worker in workers)
        intervals = {}
        for kind, pid, timestamp in events:
            intervals.setdefault(pid, {})[kind] = timestamp
        assert len(intervals) == 3
        ordered = sorted(intervals.values(), key=lambda item: item["start"])
        assert all(left["end"] <= right["start"] for left, right in pairwise(ordered)), ordered
        assert all(
            right["start"] - left["end"] >= interval - 1e-6 for left, right in pairwise(ordered)
        ), ordered
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        output.close()
        output.join_thread()


def test_crashed_process_releases_host_slot(tmp_path):
    path = tmp_path / "rates.sqlite3"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    worker = context.Process(target=_process_hold_slot, args=(path, ready))
    worker.start()
    try:
        assert ready.wait(10)
        other = RateLimiter(path, interval_seconds=0, max_wait_seconds=0)
        with (
            pytest.raises(RetrievalError, match="concurrency limit"),
            other.slot("www.nomisweb.co.uk"),
        ):
            pytest.fail("An active process must hold its host slot")
        worker.terminate()
        worker.join(timeout=5)
        assert not worker.is_alive()
        with other.slot("www.nomisweb.co.uk"):
            pass
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)


def test_integer_and_http_date_retry_after():
    now = 1_700_000_000.0
    after = format_datetime(datetime.fromtimestamp(now + 120, UTC), usegmt=True)
    assert retry_after_seconds("120", now=now) == 120
    assert retry_after_seconds(" 120 ", now=now) == 120
    assert retry_after_seconds(after, now=now) == 120
    assert retry_after_seconds(after, now=now + 121) == 0
    for invalid in (None, "", "-1", "1.5", "bogus", "１２"):
        assert retry_after_seconds(invalid, now=now) is None


def test_cooldowns_are_not_shortened_and_are_host_specific(tmp_path):
    now = [1_700_000_000.0]
    path = tmp_path / "rates.sqlite3"
    first = RateLimiter(path, interval_seconds=1, max_wait_seconds=0, clock=lambda: now[0])
    second = RateLimiter(path, interval_seconds=1, max_wait_seconds=0, clock=lambda: now[0])
    first.defer("www.nomisweb.co.uk", 120)
    second.defer("www.nomisweb.co.uk", 5)
    with pytest.raises(RetrievalError, match="120.000s"):
        second.acquire("www.nomisweb.co.uk")
    second.acquire("api.stats.gov.wales")
    now[0] += 120
    second.acquire("www.nomisweb.co.uk")


def test_each_sleep_is_bounded_and_cooldown_is_rechecked(tmp_path):
    now = [1_700_000_000.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(
        tmp_path / "rates.sqlite3",
        max_wait_seconds=180,
        clock=lambda: now[0],
        monotonic=lambda: now[0],
        sleeper=sleep,
    )
    limiter.defer("www.nomisweb.co.uk", 120)
    limiter.acquire("www.nomisweb.co.uk")
    assert sleeps == [60, 60]


def test_host_specific_policy_and_invalid_configuration(tmp_path):
    limiter = RateLimiter(
        tmp_path / "rates.sqlite3",
        interval_seconds=1,
        host_intervals={"www.nomisweb.co.uk": 3},
        clock=lambda: 100,
    )
    limiter.acquire("www.nomisweb.co.uk")
    with sqlite3.connect(limiter.path) as connection:
        assert connection.execute("SELECT next_allowed FROM provider_rate").fetchone()[0] == 103
    for invalid in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite, nonnegative"):
            RateLimiter(tmp_path / "bad.sqlite3", interval_seconds=invalid)


def test_corrupt_rate_state_fails_closed(tmp_path):
    path = tmp_path / "rates.sqlite3"
    path.write_bytes(b"not SQLite")
    with pytest.raises(RetrievalError, match="rate-limit state unavailable"):
        RateLimiter(path)
