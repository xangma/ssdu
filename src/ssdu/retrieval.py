"""Local-only plans and durable, checkpointed retrieval jobs for general collections."""

import fcntl
import hashlib
import json
import logging
import math
import os
import socket
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from importlib.metadata import version
from json import loads
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from ssdu.collections import Collections
from ssdu.http import HttpClient, ProviderRetry, Resource
from ssdu.models import Request, RetrievalError
from ssdu.providers import get_provider, provider_ids
from ssdu.rate_limit import RateLimitWait
from ssdu.registry import dataset_key, snapshot_descriptor
from ssdu.retrieval_models import (
    JobId,
    PlanEntry,
    PlanInput,
    RetrievalPlan,
    SubmitInput,
)
from ssdu.service import Service
from ssdu.store import Store, _atomic_json, canonical

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled"}


class JobStopped(RetrievalError):
    pass


class JobBudgetExceeded(RetrievalError):
    pass


def _implementation():
    root = Path(__file__).parent
    names = [
        "retrieval.py",
        "retrieval_models.py",
        "store.py",
        "shared.py",
        "collections.py",
        "collection_models.py",
        "service.py",
        "contracts.py",
        "dataset_contract.py",
        "normalization.py",
        "rate_limit.py",
        "http.py",
        "adapters/deprivation.py",
        "adapters/nomis.py",
        "adapters/statswales.py",
    ]
    paths = {root / name for name in names}
    for provider in provider_ids():
        paths.update(get_provider(provider).implementation_paths())
    source = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }
    source["duckdb_version"] = version("duckdb")
    return hashlib.sha256(canonical(source)).hexdigest()


def _validate_request(request):
    """Validate only what local adapter contracts establish; remote code lists stay unknown."""
    get_provider(request.provider).validate_request(request)


