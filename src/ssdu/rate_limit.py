"""Process-shared, fail-closed provider pacing and cooldowns."""

import fcntl
import hashlib
import math
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

from ssdu.models import RetrievalError


class RateLimitWait(RetrievalError):
    """The caller must yield while the shared provider cooldown remains in force."""

    def __init__(self, host, retry_at, message):
        super().__init__(message)
        self.host = host
        self.retry_at = retry_at


def retry_after_seconds(value: str | None, *, now: float) -> float | None:
    """RFC 9110 Retry-After: nonnegative seconds or an HTTP date, without a cap."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None


class RateLimiter:
    """Share request reservations, concurrency and cooldowns across clients.

    The default one-second spacing is an operator policy, not a provider quota.
    All processes on one machine must use the same database path. POSIX file
    locks additionally allow one in-flight request per host and release on
    process exit. HTTP clients also preserve the configured spacing after an
    attempt closes, so dispatch delays cannot consume the next request's gap.
    Multiple machines require a separate distributed coordinator.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        interval_seconds: float | None = None,
        max_wait_seconds: float | None = None,
        host_intervals: dict[str, float] | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.path = (
            Path(
                path
                or os.environ.get("SSDU_RATE_DB")
                or Path.home() / ".cache" / "ssdu" / "provider-rate.sqlite3"
            )
            .expanduser()
            .resolve()
        )
        self.interval_seconds = self._setting(interval_seconds, "SSDU_RATE_INTERVAL_SECONDS", 1.0)
        self.max_wait_seconds = self._setting(max_wait_seconds, "SSDU_RATE_MAX_WAIT_SECONDS", 30.0)
        self.host_intervals = {
            host.lower(): self._validate_interval(value)
            for host, value in (host_intervals or {}).items()
        }
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RetrievalError(f"Cannot initialise provider rate-limit database: {exc}") from exc
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_rate "
                "(host TEXT PRIMARY KEY, next_allowed REAL NOT NULL)"
            )

    @staticmethod
    def _validate_interval(value: float) -> float:
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Rate-limit settings must be finite, nonnegative seconds")
        return value

    @classmethod
    def _setting(cls, explicit: float | None, variable: str, default: float) -> float:
        return cls._validate_interval(
            explicit if explicit is not None else os.environ.get(variable, default)
        )

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError) as exc:
            raise RetrievalError(f"Provider rate-limit state unavailable: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def deadline(self) -> float:
        """One wait budget for a complete logical request, including its retries."""
        return self.monotonic() + self.max_wait_seconds

    def next_allowed(self, host):
        """Inspect a persisted cooldown without reserving a request or waiting."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT next_allowed FROM provider_rate WHERE host=?", (host.lower(),)
            ).fetchone()
        return row[0] if row else self.clock()

    @contextmanager
    def slot(self, host: str, *, deadline: float | None = None):
        """Hold one host's request slot; never hold a SQLite transaction here.

        Keep lock files in place: unlinking one could create competing inodes.
        OS descriptor locks recover automatically after crashes, without leases.
        """
        host = host.lower()
        deadline = self.deadline() if deadline is None else deadline
        suffix = hashlib.sha256(host.encode()).hexdigest()
        lock_path = self.path.with_name(f"{self.path.name}.{suffix}.lock")
        try:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise RetrievalError(f"Provider request lock unavailable for {host}: {exc}") from exc
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise RateLimitWait(
                            host,
                            self.clock() + 1,
                            f"Provider concurrency limit for {host}: "
                            "one request is already in flight; wait budget exhausted",
                        ) from None
                    self.sleeper(min(0.05, remaining))
                except OSError as exc:
                    raise RetrievalError(
                        f"Provider request lock unavailable for {host}: {exc}"
                    ) from exc
            yield
        finally:
            # Closing the descriptor releases flock, including exception paths.
            os.close(descriptor)

    def acquire(self, host: str, *, deadline: float | None = None) -> None:
        """Reserve an attempt atomically; its caller can be descheduled afterward."""
        host = host.lower()
        deadline = self.deadline() if deadline is None else deadline
        while True:
            with self._transaction() as connection:
                now = self.clock()
                row = connection.execute(
                    "SELECT next_allowed FROM provider_rate WHERE host = ?", (host,)
                ).fetchone()
                wait = max(0.0, row[0] - now) if row else 0.0
                if wait == 0:
                    connection.execute(
                        "INSERT INTO provider_rate(host, next_allowed) VALUES (?, ?) "
                        "ON CONFLICT(host) DO UPDATE SET next_allowed = excluded.next_allowed",
                        (host, now + self.host_intervals.get(host, self.interval_seconds)),
                    )
                    return
            remaining = max(0.0, deadline - self.monotonic())
            if wait > remaining:
                raise RateLimitWait(
                    host,
                    self.clock() + wait,
                    f"Provider rate limit for {host}: required wait {wait:.3f}s exceeds "
                    f"remaining wait budget {remaining:.3f}s; cooldown remains persisted",
                )
            # Release the SQLite lock before waiting and recheck after waking;
            # another process may have received a longer provider cooldown.
            self.sleeper(min(wait, 60.0))

    def complete_attempt(self, host: str) -> None:
        """Persist spacing after an HTTP attempt closes, before releasing its slot.

        This conservative boundary covers both a delayed dispatch and a slow
        response. Do not call it for attempts denied by a budget or cooldown.
        Existing provider Retry-After deadlines are never shortened.
        """
        host = host.lower()
        self.defer(host, self.host_intervals.get(host, self.interval_seconds))

    def defer(self, host: str, seconds: float) -> None:
        """Persist a cooldown without ever shortening another client's delay."""
        if math.isnan(seconds) or seconds < 0:
            raise ValueError("Provider cooldown must be nonnegative")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO provider_rate(host, next_allowed) VALUES (?, ?) "
                "ON CONFLICT(host) DO UPDATE SET next_allowed = "
                "MAX(provider_rate.next_allowed, excluded.next_allowed)",
                (host.lower(), self.clock() + seconds),
            )
