"""Bounded, durable capture of service metadata graphs, without data retrieval.

Plans are local immutable descriptions. Each adapter-generated operation and its
HTTP evidence is checkpointed independently, so a worker may yield after any task
and restart without losing budgets or repeating completed requests.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from ssdu.connector_cache import CachedClient, _atomic, _canonical
from ssdu.http import HttpClient, ProviderRetry, Resource
from ssdu.metadata_connectors import REGISTRY, get_metadata_connector
from ssdu.metadata_graph import validate_entity, validate_link
from ssdu.metadata_graph_store import MetadataGraphStore
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter, RateLimitWait

DEFAULT_BUDGET = {
    "max_requests": 1000,
    "max_total_bytes": 100_000_000,
    "max_response_bytes": 25_000_000,
    "max_parse_bytes": 100_000_000,
    "max_attempts_per_task": 3,
}
DEFAULT_SPEC = {
    "page_size": 50,
    "max_pages": 1000,
    "max_tasks": 10_000,
    "max_entities": 1_000_000,
}
TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
KINDS = (
    "study",
    "dataset",
    "release",
    "table",
    "instrument",
    "variable",
    "question",
    "code_list",
    "code",
)


class GraphBudgetExceeded(RetrievalError):
    """An explicit campaign bound was reached; retained results remain usable."""


class GraphJobStopped(RetrievalError):
    """A cancellation was observed at a task or request boundary."""


class GraphAttemptsExceeded(RetrievalError):
    """An operation consumed its durable retry allowance, including crashes."""


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _task_id(value):
    # Pagination history is a guard, not a new remote operation identity.
    return _hash(
        {
            key: item
            for key, item in value.items()
            if key not in {"seen_page_hashes", "expected_total"}
        }
    )


def _implementation():
    root = Path(__file__).parent
    return _hash(
        {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "metadata_graph_capture.py",
                "metadata_graph.py",
                "metadata_connectors.py",
                "adapters/metadataworks.py",
            )
            if (root / name).exists()
        }
    )


def _signed(value, field):
    return {field: _hash(value), **value}


def _verified(value, field):
    if not isinstance(value, dict) or value.get(field) != _hash(
        {key: item for key, item in value.items() if key != field}
    ):
        raise RetrievalError("Metadata graph checkpoint checksum differs")
    return value


def _identity(value, *, job=False):
    pattern = r"job_[a-f0-9]{32}" if job else r"[a-f0-9]{64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid metadata graph identity")
    return value


def _positive(value, name, ceiling):
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
    return value


def _task(value, backend):
    if not isinstance(value, dict) or len(_canonical(value)) > 1_000_000:
        raise ValueError("Metadata operation must be a bounded JSON object")
    if any(key in value for key in ("url", "host", "headers", "method")):
        raise ValueError("Metadata operations cannot supply remote URLs or headers")
    allowed = {
        "kind",
        "native_id",
        "origin",
        "version",
        "page_number",
        "page_size",
        "dataset_id",
        "table_id",
        "seen_page_hashes",
        "expected_total",
    }
    if set(value) - allowed or value.get("kind") not in {
        "inventory",
        "dataset",
        "structure",
        "dictionary",
        "element",
    }:
        raise ValueError("Unknown metadata operation")
    if "page_number" in value:
        number = value["page_number"]
        minimum = 1 if value["kind"] == "inventory" else 0
        if type(number) is not int or not minimum <= number <= 1_000_000:
            raise ValueError("Invalid metadata page number")
    if "page_size" in value:
        _positive(value["page_size"], "page_size", 1000)
    for key, item in value.items():
        if (
            key not in {"page_number", "page_size", "seen_page_hashes", "expected_total"}
            and item is not None
            and (not isinstance(item, str) or not item or len(item) > 2000)
        ):
            raise ValueError("Invalid metadata operation identifier")
    if "expected_total" in value and (
        type(value["expected_total"]) is not int or not 0 <= value["expected_total"] <= 10**12
    ):
        raise ValueError("Invalid metadata pagination denominator")
    if "seen_page_hashes" in value:
        hashes = value["seen_page_hashes"]
        if not isinstance(hashes, list) or len(hashes) > 10_000:
            raise ValueError("Invalid metadata pagination history")
        for digest in hashes:
            _identity(digest)
    validator = getattr(backend, "validate_task", None)
    if validator:
        validated = validator(value)
        if validated is not None:
            value = validated
    return json.loads(_canonical(value))


class MetadataGraphCapture:
    """Capture public catalogue entities using a metadata-only connector.

    ``run_job(max_tasks=N)`` yields after N operations; it does not change the
    campaign's hard bounds. Rate-limit waits are returned as ``waiting`` with an
    absolute ``retry_at``. Calling the worker before that time performs no HTTP.
    """

    def __init__(self, root="data", *, clock=None, client_factory=None, registry=REGISTRY):
        value = root if isinstance(root, (str, os.PathLike)) else root.root
        self.store_root = Path(value).expanduser().resolve()
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.root = self.store_root / "metadata-graph-capture"
        self.root.mkdir(exist_ok=True)
        (self.root / "checkpoints").mkdir(exist_ok=True)
        self.path = self.root / "jobs.sqlite3"
        self.clock = clock or time.time
        self.client_factory = client_factory
        self.registry = registry
        self.snapshots = MetadataGraphStore(self.store_root)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, document TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL, error TEXT, result TEXT,
                    UNIQUE(job_id, task_id));
                CREATE TABLE IF NOT EXISTS entity_claims (
                    job_id TEXT NOT NULL, entity_id TEXT NOT NULL,
                    document TEXT NOT NULL, UNIQUE(job_id, entity_id));
                CREATE INDEX IF NOT EXISTS tasks_job_seq ON tasks(job_id, seq);
                CREATE INDEX IF NOT EXISTS tasks_job_status_seq ON tasks(job_id, status, seq);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _save(db, job):
        db.execute(
            "UPDATE jobs SET document=? WHERE job_id=?",
            (_canonical(job).decode(), job["job_id"]),
        )

    def plan_capture(self, spec, *, offline=False, refresh=False, budget=None):
        if type(offline) is not bool or type(refresh) is not bool or (offline and refresh):
            raise ValueError("Offline captures cannot refresh providers")
        if not isinstance(spec, dict) or set(spec) - {"connector", *DEFAULT_SPEC}:
            raise ValueError("Unknown metadata graph capture option")
        registration = get_metadata_connector(spec.get("connector"), registry=self.registry)
        normalized = {**DEFAULT_SPEC, **spec, "connector": registration.id}
        for name, ceiling in {
            "page_size": 1000,
            "max_pages": 1_000_000,
            "max_tasks": 1_000_000,
            "max_entities": 2_000_000,
        }.items():
            _positive(normalized[name], name, ceiling)
        if budget is not None and (
            not isinstance(budget, dict) or set(budget) - set(DEFAULT_BUDGET)
        ):
            raise ValueError("Unknown metadata graph budget")
        limits = {**DEFAULT_BUDGET, **(budget or {})}
        for name, limit in limits.items():
            ceiling = 25_000_000 if name == "max_response_bytes" else 10_000_000_000
            _positive(limit, name, ceiling)
        initial = [
            _task(item, registration.backend())
            for item in registration.backend().initial_tasks(page_size=normalized["page_size"])
        ]
        content = {
            "schema_version": 1,
            "kind": "metadata_graph_capture",
            "spec": normalized,
            "budget": limits,
            "offline": offline,
            "refresh": refresh,
            "connector_fingerprint": registration.fingerprint,
            "implementation": _implementation(),
            "initial_tasks": initial,
            "ready": True,
            "provider_access_required": not offline,
            "warnings": [
                "Planning performs no HTTP; catalogue size and capture cost are unknown.",
                "Completeness describes this public service catalogue, not all provider holdings.",
                "Variables, questions and codes are captured where published; absence is not completeness.",
                "An observed traversal is not an atomic upstream snapshot.",
            ],
        }
        plan = _signed(content, "plan_id")
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO plans VALUES (?,?)",
                (plan["plan_id"], _canonical(plan).decode()),
            )
        return plan

    def get_plan(self, plan_id):
        _identity(plan_id)
        with self._db() as db:
            row = db.execute("SELECT document FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        plan = _verified(json.loads(row[0]), "plan_id")
        if plan["plan_id"] != plan_id:
            raise RetrievalError("Metadata graph plan identity differs")
        return plan

    def submit_plan(self, plan_id, idempotency_key):
        plan = self.get_plan(plan_id)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValueError("Idempotency keys require 1 to 200 characters")
        registration = get_metadata_connector(plan["spec"]["connector"], registry=self.registry)
        if (
            registration.fingerprint != plan["connector_fingerprint"]
            or plan["implementation"] != _implementation()
        ):
            raise ValueError("Metadata connector changed; create a new plan")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job_id,plan_id FROM jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row:
                if row["plan_id"] != plan_id:
                    raise ValueError("Idempotency key already refers to another plan")
                job_id = row["job_id"]
            else:
                job_id, now = "job_" + uuid.uuid4().hex, self.clock()
                job = {
                    "job_id": job_id,
                    "plan_id": plan_id,
                    "status": "queued",
                    "created_at": now,
                    "updated_at": now,
                    "retry_at": None,
                    "requests": 0,
                    "bytes": 0,
                    "parse_bytes": 0,
                    "cancel_requested": False,
                    "error": None,
                    "result": None,
                    "bounds": [],
                    "warnings": [],
                    "pages_scheduled": sum("page_number" in task for task in plan["initial_tasks"]),
                    "entities_captured": 0,
                    "tasks_scheduled": len({_task_id(task) for task in plan["initial_tasks"]}),
                }
                db.execute(
                    "INSERT INTO jobs VALUES (?,?,?,?)",
                    (job_id, plan_id, idempotency_key, _canonical(job).decode()),
                )
                for task in plan["initial_tasks"]:
                    db.execute(
                        "INSERT OR IGNORE INTO tasks(job_id,task_id,document,status) VALUES(?,?,?,?)",
                        (job_id, _task_id(task), _canonical(task).decode(), "pending"),
                    )
        return self.get_job(job_id)

    def get_job(self, job_id):
        job = self._job_document(job_id)
        with self._db() as db:
            counts = dict(
                db.execute(
                    "SELECT status,count(*) FROM tasks WHERE job_id=? GROUP BY status", (job_id,)
                ).fetchall()
            )
        job["tasks"] = {"total": sum(counts.values()), **counts}
        return job

    def _job_document(self, job_id):
        """Constant-size worker state; detailed task counts are a reader operation."""
        _identity(job_id, job=True)
        with self._db() as db:
            row = db.execute("SELECT document FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return json.loads(row[0])

    def list_jobs(self, *, limit=20, offset=0):
        _positive(limit, "limit", 1000)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        with self._db() as db:
            rows = db.execute(
                "SELECT job_id FROM jobs ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        return {
            "items": [self.get_job(row[0]) for row in rows],
            "total": total,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def _update(self, job_id, **values):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = json.loads(row[0])
            job.update(values, updated_at=self.clock())
            self._save(db, job)

    def cancel_job(self, job_id):
        job = self.get_job(job_id)
        if job["status"] not in TERMINAL:
            self._update(job_id, cancel_requested=True, status="cancelled")
        return self.get_job(job_id)

    def retry_job(self, job_id):
        job = self.get_job(job_id)
        if job["status"] not in {"failed", "waiting", "partial"} or job["bounds"]:
            raise ValueError("Only failed or waiting captures can be retried")
        plan = self.get_plan(job["plan_id"])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE tasks SET status='pending',error=NULL WHERE job_id=? "
                "AND status='failed' AND attempts<?",
                (job_id, plan["budget"]["max_attempts_per_task"]),
            )
        self._update(job_id, status="queued", error=None, result=None, cache_seeded=False)
        return self.get_job(job_id)

    def _consume(
        self,
        job_id,
        budget,
        *,
        requests=0,
        byte_count=0,
        parse_bytes=0,
        task_id=None,
        start_attempt=False,
    ):
        exceeded = None
        stopped = False
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            job = json.loads(row[0])
            stopped = job["cancel_requested"] or job["status"] != "running"
            if stopped:
                if not byte_count:
                    raise GraphJobStopped("Metadata graph capture cancelled")
                # The stream has already delivered these bytes, even if a
                # cancellation arrived while the provider was sending them.
                job["bytes"] += byte_count
                job["updated_at"] = self.clock()
                self._save(db, job)
            else:
                self._consume_active(
                    db, job, budget, requests, byte_count, parse_bytes, task_id, start_attempt
                )
            if job["bytes"] > budget["max_total_bytes"]:
                exceeded = "download byte budget exhausted"
        if stopped:
            raise GraphJobStopped("Metadata graph capture cancelled")
        if exceeded:
            raise GraphBudgetExceeded(exceeded)

    def _consume_active(
        self, db, job, budget, requests, byte_count, parse_bytes, task_id, start_attempt
    ):
        if job["requests"] + requests > budget["max_requests"]:
            raise GraphBudgetExceeded("request budget exhausted")
        if job["parse_bytes"] + parse_bytes > budget["max_parse_bytes"]:
            raise GraphBudgetExceeded("JSON parse budget exhausted")
        if start_attempt:
            row = db.execute(
                "SELECT attempts FROM tasks WHERE job_id=? AND task_id=?", (job["job_id"], task_id)
            ).fetchone()
            if row is None or row[0] >= budget["max_attempts_per_task"]:
                raise GraphAttemptsExceeded("Metadata operation retry allowance exhausted")
            db.execute(
                "UPDATE tasks SET attempts=attempts+1 WHERE job_id=? AND task_id=?",
                (job["job_id"], task_id),
            )
        job["requests"] += requests
        job["bytes"] += byte_count
        job["parse_bytes"] += parse_bytes
        job["updated_at"] = self.clock()
        self._save(db, job)

    def _operation(self, job_id, task, plan, registration, backend):
        root = self.root / "checkpoints" / job_id / _hash(task)
        root.mkdir(parents=True, exist_ok=True)
        receipt = root / "result.json"
        if receipt.exists():
            value = _verified(json.loads(receipt.read_bytes()), "receipt_id")
            if value["task"] != task:
                raise RetrievalError("Metadata operation identity differs")
            resources = [_read_resource(root, item) for item in value["resources"]]
            return value["result"], resources
        budget = plan["budget"]
        job = self._job_document(job_id)
        transport = None
        if not plan["offline"]:
            charged = False
            response_bytes = 0

            def before_attempt(*_):
                nonlocal charged, response_bytes
                self._consume(
                    job_id, budget, requests=1, task_id=_task_id(task), start_attempt=not charged
                )
                charged = True
                response_bytes = 0

            def on_bytes(count):
                nonlocal response_bytes
                response_bytes += count
                self._consume(job_id, budget, byte_count=count)
                if response_bytes > budget["max_response_bytes"]:
                    raise GraphBudgetExceeded("response byte bound reached")

            kwargs = {
                "max_requests": max(1, budget["max_requests"] - job["requests"]),
                "max_total_bytes": max(1, budget["max_total_bytes"] - job["bytes"]),
                "max_bytes": budget["max_response_bytes"],
                "retries": 0,
                "before_attempt": before_attempt,
                "on_bytes": on_bytes,
                "rate_limiter": RateLimiter(max_wait_seconds=0, clock=self.clock),
                "allowed_hosts": registration.allowed_hosts,
                "headers": dict(registration.headers),
            }
            transport = (
                self.client_factory(**kwargs) if self.client_factory else HttpClient(**kwargs)
            )
        with CachedClient(
            self.store_root,
            client=transport,
            offline=plan["offline"],
            refresh=plan["refresh"],
            allowed_hosts=registration.allowed_hosts,
            close_client=True,
            clock=self.clock,
        ) as cache:
            client = _CheckpointClient(
                root, cache, lambda count: self._consume(job_id, budget, parse_bytes=count)
            )
            with cache.scope(registration.cache_scope):
                result = backend.fetch(task, client)
            if not isinstance(result, dict):
                raise RetrievalError("Malformed metadata adapter result")
            result["entities"] = [validate_entity(entity) for entity in result.get("entities", [])]
            result["links"] = [validate_link(link) for link in result.get("links", [])]
            value = _signed(
                {"task": task, "result": result, "resources": client.evidence}, "receipt_id"
            )
            _atomic(receipt, _canonical(value))
            return result, client.resources

    def run_job(self, job_id, *, max_tasks=None):
        job = self.get_job(job_id)
        if max_tasks is not None:
            _positive(max_tasks, "max_tasks", 1_000_000)
        if job["status"] in TERMINAL:
            return job
        lock_path = self.root / f"{job_id}.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.get_job(job_id)
            try:
                return self._run_locked(job_id, max_tasks)
            except GraphJobStopped:
                self._update(job_id, status="cancelled")
                return self.get_job(job_id)
            except (RetrievalError, ValueError, KeyError, TypeError) as exc:
                self._update(job_id, status="failed", error=str(exc))
                return self.get_job(job_id)

    def _run_locked(self, job_id, max_tasks):
        job = self._job_document(job_id)
        if job["status"] in TERMINAL:
            return self.get_job(job_id)
        if job["retry_at"] is not None and self.clock() < job["retry_at"]:
            return self.get_job(job_id)
        plan = self.get_plan(job["plan_id"])
        registration = get_metadata_connector(plan["spec"]["connector"], registry=self.registry)
        if (
            registration.fingerprint != plan["connector_fingerprint"]
            or plan["implementation"] != _implementation()
        ):
            self._update(job_id, status="failed", error="Metadata connector changed; replan")
            return self.get_job(job_id)
        backend = registration.backend()
        if plan["offline"] and not job.get("cache_seeded"):
            self._seed_cache()
            self._update(job_id, cache_seeded=True)
        self._update(job_id, status="running", retry_at=None, error=None)
        completed = 0
        while max_tasks is None or completed < max_tasks:
            job = self._job_document(job_id)
            if job["cancel_requested"]:
                self._update(job_id, status="cancelled")
                return self.get_job(job_id)
            with self._db() as db:
                row = db.execute(
                    "SELECT * FROM tasks WHERE seq=(SELECT min(seq) FROM ("
                    "SELECT min(seq) AS seq FROM tasks WHERE job_id=? AND status='pending' "
                    "UNION ALL SELECT min(seq) AS seq FROM tasks WHERE job_id=? AND status='running' "
                    "UNION ALL SELECT min(seq) AS seq FROM tasks WHERE job_id=? AND status='waiting'))",
                    (job_id, job_id, job_id),
                ).fetchone()
            if row is None:
                return self._finish(job_id, plan, registration)
            if row["retry_at"] is not None and self.clock() < row["retry_at"]:
                self._update(job_id, status="waiting", retry_at=row["retry_at"])
                return self.get_job(job_id)
            task = _task(json.loads(row["document"]), backend)
            if _task_id(task) != row["task_id"]:
                raise RetrievalError("Queued metadata operation checksum differs")
            try:
                with self._db() as db:
                    db.execute("UPDATE tasks SET status='running' WHERE seq=?", (row["seq"],))
                result, _ = self._operation(job_id, task, plan, registration, backend)
                self._accept(job_id, row, task, result, plan, backend)
            except (ProviderRetry, RateLimitWait) as exc:
                with self._db() as db:
                    attempts = db.execute(
                        "SELECT attempts FROM tasks WHERE seq=?", (row["seq"],)
                    ).fetchone()[0]
                exhausted = attempts >= plan["budget"]["max_attempts_per_task"]
                retry_at = float(exc.retry_at)
                if not math.isfinite(retry_at):
                    # Preserve the limiter's cooldown. A nonrepresentable
                    # deadline must not silently become a shorter wait.
                    with self._db() as db:
                        db.execute(
                            "UPDATE tasks SET status='failed',attempts=?,retry_at=NULL,error=? WHERE seq=?",
                            (attempts, f"Provider cooldown is not finite: {exc}", row["seq"]),
                        )
                    completed += 1
                    continue
                with self._db() as db:
                    db.execute(
                        "UPDATE tasks SET status=?,attempts=?,retry_at=?,error=? WHERE seq=?",
                        (
                            "failed" if exhausted else "waiting",
                            attempts,
                            retry_at,
                            str(exc),
                            row["seq"],
                        ),
                    )
                if exhausted:
                    completed += 1
                    continue
                self._update(job_id, status="waiting", retry_at=retry_at, error=str(exc))
                return self.get_job(job_id)
            except GraphBudgetExceeded as exc:
                self._bound(job_id, str(exc))
                return self._finish(job_id, plan, registration)
            except GraphJobStopped:
                self._update(job_id, status="cancelled")
                return self.get_job(job_id)
            except (RetrievalError, ValueError, KeyError, TypeError) as exc:
                with self._db() as db:
                    db.execute(
                        "UPDATE tasks SET status='failed',attempts=max(attempts,?),error=? WHERE seq=?",
                        (row["attempts"] + 1, str(exc), row["seq"]),
                    )
            completed += 1
            if self._job_document(job_id)["bounds"]:
                return self._finish(job_id, plan, registration)
        self._update(job_id, status="queued")
        return self.get_job(job_id)

    def _bound(self, job_id, reason):
        job = self._job_document(job_id)
        self._update(job_id, bounds=sorted({*job["bounds"], reason}))

    def _accept(self, job_id, row, task, result, plan, backend):
        if not isinstance(result, dict) or set(result) - {
            "entities",
            "links",
            "next_tasks",
            "coverage",
        }:
            raise RetrievalError("Malformed metadata adapter result")
        for field in ("entities", "links", "next_tasks"):
            if not isinstance(result.get(field), list):
                raise RetrievalError(f"Metadata adapter {field} must be a list")
        result["entities"] = [validate_entity(entity) for entity in result["entities"]]
        result["links"] = [validate_link(link) for link in result["links"]]
        queued = [_task(item, backend) for item in result["next_tasks"]]
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = json.loads(
                db.execute("SELECT document FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            )
            if job["cancel_requested"]:
                raise GraphJobStopped("Metadata graph capture cancelled")
            warnings, bounds = list(job["warnings"]), list(job["bounds"])
            count = job.get("tasks_scheduled")
            if count is None:  # Jobs created before this optional progress counter.
                count = db.execute(
                    "SELECT count(*) FROM tasks WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            for entity in result["entities"]:
                claim = db.execute(
                    "SELECT document FROM entity_claims WHERE job_id=? AND entity_id=?",
                    (job_id, entity["id"]),
                ).fetchone()
                existing = json.loads(claim[0]) if claim else None
                if existing is not None and existing != entity:
                    # Different source pointers on compatible repeated entities are merged at publication.
                    comparable = lambda value: {k: v for k, v in value.items() if k != "provenance"}
                    if _canonical(comparable(existing)) != _canonical(comparable(entity)):
                        raise RetrievalError(
                            f"Conflicting metadata entity identity: {entity['id']}"
                        )
                if existing is None:
                    job["entities_captured"] += 1
                    if job["entities_captured"] > plan["spec"]["max_entities"]:
                        raise GraphBudgetExceeded("entity count bound reached")
                    db.execute(
                        "INSERT INTO entity_claims VALUES(?,?,?)",
                        (job_id, entity["id"], _canonical(entity).decode()),
                    )
            for following in queued:
                task_id = _task_id(following)
                exists = db.execute(
                    "SELECT document FROM tasks WHERE job_id=? AND task_id=?", (job_id, task_id)
                ).fetchone()
                if exists:
                    if "page_number" in following and following.get("kind") == task.get("kind"):
                        warnings.append(
                            "Repeated pagination task rejected; upstream traversal may be cyclic"
                        )
                    continue
                if count >= plan["spec"]["max_tasks"]:
                    bounds.append("task count bound reached")
                    continue
                if "page_number" in following:
                    if job["pages_scheduled"] >= plan["spec"]["max_pages"]:
                        bounds.append("page count bound reached")
                        continue
                    job["pages_scheduled"] += 1
                db.execute(
                    "INSERT INTO tasks(job_id,task_id,document,status) VALUES(?,?,?,?)",
                    (job_id, task_id, _canonical(following).decode(), "pending"),
                )
                count += 1
            db.execute(
                "UPDATE tasks SET status='done',result=?,error=NULL,retry_at=NULL WHERE seq=?",
                (_canonical(result).decode(), row["seq"]),
            )
            job.pop("tasks", None)
            job.update(
                warnings=sorted(set(warnings)),
                bounds=sorted(set(bounds)),
                updated_at=self.clock(),
                tasks_scheduled=count,
            )
            self._save(db, job)

    def _seed_cache(self):
        # Imported snapshots are independently verified before their raw evidence
        # may enter the exact-request response cache.
        self.snapshots.seed_all_cache()

    def _finish(self, job_id, plan, registration):
        job = self._job_document(job_id)

        def check():
            current = self._job_document(job_id)
            if current["cancel_requested"] or current["status"] != "running":
                raise GraphJobStopped("Metadata graph capture cancelled")

        with _GraphMerge(self.root, check) as merged:
            for number, row in enumerate(self._task_rows(job_id)):
                if number % 1000 == 0:
                    check()
                task = json.loads(row["document"])
                receipt_path = self.root / "checkpoints" / job_id / _hash(task)
                if row["status"] != "done":
                    merged.failure({"task": task, "status": row["status"], "error": row["error"]})
                    for path in receipt_path.glob("request-*.json"):
                        checkpoint = _verified(json.loads(path.read_bytes()), "checkpoint_id")
                        merged.resource(receipt_path, checkpoint["resource"])
                    continue
                receipt = _verified(
                    json.loads((receipt_path / "result.json").read_bytes()), "receipt_id"
                )
                result = receipt["result"]
                if _canonical(result).decode() != row["result"]:
                    raise RetrievalError("Task result differs from immutable operation receipt")
                for entity in result["entities"]:
                    merged.entity(entity)
                for link in result["links"]:
                    merged.link(link)
                events = result.get("coverage", [])
                for event in events if isinstance(events, list) else [events]:
                    merged.coverage(event)
                for item in receipt["resources"]:
                    merged.resource(receipt_path, item)
            report = merged.report(job, registration)
            check()
            snapshot_id = self.snapshots.publish(
                merged.entities(), merged.links(), merged.resources(), report
            )
            check()
        self._update(
            job_id,
            status=report["status"],
            retry_at=None,
            result={"snapshot_id": snapshot_id, "report": report},
        )
        return self.get_job(job_id)

    def _task_rows(self, job_id):
        """Read one bounded operation at a time without holding a read lock.

        Keyset iteration uses tasks_job_seq. Closing each statement before file
        reads also lets other campaigns checkpoint and users cancel this job.
        """
        position = 0
        with self._db() as db:
            while True:
                cursor = db.execute(
                    "SELECT * FROM tasks WHERE job_id=? AND seq>? ORDER BY seq LIMIT 1",
                    (job_id, position),
                )
                row = cursor.fetchone()
                cursor.close()
                if row is None:
                    return
                position = row["seq"]
                yield row

    def report(self, snapshot_id):
        return self.snapshots.report(snapshot_id)


class _GraphMerge:
    """Disk-backed finalization; memory is bounded by one operation/response.

    Temporary rows are reconstructible from signed receipts. They are not a new
    checkpoint authority, so synchronous writes and journals are unnecessary.
    The immutable store independently verifies the streamed publication inputs.
    """

    def __init__(self, root, check):
        self.temporary = tempfile.TemporaryDirectory(prefix=".graph-merge-", dir=root)
        self.db = sqlite3.connect(Path(self.temporary.name) / "merge.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.check = check
        self.db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-2048;
            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE links (
                link_id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
                relation TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE resources (
                resource_id TEXT PRIMARY KEY, checkpoint_root TEXT NOT NULL,
                document TEXT NOT NULL);
            CREATE TABLE coverage (
                seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                status TEXT NOT NULL, expected INTEGER, document TEXT NOT NULL);
            CREATE TABLE latest_coverage (
                kind TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL,
                expected INTEGER, document TEXT NOT NULL, PRIMARY KEY(kind,scope));
            CREATE TABLE failures (seq INTEGER PRIMARY KEY, document TEXT NOT NULL);
            CREATE VIEW unresolved AS
                SELECT l.source,l.target,l.relation FROM links l
                LEFT JOIN entities source_entity ON source_entity.entity_id=l.source
                LEFT JOIN entities target_entity ON target_entity.entity_id=l.target
                WHERE source_entity.entity_id IS NULL OR target_entity.entity_id IS NULL;
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.db.close()
        finally:
            self.temporary.cleanup()

    def _merged(self, table, key, document):
        field = "entity_id" if table == "entities" else "link_id"
        prior = self.db.execute(f"SELECT document FROM {table} WHERE {field}=?", (key,)).fetchone()
        if prior:
            previous = json.loads(prior[0])
            comparable = lambda value: {
                key: item for key, item in value.items() if key != "provenance"
            }
            if _canonical(comparable(previous)) != _canonical(comparable(document)):
                raise RetrievalError("Conflicting retained metadata assertion")
            document = {
                **document,
                "provenance": _unique([*previous["provenance"], *document["provenance"]]),
            }
        return _canonical(document).decode()

    def entity(self, entity):
        document = self._merged("entities", entity["id"], entity)
        self.db.execute(
            "INSERT OR REPLACE INTO entities VALUES(?,?,?)",
            (entity["id"], entity["kind"], document),
        )

    def link(self, link):
        key = _hash([link["source"], link["target"], link["relation"]])
        document = self._merged("links", key, link)
        self.db.execute(
            "INSERT OR REPLACE INTO links VALUES(?,?,?,?,?)",
            (key, link["source"], link["target"], link["relation"], document),
        )

    def resource(self, root, document):
        # Check every checkpoint, including duplicate evidence copies. Retain
        # only its descriptor; raw response bodies never accumulate in Python.
        _read_resource(root, document)
        self.db.execute(
            "INSERT OR REPLACE INTO resources VALUES(?,?,?)",
            (_hash(document), str(root), _canonical(document).decode()),
        )

    def coverage(self, event):
        if (
            not isinstance(event, dict)
            or any(not isinstance(event.get(key), str) for key in ("kind", "status"))
            or not isinstance(event.get("scope", ""), str)
        ):
            raise RetrievalError("Invalid metadata coverage event")
        expected = event.get("expected") if type(event.get("expected")) is int else None
        fields = (
            event["kind"],
            event.get("scope", ""),
            event["status"],
            expected,
            _canonical(event).decode(),
        )
        self.db.execute(
            "INSERT INTO coverage(kind,scope,status,expected,document) VALUES(?,?,?,?,?)", fields
        )
        self.db.execute(
            "INSERT INTO latest_coverage VALUES(?,?,?,?,?) "
            "ON CONFLICT(kind,scope) DO UPDATE SET status=excluded.status,"
            "expected=excluded.expected,document=excluded.document",
            fields,
        )

    def failure(self, value):
        self.db.execute("INSERT INTO failures(document) VALUES(?)", (_canonical(value).decode(),))

    def _rows(self, table, *, resources=False):
        columns = "checkpoint_root,document" if resources else "document"
        for index, row in enumerate(
            self.db.execute(f"SELECT {columns} FROM {table} ORDER BY rowid")
        ):
            if index % 1000 == 0:
                self.check()
            value = json.loads(row["document"])
            yield _read_resource(Path(row["checkpoint_root"]), value) if resources else value

    def entities(self):
        return self._rows("entities")

    def links(self):
        return self._rows("links")

    def resources(self):
        return self._rows("resources", resources=True)

    def report(self, job, registration):
        def count(table):
            return self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

        # A shared byte allowance bounds report size, including long source
        # identifiers/error strings. Counts remain exhaustive when samples trim.
        remaining = 1_000_000

        def sample(sql, arguments=(), *, plain=False):
            nonlocal remaining
            values = []
            for row in self.db.execute(sql, arguments):
                value = dict(row) if plain else json.loads(row["document"])
                size = len(_canonical(value)) + 1
                if len(values) >= 1000 or size > remaining:
                    break
                remaining -= size
                values.append(value)
            return values

        counts = {
            row["kind"]: row["n"]
            for row in self.db.execute("SELECT kind,count(*) AS n FROM entities GROUP BY kind")
        }
        failure_count, unresolved_count, coverage_count = (
            count("failures"),
            count("unresolved"),
            count("coverage"),
        )
        failures = sample("SELECT document FROM failures ORDER BY seq")
        unresolved = sample("SELECT * FROM unresolved ORDER BY source,target,relation", plain=True)
        coverage = sample("SELECT document FROM coverage ORDER BY seq")
        per_kind = {}
        reconciliation = []
        for kind in sorted(set(KINDS) | set(counts)):
            events = self.db.execute(
                "SELECT count(*) AS n,sum(expected) AS declared,"
                "sum(CASE WHEN status NOT IN ('exhausted','complete') THEN 1 ELSE 0 END) AS unknown "
                "FROM latest_coverage WHERE kind=?",
                (kind,),
            ).fetchone()
            missing_count = self.db.execute(
                "SELECT count(*) FROM latest_coverage WHERE kind=? "
                "AND status IN ('missing','unavailable','unsupported')",
                (kind,),
            ).fetchone()[0]
            missing = sample(
                "SELECT document FROM latest_coverage WHERE kind=? "
                "AND status IN ('missing','unavailable','unsupported') ORDER BY scope",
                (kind,),
            )
            item = {
                "captured": counts.get(kind, 0),
                "declared_total": events["declared"],
                "exhausted": bool(events["n"]) and not events["unknown"],
                "missing": missing,
                "missing_count": missing_count,
                "missing_truncated": missing_count > len(missing),
                "unknown": not events["n"] or bool(events["unknown"]),
                "bounded": bool(job["bounds"]),
            }
            if events["declared"] is not None and item["captured"] < events["declared"]:
                item.update(exhausted=False, unknown=True)
                reconciliation.append(
                    {
                        "kind": kind,
                        "declared_total": events["declared"],
                        "captured": item["captured"],
                        "reason": "Unique retained entities do not reconcile with the declared public scope total",
                    }
                )
            per_kind[kind] = item
        coverage_gaps = self.db.execute(
            "SELECT count(*) FROM coverage WHERE status IN ('partial','failed','missing','unavailable')"
        ).fetchone()[0]
        partial = bool(
            job["bounds"]
            or job["warnings"]
            or failure_count
            or unresolved_count
            or coverage_gaps
            or reconciliation
        )
        status = "partial" if partial else "succeeded"
        entity_count = sum(counts.values())
        if failure_count and not entity_count and not job["bounds"]:
            status = "failed"
        return {
            "schema_version": 1,
            "connector": registration.id,
            "catalogue_url": registration.catalogue_url,
            "scope": "observed public service catalogue; not all originating providers' holdings",
            "status": status,
            "complete": not partial and not any(item["unknown"] for item in per_kind.values()),
            "traversal_complete": not partial,
            "entity_count": entity_count,
            "link_count": count("links"),
            "entities": per_kind,
            "coverage": coverage,
            "coverage_event_count": coverage_count,
            "coverage_truncated": coverage_count > len(coverage),
            "unresolved_references": unresolved,
            "unresolved_reference_count": unresolved_count,
            "unresolved_references_truncated": unresolved_count > len(unresolved),
            "failures": failures,
            "failure_count": failure_count,
            "failures_truncated": failure_count > len(failures),
            "reconciliation_gaps": reconciliation,
            "bounds": job["bounds"],
            "warnings": job["warnings"],
            "requests": job["requests"],
            "bytes": job["bytes"],
            "parse_bytes": job["parse_bytes"],
            "sample_byte_budget": 1_000_000,
        }


def _unique(values):
    return list({_canonical(value): value for value in values}.values())


def _resource_document(resource):
    return {
        "body_sha256": hashlib.sha256(resource.body).hexdigest(),
        "url": resource.url,
        "method": resource.method,
        "request_body": resource.request_body,
        "content_type": resource.content_type,
        "headers": resource.headers,
        "cache_scope": resource.cache_scope,
        "captured_at": resource.captured_at,
        "checked_at": resource.checked_at,
    }


def _read_resource(root, document):
    digest = document["body_sha256"]
    _identity(digest)
    path = root / f"{digest}.bin"
    try:
        if path.is_symlink() or path.stat().st_size > 25_000_000:
            raise RetrievalError("Metadata operation evidence exceeds byte bound")
        body = path.read_bytes()
    except OSError as exc:
        raise RetrievalError("Metadata operation evidence is unavailable") from exc
    if hashlib.sha256(body).hexdigest() != digest:
        raise RetrievalError("Metadata operation evidence checksum differs")
    return Resource(
        body=body,
        url=document["url"],
        method=document["method"],
        request_body=document["request_body"],
        content_type=document["content_type"],
        headers=document["headers"],
        cache_scope=document["cache_scope"],
        captured_at=document["captured_at"],
        checked_at=document["checked_at"],
        cache_hit=True,
    )


class _ParsedResource:
    def __init__(self, resource, consume):
        self.resource, self.consume = resource, consume

    def __getattr__(self, name):
        return getattr(self.resource, name)

    def json(self):
        self.consume(len(self.resource.body))
        return self.resource.json()


class _CheckpointClient:
    def __init__(self, root, client, consume):
        self.root, self.client, self.consume = root, client, consume
        self.resources, self.evidence = [], []
        self.position = 0

    def get(self, url, params=None):
        return self.request("GET", url, params=params)

    def post(self, url, *, json):
        return self.request("POST", url, json=json)

    def request(self, method, url, *, params=None, json=None):
        identity = {
            "method": method,
            "url": str(httpx.Request(method, url, params=params).url),
            "request_body": json,
            "cache_scope": self.client.cache_scope,
        }
        path = self.root / f"request-{self.position}.json"
        if path.exists():
            checkpoint = _verified(__import__("json").loads(path.read_bytes()), "checkpoint_id")
            if checkpoint["request"] != identity:
                raise RetrievalError("Metadata request checkpoint identity differs")
            resource = _read_resource(self.root, checkpoint["resource"])
        else:
            resource = self.client.request(method, url, params=params, json=json)
            document = _resource_document(resource)
            _atomic(self.root / f"{document['body_sha256']}.bin", resource.body)
            _atomic(
                path,
                _canonical(_signed({"request": identity, "resource": document}, "checkpoint_id")),
            )
        self.position += 1
        self.resources.append(resource)
        self.evidence.append(_resource_document(resource))
        return _ParsedResource(resource, self.consume)