class RetrievalService:
    def __init__(self, root="data", *, clock=None, client_factory=None):
        self.store = root if isinstance(root, Store) else Store(root)
        self.clock = clock or time.time
        self.client_factory = client_factory
        self.path = self.store.root / "retrieval.sqlite3"
        self.root = self.store.root / "retrieval"
        self.root.mkdir(exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY, document TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                    phase TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, retry_at REAL,
                    requests INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                    result TEXT, error TEXT, worker TEXT
                );
                CREATE TABLE IF NOT EXISTS entries (
                    job_id TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,
                    snapshot_id TEXT, error TEXT, PRIMARY KEY(job_id, name)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL, kind TEXT NOT NULL, entry_name TEXT
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _tx(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _event(self, db, job_id, kind, entry=None):
        db.execute(
            "INSERT INTO events(job_id,occurred_at,kind,entry_name) VALUES(?,?,?,?)",
            (job_id, self.clock(), kind, entry),
        )

    def _cached(self, request):
        key = hashlib.sha256(canonical(request.to_dict())).hexdigest()
        snapshot_id = self.store._index().get(key)
        if snapshot_id:
            return self.store._cached_manifest(request, snapshot_id)
        manifests, _ = self.store._scan_manifests()
        matches = [m for m in manifests if m["request"] == request.to_dict()]
        return (
            self.store._cached_manifest(
                request, self.store._preferred_capture(matches)["product_id"]
            )
            if matches
            else None
        )

    def plan_retrieval(self, spec, *, offline=False, refresh=False, budget=None):
        request = PlanInput.model_validate(
            {
                "spec": spec,
                "offline": offline,
                "refresh": refresh,
                **({"budget": budget} if budget is not None else {}),
            }
        )
        entries = {}
        query_service = Service(self.store.root, index_store=False)
        for name, entry in request.spec.entries.items():
            fact = PlanEntry(action="blocked", dataset_id="unknown")
            try:
                manifest = None
                if entry.snapshot_id:
                    manifest = self.store.verify(entry.snapshot_id)
                    fact.action = "snapshot"
                else:
                    native = Request(**entry.request.model_dump())
                    fact.dataset_id = dataset_key(native.provider, native.dataset_id)
                    _validate_request(native)
                    try:
                        metadata = query_service.registry.get(fact.dataset_id)
                    except KeyError:
                        fact.warnings.append("No retained provider catalogue record is available.")
                    else:
                        fact.metadata_version = metadata["metadata_version"]
                    manifest = None if refresh else self._cached(native)
                    if manifest:
                        fact.action = "cached"
                    elif not refresh:
                        planner = getattr(
                            get_provider(native.provider).adapter(), "plan_retained", None
                        )
                        if planner is not None:
                            planner(self, native, entry.query, fact, query_service)
                    if fact.action == "blocked":
                        fact.action = "fetch"
                        fact.warnings.append(
                            "Provider dimension/code validation and current release identity require acquisition; counts and cost are unknown."
                        )
                if manifest:
                    fact.dataset_id = dataset_key(manifest["provider"], manifest["dataset_id"])
                    fact.snapshot_id = manifest["product_id"]
                    fact.metadata_version = hashlib.sha256(
                        canonical(snapshot_descriptor(self.store, manifest))
                    ).hexdigest()
                    fact.known_rows = manifest["rows"]
                    fact.selector_validation = "validated"
                    query_service.query_data(manifest["product_id"], entry.query)
                    fact.query_validation = "validated"
                    fact.warnings.append(
                        "Retained evidence is pinned; a cache hit does not establish provider freshness."
                    )
                if offline and fact.action == "fetch":
                    fact.action = "blocked"
                    fact.issues.append(
                        "Offline retrieval requires retained data; provider access would be necessary."
                    )
            except (ValueError, TypeError, KeyError, OSError, RetrievalError):
                fact.action = "blocked"
                fact.issues.append(
                    "Input, selection, query or retained evidence could not be verified; correct the entry or repair the cache."
                )
                fact.selector_validation = "invalid"
            entries[name] = fact.model_dump()
        provider_access = any(e["action"] == "fetch" for e in entries.values())
        content = {
            "schema_version": 1,
            "input": request.model_dump(),
            "implementation": _implementation(),
            "entries": entries,
            "ready": all(e["action"] != "blocked" for e in entries.values()),
            "provider_access_required": provider_access,
            "estimated_requests": None if provider_access else 0,
            "estimated_bytes": None if provider_access else 0,
        }
        plan = RetrievalPlan.model_validate(
            {"plan_id": hashlib.sha256(canonical(content)).hexdigest(), **content}
        )
        with self._tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO plans VALUES(?,?,?)",
                (plan.plan_id, canonical(plan.model_dump()).decode(), self.clock()),
            )
        return self.get_plan(plan.plan_id)

    def get_plan(self, plan_id):
        from ssdu.contracts import SnapshotId

        plan_id = TypeAdapter(SnapshotId).validate_python(plan_id)
        with self._db() as db:
            row = db.execute("SELECT document FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            raise KeyError(plan_id)
        plan = RetrievalPlan.model_validate(json.loads(row[0]))
        if plan.plan_id != plan_id:
            raise RetrievalError("Stored retrieval plan identity differs")
        return plan.model_dump()

    def submit_plan(self, plan_id, idempotency_key):
        args = SubmitInput(plan_id=plan_id, idempotency_key=idempotency_key)
        plan = RetrievalPlan.model_validate(self.get_plan(args.plan_id))
        if not plan.ready:
            raise ValueError("Plan has blocked entries")
        with self._tx() as db:
            old = db.execute(
                "SELECT job_id,plan_id FROM jobs WHERE idempotency_key=?", (args.idempotency_key,)
            ).fetchone()
            if old:
                if old["plan_id"] != args.plan_id:
                    raise ValueError("Idempotency key already refers to another plan")
                job_id = old["job_id"]
            else:
                if plan.implementation != _implementation():
                    raise ValueError("Implementation changed; create a new plan")
                job_id = "job_" + uuid.uuid4().hex
                now = self.clock()
                db.execute(
                    "INSERT INTO jobs(job_id,plan_id,idempotency_key,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                    (job_id, args.plan_id, args.idempotency_key, now, now),
                )
                db.executemany(
                    "INSERT INTO entries(job_id,name,state) VALUES(?,?,'pending')",
                    [(job_id, name) for name in plan.entries],
                )
                self._event(db, job_id, "submitted")
        return self.get_job(job_id)

    def get_job(self, job_id):
        job_id = TypeAdapter(JobId).validate_python(job_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            result = dict(row)
            entries = db.execute(
                "SELECT name,state,attempts,epoch,snapshot_id,error FROM entries WHERE job_id=? ORDER BY name",
                (job_id,),
            ).fetchall()
            events = db.execute(
                "SELECT occurred_at,kind,entry_name FROM events WHERE job_id=? ORDER BY event_id DESC LIMIT 20",
                (job_id,),
            ).fetchall()
        for key in ("result", "error", "worker"):
            result[key] = json.loads(result[key]) if result[key] else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["entries"] = {
            e["name"]: {
                k: json.loads(value) if k == "error" and value else value
                for k, value in dict(e).items()
                if k != "name"
            }
            for e in entries
        }
        result["events"] = [dict(e) for e in reversed(events)]
        result["budget"] = self.get_plan(result["plan_id"])["input"]["budget"]
        return result

    def list_jobs(self, limit=20, offset=0):
        if (
            type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= 100
            or not 0 <= offset <= 100_000
        ):
            raise ValueError("Invalid job page bounds")
        with self._db() as db:
            rows = db.execute(
                "SELECT job_id,plan_id,status,created_at,updated_at,retry_at FROM jobs ORDER BY created_at,job_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        end = offset + len(rows)
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "next_offset": end if end < total else None,
        }

    def cancel_job(self, job_id):
        TypeAdapter(JobId).validate_python(job_id)
        with self._tx() as db:
            row = db.execute("SELECT status,phase FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["status"] not in TERMINAL and row["phase"] != "finalizing":
                state = "running" if row["status"] == "running" else "cancelled"
                db.execute(
                    "UPDATE jobs SET status=?,cancel_requested=1,updated_at=? WHERE job_id=?",
                    (state, self.clock(), job_id),
                )
                self._event(db, job_id, "cancel_requested")
        return self.get_job(job_id)

    def retry_job(self, job_id):
        job = self.get_job(job_id)
        if job["status"] != "failed":
            raise ValueError("Only failed jobs can be retried")
        budget = job["budget"]
        if (
            job["requests"] >= budget["max_requests"]
            or job["bytes"] >= budget["max_total_bytes"]
            or any(
                e["state"] != "succeeded" and e["attempts"] >= budget["max_attempts"]
                for e in job["entries"].values()
            )
        ):
            raise ValueError("Job budget exhausted; create a new plan/job explicitly")
        with self._tx() as db:
            changed = db.execute(
                "UPDATE jobs SET status='queued',phase=NULL,error=NULL,retry_at=NULL,updated_at=? WHERE job_id=? AND status='failed'",
                (self.clock(), job_id),
            ).rowcount
            if not changed:
                raise ValueError("Job state changed")
            db.execute(
                "UPDATE entries SET state='pending',epoch=epoch+1,error=NULL WHERE job_id=? AND state!='succeeded'",
                (job_id,),
            )
            self._event(db, job_id, "retry_requested")
        return self.get_job(job_id)

    def _check_running(self, db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row["cancel_requested"] or row["status"] != "running":
            raise JobStopped("Job cancelled")
        return row

    def _consume(self, job_id, budget, *, requests=0, bytes_count=0):
        exhausted = False
        with self._tx() as db:
            row = self._check_running(db, job_id)
            if requests and row["requests"] + requests > budget.max_requests:
                raise JobBudgetExceeded("Request budget exhausted")
            db.execute(
                "UPDATE jobs SET requests=requests+?,bytes=bytes+?,updated_at=? WHERE job_id=?",
                (requests, bytes_count, self.clock(), job_id),
            )
            exhausted = row["bytes"] + bytes_count > budget.max_total_bytes
        if exhausted:
            raise JobBudgetExceeded("Byte budget exhausted")

    def _check_cancel(self, job_id):
        with self._db() as db:
            self._check_running(db, job_id)

    def _client(self, job_id, name, epoch, budget, *, provider=None, refresh=False):
        progress = self.get_job(job_id)
        kwargs = {
            "max_requests": max(1, budget.max_requests - progress["requests"]),
            "max_total_bytes": max(1, budget.max_total_bytes - progress["bytes"]),
            "before_attempt": lambda *_: self._consume(job_id, budget, requests=1),
            "on_bytes": lambda count: self._consume(job_id, budget, bytes_count=count),
        }
        client = self.client_factory(**kwargs) if self.client_factory else HttpClient(**kwargs)
        if provider is not None and get_provider(provider).workflow:
            from ssdu.connector_cache import CachedClient

            client = CachedClient(
                self.store.root,
                client=client,
                # An explicit retry discards a failed entry's HTTP epoch as well
                # as positional checkpoints. A cube may have rejected changed
                # data before reaching its final metadata check.
                refresh=refresh or epoch > 0,
                allowed_hosts=get_provider(provider).allowed_hosts,
                close_client=True,
            )
        return CheckpointClient(self, job_id, name, epoch, client)

    def _acquire(self, job_id, name, fact, entry, plan, epoch):
        self._check_cancel(job_id)
        receipt = self.get_job(job_id)["entries"][name]["snapshot_id"]
        if receipt:
            manifest = self.store.verify(receipt)
            if (entry.request and manifest["request"] != entry.request.model_dump()) or (
                entry.snapshot_id and receipt != entry.snapshot_id
            ):
                raise RetrievalError("Entry receipt differs from its input")
            return manifest
        if fact.action in {"snapshot", "cached"}:
            return self.store.verify(fact.snapshot_id)
        native = Request(**entry.request.model_dump())
        if fact.action == "derive":
            derive = getattr(get_provider(native.provider).adapter(), "derive_retained", None)
            if derive is None:
                raise RetrievalError("Provider has no retained derivation handler")
            return derive(self.store, native, fact)
        if not plan.input.refresh:
            cached = self._cached(native)
            if cached:
                return cached
        with self._client(
            job_id,
            name,
            epoch,
            plan.input.budget,
            provider=native.provider,
            refresh=plan.input.refresh,
        ) as client:
            return self.store.fetch(
                native, offline=plan.input.offline, refresh=plan.input.refresh, client=client
            )

    def _retry_at(self, error, attempts, budget):
        if isinstance(error, RateLimitWait):
            return max(self.clock() + budget.retry_delay_seconds, error.retry_at)
        if isinstance(error, ProviderRetry):
            return max(
                error.retry_at,
                self.clock() + budget.retry_delay_seconds * (2 ** (attempts - 1)),
            )
        cause = error.__cause__
        if isinstance(cause, httpx.TransportError) or (
            isinstance(cause, httpx.HTTPStatusError)
            and (cause.response.status_code == 429 or cause.response.status_code >= 500)
        ):
            return self.clock() + budget.retry_delay_seconds * (2 ** (attempts - 1))
        return None

    def _finish_error(self, job_id, name, error, plan):
        job = self.get_job(job_id)
        attempts = job["entries"].get(name, {}).get("attempts", 1)
        retry_at = self._retry_at(error, attempts, plan.input.budget)
        if isinstance(error, JobStopped) or job["cancel_requested"]:
            status, code, message = (
                "cancelled",
                "cancelled",
                "Cancellation completed at a checkpoint.",
            )
        elif (
            isinstance(error, JobBudgetExceeded)
            or job["requests"] >= plan.input.budget.max_requests
            or job["bytes"] >= plan.input.budget.max_total_bytes
        ):
            status, code, message = (
                "failed",
                "budget_exhausted",
                "The cumulative job request or byte budget was exhausted.",
            )
        elif (
            retry_at is not None
            and attempts < plan.input.budget.max_attempts
            and job["requests"] < plan.input.budget.max_requests
            and job["bytes"] < plan.input.budget.max_total_bytes
        ):
            status, code, message = (
                "waiting",
                "provider_retry",
                "Provider access is deferred; retained checkpoints will be reused.",
            )
        else:
            status, code, message = (
                "failed",
                "retrieval_failed",
                "Input, provider response or retained evidence could not be verified.",
            )
        if retry_at is not None and not math.isfinite(retry_at):
            status, retry_at = "failed", None
        with self._tx() as db:
            current = db.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if current["cancel_requested"]:
                status, code, message = (
                    "cancelled",
                    "cancelled",
                    "Cancellation completed at a checkpoint.",
                )
            document = canonical({"code": code, "message": message}).decode()
            db.execute(
                "UPDATE jobs SET status=?,phase=NULL,error=?,retry_at=?,worker=NULL,updated_at=? WHERE job_id=?",
                (status, document, retry_at if status == "waiting" else None, self.clock(), job_id),
            )
            if name:
                db.execute(
                    "UPDATE entries SET state=?,error=? WHERE job_id=? AND name=? AND state!='succeeded'",
                    ("pending" if status == "waiting" else "failed", document, job_id, name),
                )
            self._event(db, job_id, status, name)

    def run_job(self, job_id=None):
        if job_id is None:
            with self._db() as db:
                ids = [
                    r[0]
                    for r in db.execute(
                        "SELECT job_id FROM jobs WHERE status IN ('queued','running','waiting') AND (retry_at IS NULL OR retry_at<=?) ORDER BY created_at,job_id",
                        (self.clock(),),
                    )
                ]
            for candidate in ids:
                result = self._run_locked(candidate)
                if result is not None:
                    return result
            return {"status": "idle"}
        TypeAdapter(JobId).validate_python(job_id)
        self.get_job(job_id)
        return self._run_locked(job_id) or self.get_job(job_id)

    def _run_locked(self, job_id):
        with (self.root / f"{job_id}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            job = self.get_job(job_id)
            if job["status"] in TERMINAL or (
                job["retry_at"] is not None and job["retry_at"] > self.clock()
            ):
                return job
            if job["cancel_requested"]:
                with self._tx() as db:
                    db.execute(
                        "UPDATE jobs SET status='cancelled',worker=NULL,updated_at=? WHERE job_id=?",
                        (self.clock(), job_id),
                    )
                return self.get_job(job_id)
            with self._tx() as db:
                # cancel_job can race the file-lock claim; inspect within the write transaction.
                state = db.execute(
                    "SELECT status,cancel_requested FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if state["status"] in TERMINAL or state["cancel_requested"]:
                    return self.get_job(job_id)
                worker = canonical(
                    {"pid": os.getpid(), "host": socket.gethostname(), "started_at": self.clock()}
                ).decode()
                db.execute(
                    "UPDATE jobs SET status='running',phase='acquiring',retry_at=NULL,error=NULL,worker=?,updated_at=? WHERE job_id=?",
                    (worker, self.clock(), job_id),
                )
                self._event(db, job_id, "worker_started")
            plan = RetrievalPlan.model_validate(self.get_plan(job["plan_id"]))
            name = None
            try:
                if plan.implementation != _implementation():
                    raise ValueError("Implementation changed; replan before resuming")
                for name, entry in plan.input.spec.entries.items():
                    self._check_cancel(job_id)
                    progress = self.get_job(job_id)["entries"][name]
                    if progress["state"] == "succeeded":
                        self.store.verify(progress["snapshot_id"])
                        continue
                    with self._tx() as db:
                        self._check_running(db, job_id)
                        if (
                            progress["state"] != "running"
                            and progress["attempts"] >= plan.input.budget.max_attempts
                        ):
                            raise JobBudgetExceeded("Entry attempt budget exhausted")
                        db.execute(
                            "UPDATE entries SET state='running',attempts=attempts+?,error=NULL WHERE job_id=? AND name=?",
                            (0 if progress["state"] == "running" else 1, job_id, name),
                        )
                        self._event(db, job_id, "entry_started", name)
                    manifest = self._acquire(
                        job_id, name, plan.entries[name], entry, plan, progress["epoch"]
                    )
                    with self._tx() as db:
                        self._check_running(db, job_id)
                        db.execute(
                            "UPDATE entries SET snapshot_id=? WHERE job_id=? AND name=?",
                            (manifest["product_id"], job_id, name),
                        )
                    Service(self.store.root, index_store=False).query_data(
                        manifest["product_id"], entry.query
                    )
                    with self._tx() as db:
                        self._check_running(db, job_id)
                        db.execute(
                            "UPDATE entries SET state='succeeded',snapshot_id=?,error=NULL WHERE job_id=? AND name=?",
                            (manifest["product_id"], job_id, name),
                        )
                        self._event(db, job_id, "entry_completed", name)
                with self._tx() as db:
                    self._check_running(db, job_id)
                    db.execute("UPDATE jobs SET phase='finalizing' WHERE job_id=?", (job_id,))
                # Once finalization starts cancellation cannot interrupt publication.
                snapshots = {
                    n: e["snapshot_id"] for n, e in self.get_job(job_id)["entries"].items()
                }
                lock = Collections(self.store).resolve(
                    plan.input.spec, offline=True, activate=False, snapshot_ids=snapshots
                )
                result = {"collection": lock.spec.name, "lock_id": lock.lock_id}
                with self._tx() as db:
                    db.execute(
                        "UPDATE jobs SET status='succeeded',phase=NULL,result=?,error=NULL,worker=NULL,updated_at=? WHERE job_id=?",
                        (canonical(result).decode(), self.clock(), job_id),
                    )
                    self._event(db, job_id, "completed")
            except Exception as error:  # noqa: BLE001 -- persist a safe terminal result at worker boundary
                logger.info("Retrieval job %s failed (%s)", job_id, type(error).__name__)
                self._finish_error(job_id, name, error, plan)
            except BaseException:
                with self._tx() as db:
                    db.execute(
                        "UPDATE jobs SET status='queued',phase=NULL,worker=NULL,updated_at=? WHERE job_id=?",
                        (self.clock(), job_id),
                    )
                    self._event(db, job_id, "interrupted", name)
                raise
            return self.get_job(job_id)


class CheckpointClient:
    """Replay a verified successful HTTP prefix, preserving adapter consistency checks."""

    persistent_cache = True

    def __init__(self, service, job_id, entry, epoch, client):
        self.service, self.job_id, self.client = service, job_id, client
        self.root = service.root / "checkpoints" / job_id / entry / str(epoch)
        self.root.mkdir(parents=True, exist_ok=True)
        self.position = 0
        self.resources = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.client.__exit__(*args)

    def get(self, url, params=None):
        return self.request("GET", url, params=params)

    def post(self, url, *, json):
        return self.request("POST", url, json=json)

    @contextmanager
    def scope(self, token):
        with self.client.scope(token):
            yield self

    def request(self, method, url, *, params=None, json=None):
        self.service._check_cancel(self.job_id)
        # Positional replay distinguishes the adapter's before/after revision checks.
        key = hashlib.sha256(
            canonical(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "json": json,
                    "cache_scope": getattr(self.client, "cache_scope", None),
                }
            )
        ).hexdigest()
        path = self.root / f"{self.position:05d}.json"
        raw = path.with_suffix(".bin")
        self.position += 1
        if path.exists():
            doc = loads(path.read_bytes())
            capture_id = doc.pop("capture_id")
            body = raw.read_bytes()
            if (
                capture_id != hashlib.sha256(canonical(doc)).hexdigest()
                or doc["request_key"] != key
                or doc["body_sha256"] != hashlib.sha256(body).hexdigest()
            ):
                raise RetrievalError("HTTP checkpoint verification failed")
            resource = Resource(
                body,
                doc["url"],
                doc["method"],
                doc["request_body"],
                doc["content_type"],
                doc["headers"],
                doc.get("cache_scope"),
            )
        else:
            resource = self.client.request(method, url, params=params, json=json)
            doc = {
                "request_key": key,
                "body_sha256": hashlib.sha256(resource.body).hexdigest(),
                "url": resource.url,
                "method": resource.method,
                "request_body": resource.request_body,
                "content_type": resource.content_type,
                "headers": resource.headers,
                "cache_scope": resource.cache_scope,
            }
            doc["capture_id"] = hashlib.sha256(canonical(doc)).hexdigest()
            fd, filename = tempfile.mkstemp(dir=self.root, prefix=".raw-")
            temporary = Path(filename)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(resource.body)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(raw)
                _atomic_json(path, doc)
            finally:
                temporary.unlink(missing_ok=True)
        replayed = getattr(self.client, "mark_replayed", None)
        if replayed is not None:
            replayed(resource)
        self.resources.append(resource)
        return resource
