"""Explicit, durable metadata acquisition; discovery reads never contact providers."""

import fcntl
import hashlib
import json
import logging
import math
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from ssdu.connector_cache import CachedClient
from ssdu.contracts import SnapshotId
from ssdu.http import HttpClient, ProviderRetry, Resource
from ssdu.metadata_contract import safe_name, validate_record
from ssdu.metadata_store import MetadataStore
from ssdu.metadata_sync_models import SyncInput, SyncPlan
from ssdu.models import RetrievalError
from ssdu.providers import get_provider
from ssdu.rate_limit import RateLimitWait
from ssdu.retrieval import JobBudgetExceeded, JobStopped
from ssdu.retrieval_models import JobId, SubmitInput
from ssdu.store import Store, _atomic_json, canonical

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled"}


def _iso(clock):
    return datetime.fromtimestamp(clock(), UTC).isoformat().replace("+00:00", "Z")


def _implementation():
    root = Path(__file__).parent
    paths = {
        root / name
        for name in (
            "metadata_sync.py",
            "metadata_sync_models.py",
            "metadata_contract.py",
            "metadata_store.py",
            "connector_cache.py",
            "http.py",
            "rate_limit.py",
            "providers.py",
        )
    }
    paths.update(
        root / "adapters" / f"{provider}.py"
        for provider in (
            "nomis",
            "statswales",
            "mhclg",
            "nisra",
            "scotland",
        )
    )
    return hashlib.sha256(
        canonical(
            {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(paths)
            }
        )
    ).hexdigest()


def _signed(value):
    return {**value, "receipt_id": hashlib.sha256(canonical(value)).hexdigest()}


def _receipt(path):
    document = json.loads(path.read_bytes())
    digest = document.pop("receipt_id")
    if digest != hashlib.sha256(canonical(document)).hexdigest():
        raise RetrievalError("Metadata checkpoint verification failed")
    return document


def _checked(resources):
    """Oldest actual check among required resources; unknown evidence stays unknown."""
    return _earliest(resources, "checked_at")


def _earliest(resources, field):
    dates = [item.get(field) for item in resources]
    if not dates or not all(dates):
        return None
    return min(dates, key=lambda value: datetime.fromisoformat(value))


class MetadataSync:
    """Plan locally, acquire explicitly, and publish verified metadata snapshots.

    A stopped worker resumes its successful HTTP prefix. An explicit retry creates
    a fresh failed-provider epoch. Budgets include unsuccessful HTTP attempts and
    streamed bytes; cache and checkpoint hits spend neither.
    """

    def __init__(self, root="data", *, clock=None, client_factory=None):
        self.store = root if isinstance(root, Store) else Store(root)
        self.clock, self.client_factory = clock or time.time, client_factory
        self.root = self.store.root / "metadata-sync"
        self.root.mkdir(exist_ok=True)
        self.path = self.store.root / "metadata-sync.sqlite3"
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
                CREATE TABLE IF NOT EXISTS providers (
                    job_id TEXT NOT NULL, provider TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,
                    error TEXT, PRIMARY KEY(job_id, provider)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL, kind TEXT NOT NULL, provider TEXT
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

    def _event(self, db, job_id, kind, provider=None):
        db.execute(
            "INSERT INTO events(job_id,occurred_at,kind,provider) VALUES(?,?,?,?)",
            (job_id, self.clock(), kind, provider),
        )

    def plan_sync(self, spec, *, offline=False, refresh=False, budget=None):
        value = {"spec": spec, "offline": offline, "refresh": refresh}
        if budget is not None:
            value["budget"] = budget
        args = SyncInput.model_validate(value)
        content = {
            "schema_version": 1,
            "input": args.model_dump(),
            "implementation": _implementation(),
            "ready": True,
            "provider_access_required": not args.offline,
            "estimated_requests": 0 if args.offline else None,
            "estimated_bytes": 0 if args.offline else None,
            "warnings": [
                "Planning performs no provider requests; online cost and cache availability are unknown.",
                "Completeness describes this connector scope and capture, not an atomic upstream release.",
                "Cache replay preserves the original provider check time and does not establish current freshness.",
            ],
        }
        plan = SyncPlan.model_validate(
            {"plan_id": hashlib.sha256(canonical(content)).hexdigest(), **content}
        )
        with self._tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO plans VALUES(?,?,?)",
                (
                    plan.plan_id,
                    canonical(plan.model_dump()).decode(),
                    self.clock(),
                ),
            )
        return self.get_plan(plan.plan_id)

    def get_plan(self, plan_id):
        plan_id = TypeAdapter(SnapshotId).validate_python(plan_id)
        with self._db() as db:
            row = db.execute("SELECT document FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        plan = SyncPlan.model_validate(json.loads(row[0]))
        if plan.plan_id != plan_id:
            raise RetrievalError("Metadata plan identity differs")
        return plan.model_dump()

    def submit_plan(self, plan_id, idempotency_key):
        args = SubmitInput(plan_id=plan_id, idempotency_key=idempotency_key)
        plan = SyncPlan.model_validate(self.get_plan(args.plan_id))
        with self._tx() as db:
            old = db.execute(
                "SELECT job_id,plan_id FROM jobs WHERE idempotency_key=?", (args.idempotency_key,)
            ).fetchone()
            if old:
                if old["plan_id"] != plan.plan_id:
                    raise ValueError("Idempotency key already refers to another metadata plan")
                job_id = old["job_id"]
            else:
                if plan.implementation != _implementation():
                    raise ValueError("Implementation changed; create a new metadata plan")
                job_id, now = "job_" + uuid.uuid4().hex, self.clock()
                db.execute(
                    "INSERT INTO jobs(job_id,plan_id,idempotency_key,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                    (job_id, plan.plan_id, args.idempotency_key, now, now),
                )
                db.executemany(
                    "INSERT INTO providers(job_id,provider,state) VALUES(?,?,'pending')",
                    [(job_id, provider) for provider in plan.input.spec.providers],
                )
                self._event(db, job_id, "submitted")
        return self.get_job(job_id)

    def get_job(self, job_id):
        job_id = TypeAdapter(JobId).validate_python(job_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            result = dict(row)
            providers = db.execute(
                "SELECT provider,state,attempts,epoch,error FROM providers WHERE job_id=? ORDER BY provider",
                (job_id,),
            ).fetchall()
            events = db.execute(
                "SELECT occurred_at,kind,provider FROM events WHERE job_id=? ORDER BY event_id DESC LIMIT 20",
                (job_id,),
            ).fetchall()
        for key in ("result", "error", "worker"):
            result[key] = json.loads(result[key]) if result[key] else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["providers"] = {
            row["provider"]: {
                key: json.loads(value) if key == "error" and value else value
                for key, value in dict(row).items()
                if key != "provider"
            }
            for row in providers
        }
        result["events"] = [dict(row) for row in reversed(events)]
        result["budget"] = self.get_plan(result["plan_id"])["input"]["budget"]
        return result

    def list_jobs(self, limit=20, offset=0):
        self._bounds(limit, offset)
        with self._db() as db:
            rows = db.execute(
                "SELECT job_id,plan_id,status,created_at,updated_at,retry_at FROM jobs ORDER BY created_at,job_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    @staticmethod
    def _bounds(limit, offset):
        if (
            type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= 100
            or not 0 <= offset <= 100_000
        ):
            raise ValueError("Invalid metadata job page bounds")

    def cancel_job(self, job_id):
        self.get_job(job_id)
        with self._tx() as db:
            row = db.execute("SELECT status,phase FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row["status"] not in TERMINAL and row["phase"] != "finalizing":
                db.execute(
                    "UPDATE jobs SET status=?,cancel_requested=1,updated_at=? WHERE job_id=?",
                    (
                        "running" if row["status"] == "running" else "cancelled",
                        self.clock(),
                        job_id,
                    ),
                )
                self._event(db, job_id, "cancel_requested")
        return self.get_job(job_id)

    def retry_job(self, job_id):
        job = self.get_job(job_id)
        if job["status"] != "failed":
            raise ValueError("Only failed metadata jobs can be retried")
        if (
            job["requests"] >= job["budget"]["max_requests"]
            or job["bytes"] >= job["budget"]["max_total_bytes"]
            or any(
                row["state"] != "succeeded" and row["attempts"] >= job["budget"]["max_attempts"]
                for row in job["providers"].values()
            )
        ):
            raise ValueError("Metadata job budget exhausted; create a new plan/job explicitly")
        with self._tx() as db:
            changed = db.execute(
                "UPDATE jobs SET status='queued',phase=NULL,error=NULL,retry_at=NULL,updated_at=? WHERE job_id=? AND status='failed'",
                (self.clock(), job_id),
            ).rowcount
            if not changed:
                raise ValueError("Metadata job state changed")
            db.execute(
                "UPDATE providers SET state='pending',epoch=epoch+1,error=NULL WHERE job_id=? AND state!='succeeded'",
                (job_id,),
            )
            self._event(db, job_id, "retry_requested")
        return self.get_job(job_id)

    def _check_running(self, db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["cancel_requested"] or row["status"] != "running":
            raise JobStopped("Metadata job cancelled")
        return row

    def _check_cancel(self, job_id):
        with self._db() as db:
            self._check_running(db, job_id)

    def _consume(self, job_id, budget, *, requests=0, bytes_count=0):
        with self._tx() as db:
            row = self._check_running(db, job_id)
            if row["requests"] + requests > budget.max_requests:
                raise JobBudgetExceeded("Metadata request budget exhausted")
            db.execute(
                "UPDATE jobs SET requests=requests+?,bytes=bytes+?,updated_at=? WHERE job_id=?",
                (requests, bytes_count, self.clock(), job_id),
            )
            exhausted = row["bytes"] + bytes_count > budget.max_total_bytes
        if exhausted:
            raise JobBudgetExceeded("Metadata byte budget exhausted")

    def _epoch_root(self, job_id, provider, epoch):
        return self.root / "checkpoints" / job_id / provider / str(epoch)

    def _operation(self, job_id, provider, epoch, plan, kind, args):
        key = hashlib.sha256(canonical({"kind": kind, "args": args})).hexdigest()
        root = self._epoch_root(job_id, provider, epoch) / key
        root.mkdir(parents=True, exist_ok=True)
        receipt = root / "result.json"
        if receipt.exists():
            result = _receipt(receipt)
            if result["kind"] != kind or result["args"] != args:
                raise RetrievalError("Metadata operation identity differs")
            for resource in result["resources"]:
                body = (self.root / resource["checkpoint_file"]).read_bytes()
                if hashlib.sha256(body).hexdigest() != resource["body_sha256"]:
                    raise RetrievalError("Metadata operation evidence differs")
            return result["result"], [{**item, "cache_hit": True} for item in result["resources"]]
        self._check_cancel(job_id)
        job, budget = self.get_job(job_id), plan.input.budget
        kwargs = {
            "max_requests": max(1, budget.max_requests - job["requests"]),
            "max_total_bytes": max(1, budget.max_total_bytes - job["bytes"]),
            "retries": 0,
            "before_attempt": lambda *_: self._consume(job_id, budget, requests=1),
            "on_bytes": lambda count: self._consume(job_id, budget, bytes_count=count),
            "allowed_hosts": get_provider(provider).allowed_hosts,
        }
        transport = self.client_factory(**kwargs) if self.client_factory else HttpClient(**kwargs)
        with (
            CachedClient(
                self.store.root,
                client=transport,
                offline=plan.input.offline,
                refresh=not plan.input.offline and (plan.input.refresh or epoch > 0),
                allowed_hosts=get_provider(provider).allowed_hosts,
                close_client=True,
                clock=self.clock,
            ) as cache,
            MetadataCheckpointClient(self, job_id, root, cache) as client,
        ):
            adapter = get_provider(provider).adapter()
            try:
                if kind == "catalogue":
                    value = adapter.list_datasets(client=client, **args)
                elif kind == "description":
                    value = adapter.describe_dataset(client=client, **args)
                else:
                    value = adapter.list_dimension_values(client=client, **args)
            except Exception as error:
                error._metadata_evidence = list(client.evidence)
                raise
            resources = client.evidence
        _atomic_json(
            receipt, _signed({"kind": kind, "args": args, "result": value, "resources": resources})
        )
        return value, resources

    def _provider(self, job_id, provider, epoch, options, plan):
        now, rows, evidence, errors = _iso(self.clock), {}, [], []
        duplicate_count, code_pages, described, checked_count = 0, 0, 0, 0
        catalogue_complete, codes_complete = False, options.detail == "codes"
        stable, total, next_offset = True, None, 0
        if options.dataset_ids:
            rows = {
                identity: ({"id": identity, "title": identity, "provider": provider}, [])
                for identity in options.dataset_ids
            }
        else:
            for _ in range(options.max_pages):
                result, resources = self._operation(
                    job_id,
                    provider,
                    epoch,
                    plan,
                    "catalogue",
                    {
                        "query": options.query,
                        "limit": options.page_size,
                        "offset": next_offset,
                    },
                )
                evidence.extend(resources)
                if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                    raise RetrievalError("Malformed common catalogue page")
                if (
                    total is not None
                    and result.get("total") is not None
                    and total != result["total"]
                ):
                    stable = False
                if result.get("total") is not None:
                    total = result["total"]
                for row in result["items"]:
                    identity = row.get("id") if isinstance(row, dict) else None
                    if (
                        not isinstance(identity, str)
                        or not identity
                        or not isinstance(row.get("title"), str)
                        or not row["title"].strip()
                    ):
                        errors.append({"stage": "catalogue", "code": "invalid_record"})
                        stable = False
                        continue
                    if identity in rows:
                        duplicate_count += 1
                        stable = False
                        continue
                    if len(rows) >= options.max_datasets:
                        stable = False
                        break
                    rows[identity] = row, resources
                following = result.get("next_offset")
                if following is None:
                    catalogue_complete = stable and (total is None or len(rows) == total)
                    next_offset = None
                    break
                if (
                    type(following) is not int
                    or not next_offset < following <= 100_000
                    or not result["items"]
                ):
                    raise RetrievalError("Catalogue pagination failed to advance within its bounds")
                next_offset = following
                if len(rows) >= options.max_datasets:
                    break
        records = []
        for identity, (summary, summary_resources) in rows.items():
            self._check_cancel(job_id)
            descriptor, pages, record_evidence = None, [], list(summary_resources)
            if options.detail != "catalogue":
                try:
                    candidate, resources = self._operation(
                        job_id, provider, epoch, plan, "description", {"dataset_id": identity}
                    )
                    record_evidence.extend(resources)
                    evidence.extend(resources)
                    if (
                        not isinstance(candidate, dict)
                        or candidate.get("provider") != provider
                        or candidate.get("dataset_id") != identity
                        or not isinstance(candidate.get("title"), str)
                        or not candidate["title"].strip()
                        or not isinstance(candidate.get("dimensions", []), list)
                    ):
                        raise RetrievalError(
                            "Provider description identifies a different or malformed dataset"
                        )
                    descriptor = candidate
                    described += 1
                    if _checked(resources):
                        checked_count += 1
                except (JobBudgetExceeded, JobStopped, ProviderRetry, RateLimitWait):
                    raise
                except (RetrievalError, ValueError, TypeError, KeyError) as error:
                    descriptor = None
                    failed_evidence = getattr(error, "_metadata_evidence", [])
                    evidence.extend(failed_evidence)
                    record_evidence.extend(failed_evidence)
                    if options.dataset_ids or not record_evidence:
                        # User-supplied IDs alone do not establish that a dataset exists.
                        errors.append(
                            {"native_id": identity, "stage": "description", "code": "quarantined"}
                        )
                        continue
                    errors.append(
                        {"native_id": identity, "stage": "description", "code": "quarantined"}
                    )
            if (
                descriptor
                and options.detail == "codes"
                and descriptor.get("capabilities", {}).get("dimension_values")
            ):
                dimensions = descriptor.get("dimensions", [])
                if len(dimensions) > 256:
                    codes_complete = False
                    errors.append(
                        {"native_id": identity, "stage": "codes", "code": "dimension_bound"}
                    )
                for dimension in dimensions[:256]:
                    offset, complete = 0, False
                    for _ in range(options.max_code_pages):
                        try:
                            result, resources = self._operation(
                                job_id,
                                provider,
                                epoch,
                                plan,
                                "codes",
                                {
                                    "dataset_id": identity,
                                    "dimension": dimension["id"],
                                    "search": "",
                                    "limit": options.page_size,
                                    "offset": offset,
                                },
                            )
                            code_pages += 1
                            pages.append({"dimension": dimension["id"], "offset": offset, **result})
                            record_evidence.extend(resources)
                            evidence.extend(resources)
                            following = result.get("next_offset")
                            if following is None:
                                complete = True
                                break
                            if (
                                type(following) is not int
                                or not offset < following <= 100_000
                                or not result.get("items")
                            ):
                                raise RetrievalError("Dimension pagination failed to advance")
                            offset = following
                        except (JobBudgetExceeded, JobStopped, ProviderRetry, RateLimitWait):
                            raise
                        except (RetrievalError, ValueError, TypeError, KeyError) as error:
                            failed_evidence = getattr(error, "_metadata_evidence", [])
                            evidence.extend(failed_evidence)
                            record_evidence.extend(failed_evidence)
                            errors.append(
                                {"native_id": identity, "stage": "codes", "code": "quarantined"}
                            )
                            break
                    if not complete:
                        codes_complete = False
            elif options.detail == "codes" and descriptor is None:
                codes_complete = False
            base = descriptor or summary
            raw_paths = sorted({"raw/" + item["body_sha256"] + ".bin" for item in record_evidence})
            references = self._references(base, record_evidence)
            release = base.get("release")
            modified = self._modified(base)
            record = {
                "provider": provider,
                "native_id": identity,
                "dataset_id": f"{provider}:{identity}",
                "title": base.get("title", identity),
                "description": base.get("description"),
                "kind": "dataset",
                "dimensions": base.get("dimensions", []),
                "measures": base.get("measures", []),
                "distributions": base.get("distributions", []),
                "release": release,
                "coverage": base.get("coverage"),
                "access": base.get("access"),
                "licence": base.get("licence"),
                "references": references,
                "raw_resources": raw_paths,
                "source_metadata": {
                    "catalogue": summary,
                    "descriptor": descriptor,
                    "code_pages": pages,
                },
                "derived_evidence": [],
                "captured_at": now,
                "last_checked_at": _checked(record_evidence),
                "provider_modified_at": modified,
                "mapping_version": "1.0.0",
            }
            captured = _earliest(record_evidence, "captured_at")
            if captured is None:
                record.pop("captured_at", None)
            else:
                record["captured_at"] = captured
            try:
                records.append(validate_record(record))
            except ValueError:
                errors.append({"native_id": identity, "stage": "mapping", "code": "quarantined"})
        identities = {}
        for item in evidence:
            identity = canonical(
                {key: item.get(key) for key in ("method", "url", "request_body", "cache_scope")}
            )
            previous = identities.setdefault(identity, item["body_sha256"])
            if previous != item["body_sha256"]:
                errors.append({"stage": "consistency", "code": "metadata_changed_during_capture"})
                codes_complete = False
                break
        descriptions_complete = (
            options.detail != "catalogue" and described == len(rows) and len(records) == len(rows)
        )
        requested_complete = (
            (catalogue_complete or bool(options.dataset_ids))
            and not errors
            and len(records) == len(rows)
        )
        if options.detail != "catalogue":
            requested_complete = requested_complete and descriptions_complete
        if options.detail == "codes":
            requested_complete = requested_complete and codes_complete
        status = "complete" if requested_complete else "partial"
        resources = self._unique_evidence(evidence)
        report = {
            "provider": provider,
            "scope": options.model_dump(),
            "completeness": {
                "status": status,
                "catalogue_complete": catalogue_complete,
                "descriptions_complete": descriptions_complete,
                "codes_complete": codes_complete,
                "discovered_count": len(records),
                "described_count": min(described, len(records)),
                "duplicate_count": duplicate_count,
                "code_pages": code_pages,
                "quarantine_count": len(errors),
                "checked_dataset_count": min(checked_count, len(records)),
                "observed_inventory": not bool(options.dataset_ids),
                "inventory_authoritative": False,
                "reason": None
                if status == "complete"
                else "Configured bounds, changing catalogue or quarantined records prevent complete coverage.",
                "errors": errors,
            },
            "freshness": {
                "captured_at": _earliest(resources, "captured_at") or now,
                "last_checked_at": _checked(resources),
                "provider_modified_at": None,
            },
            "references": self._references({}, resources)
            if resources
            else [
                {
                    "title": f"{provider} metadata service",
                    "url": "https://" + min(get_provider(provider).allowed_hosts),
                }
            ],
            "raw_resources": sorted({"raw/" + item["body_sha256"] + ".bin" for item in resources}),
            "source_metadata": {
                "catalogue_total": total,
                "next_offset": next_offset,
                "consistency": "observed_inventory_not_atomic_upstream_snapshot",
                "inventory_basis": "observed_traversal_without_upstream_snapshot_token",
                "absence_policy": "unobserved_datasets_are_preserved",
                "capture_time_basis": "source_resources"
                if _earliest(resources, "captured_at")
                else "local_materialization_unknown_source_capture",
            },
        }
        return {"report": report, "records": records, "resources": resources}

    @staticmethod
    def _modified(base):
        release = base.get("release") or {}
        for key in ("updated", "modified", "publish_at", "published"):
            value = release.get(key) if isinstance(release, dict) else None
            if isinstance(value, str) and value:
                return value
        return base.get("updated") if isinstance(base.get("updated"), str) else None

    @staticmethod
    def _references(base, resources):
        references = [
            item
            for item in base.get("references", [])
            if isinstance(item, dict) and item.get("url") and item.get("title")
        ]
        if base.get("source_url"):
            references.append(
                {"title": base.get("title") or "Provider dataset", "url": base["source_url"]}
            )
        for item in resources:
            references.append(
                {
                    "title": "Retained provider metadata response",
                    "url": item["url"],
                    "resource_paths": ["raw/" + item["body_sha256"] + ".bin"],
                }
            )
        return sorted({canonical(item): item for item in references}.values(), key=canonical)

    @staticmethod
    def _unique_evidence(resources):
        return sorted({canonical(item): item for item in resources}.values(), key=canonical)

    def _seed_snapshots(self):
        """Recover cache only from body files bound to verified portable inventories."""
        snapshots, offset = MetadataStore(self.store.root), 0
        # Latest capture wins; collect first, then seed only absent request keys.
        candidates = []
        while True:
            page = snapshots.list(limit=100, offset=offset)
            candidates.extend(page["rows"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        from ssdu.connector_cache import _atomic, _canonical

        cache = self.store.root / "connector-cache"
        cache.mkdir(exist_ok=True)
        (cache / "objects").mkdir(exist_ok=True)
        for item in sorted(
            candidates, key=lambda row: (row["captured_at"], row["snapshot_id"]), reverse=True
        ):
            sid = item["snapshot_id"]
            manifest = snapshots.verify(sid)
            document = snapshots.read(sid)
            for response in document.get("source_metadata", {}).get("responses", []):
                if response.get("method") not in {"GET", "POST"}:
                    continue
                identity = {
                    "method": response["method"],
                    "url": response["url"],
                    "request_body": response["request_body"],
                    "cache_scope": response.get("cache_scope"),
                }
                key = hashlib.sha256(_canonical(identity)).hexdigest()
                path = cache / f"{key}.json"
                if path.exists():
                    continue
                name = safe_name(response["file"])
                if "resources/" + name not in manifest["files"]:
                    raise RetrievalError("Metadata response is absent from the verified inventory")
                body = (snapshots.path(sid) / "resources" / name).read_bytes()
                digest = hashlib.sha256(body).hexdigest()
                record = {
                    "request": identity,
                    "body_sha256": digest,
                    "url": response["url"],
                    "content_type": response["content_type"],
                    "headers": response["headers"],
                    "captured_at": response.get("captured_at"),
                    "checked_at": response.get("checked_at"),
                }
                record["capture_id"] = hashlib.sha256(_canonical(record)).hexdigest()
                with (cache / f"{key}.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if not path.exists():
                        _atomic(cache / "objects" / f"{digest}.bin", body)
                        _atomic(path, _canonical(record))

    def _retry_at(self, error, attempts, budget):
        if isinstance(error, RateLimitWait):
            return max(self.clock() + budget.retry_delay_seconds, error.retry_at)
        if isinstance(error, ProviderRetry):
            return max(
                error.retry_at, self.clock() + budget.retry_delay_seconds * 2 ** (attempts - 1)
            )
        cause = error.__cause__
        if (
            isinstance(cause, httpx.TransportError)
            or isinstance(cause, httpx.HTTPStatusError)
            and (cause.response.status_code == 429 or cause.response.status_code >= 500)
        ):
            return self.clock() + budget.retry_delay_seconds * 2 ** (attempts - 1)
        return None

    def _finish_error(self, job_id, provider, error, plan):
        job = self.get_job(job_id)
        attempts = job["providers"].get(provider, {}).get("attempts", 1)
        retry_at = self._retry_at(error, attempts, plan.input.budget)
        if isinstance(error, JobStopped) or job["cancel_requested"]:
            status, code = "cancelled", "cancelled"
        elif (
            isinstance(error, JobBudgetExceeded)
            or job["requests"] >= plan.input.budget.max_requests
            or job["bytes"] >= plan.input.budget.max_total_bytes
        ):
            status, code = "failed", "budget_exhausted"
        elif (
            retry_at is not None
            and (isinstance(error, RateLimitWait) or attempts < plan.input.budget.max_attempts)
            and math.isfinite(retry_at)
        ):
            status, code = "waiting", "provider_retry"
        else:
            status, code = "failed", "metadata_sync_failed"
        document = canonical(
            {
                "code": code,
                "message": "Metadata sync stopped; retained successful checkpoints remain available. No new inventory was published.",
            }
        ).decode()
        with self._tx() as db:
            cancelled = db.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            if cancelled:
                status, code = "cancelled", "cancelled"
                document = canonical(
                    {"code": code, "message": "Cancellation completed at a checkpoint."}
                ).decode()
            db.execute(
                "UPDATE jobs SET status=?,phase=NULL,error=?,retry_at=?,worker=NULL,updated_at=? WHERE job_id=?",
                (status, document, retry_at if status == "waiting" else None, self.clock(), job_id),
            )
            if provider:
                db.execute(
                    "UPDATE providers SET state=?,error=?,attempts=attempts-? WHERE job_id=? AND provider=? AND state!='succeeded'",
                    (
                        "pending" if status == "waiting" else "failed",
                        document,
                        1 if isinstance(error, RateLimitWait) and attempts else 0,
                        job_id,
                        provider,
                    ),
                )
            self._event(db, job_id, status, provider)

    def run_job(self, job_id=None):
        if job_id is None:
            with self._db() as db:
                ids = [
                    row[0]
                    for row in db.execute(
                        "SELECT job_id FROM jobs WHERE status IN ('queued','running','waiting') AND (retry_at IS NULL OR retry_at<=?) ORDER BY created_at,job_id",
                        (self.clock(),),
                    )
                ]
            for candidate in ids:
                result = self._run_locked(candidate)
                if result is not None:
                    return result
            return {"status": "idle"}
        self.get_job(job_id)
        return self._run_locked(job_id) or self.get_job(job_id)

    def _run_locked(self, job_id):
        with (self.root / f"{job_id}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            job = self.get_job(job_id)
            if (
                job["status"] in TERMINAL
                or job["retry_at"] is not None
                and job["retry_at"] > self.clock()
            ):
                return job
            with self._tx() as db:
                state = db.execute(
                    "SELECT status,cancel_requested FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if state["status"] in TERMINAL:
                    return self.get_job(job_id)
                if state["cancel_requested"]:
                    db.execute(
                        "UPDATE jobs SET status='cancelled',phase=NULL,worker=NULL,updated_at=? WHERE job_id=?",
                        (self.clock(), job_id),
                    )
                    self._event(db, job_id, "cancelled")
                else:
                    db.execute(
                        "UPDATE jobs SET status='running',phase='acquiring',retry_at=NULL,error=NULL,worker=?,updated_at=? WHERE job_id=?",
                        (
                            canonical(
                                {
                                    "pid": os.getpid(),
                                    "host": socket.gethostname(),
                                    "started_at": self.clock(),
                                }
                            ).decode(),
                            self.clock(),
                            job_id,
                        ),
                    )
                self._event(db, job_id, "worker_started")
            if self.get_job(job_id)["status"] == "cancelled":
                return self.get_job(job_id)
            plan, provider = SyncPlan.model_validate(self.get_plan(job["plan_id"])), None
            try:
                if plan.implementation != _implementation():
                    raise ValueError("Metadata implementation changed; replan before resuming")
                if plan.input.offline:
                    self._seed_snapshots()
                receipts = []
                for provider, options in plan.input.spec.providers.items():
                    self._check_cancel(job_id)
                    progress = self.get_job(job_id)["providers"][provider]
                    path = self._epoch_root(job_id, provider, progress["epoch"]) / "provider.json"
                    if progress["state"] == "succeeded":
                        receipts.append(_receipt(path))
                        continue
                    with self._tx() as db:
                        self._check_running(db, job_id)
                        if (
                            progress["state"] != "running"
                            and progress["attempts"] >= plan.input.budget.max_attempts
                        ):
                            raise JobBudgetExceeded("Metadata provider attempt budget exhausted")
                        db.execute(
                            "UPDATE providers SET state='running',attempts=attempts+?,error=NULL WHERE job_id=? AND provider=?",
                            (0 if progress["state"] == "running" else 1, job_id, provider),
                        )
                    receipt = self._provider(job_id, provider, progress["epoch"], options, plan)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_json(path, _signed(receipt))
                    with self._tx() as db:
                        self._check_running(db, job_id)
                        db.execute(
                            "UPDATE providers SET state='succeeded',error=NULL WHERE job_id=? AND provider=?",
                            (job_id, provider),
                        )
                        self._event(db, job_id, "provider_completed", provider)
                    receipts.append(receipt)
                with self._tx() as db:
                    self._check_running(db, job_id)
                    db.execute("UPDATE jobs SET phase='finalizing' WHERE job_id=?", (job_id,))
                resources, responses = {}, []
                for receipt in receipts:
                    for evidence in receipt["resources"]:
                        body = (self.root / evidence["checkpoint_file"]).read_bytes()
                        if hashlib.sha256(body).hexdigest() != evidence["body_sha256"]:
                            raise RetrievalError(
                                "Metadata response evidence differs before publication"
                            )
                        name = "raw/" + evidence["body_sha256"] + ".bin"
                        resources[name] = body
                        responses.append(
                            {
                                "file": name,
                                **{
                                    key: value
                                    for key, value in evidence.items()
                                    if key not in {"checkpoint_file", "body_sha256"}
                                },
                            }
                        )
                document = {
                    "contract_version": "1.0.0",
                    "captured_at": _iso(self.clock),
                    "scope": plan.input.spec.model_dump(),
                    "providers": [receipt["report"] for receipt in receipts],
                    "records": [record for receipt in receipts for record in receipt["records"]],
                    "source_metadata": {
                        "responses": self._unique_evidence(responses),
                        "implementation": plan.implementation,
                    },
                }
                publication = self.root / "checkpoints" / job_id / "publication.json"
                if publication.exists():
                    retained = _receipt(publication)
                    if retained["plan_id"] != plan.plan_id:
                        raise RetrievalError("Metadata publication receipt differs from its plan")
                    document = retained["document"]
                else:
                    publication.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_json(
                        publication, _signed({"plan_id": plan.plan_id, "document": document})
                    )
                sid = MetadataStore(self.store.root).publish(document, resources)
                from ssdu.metadata_index import MetadataIndex

                MetadataIndex(self.store.root, clock=self.clock).import_snapshot(sid)
                result = {
                    "snapshot_id": sid,
                    "providers": len(receipts),
                    "records": len(document["records"]),
                }
                with self._tx() as db:
                    db.execute(
                        "UPDATE jobs SET status='succeeded',phase=NULL,result=?,error=NULL,worker=NULL,updated_at=? WHERE job_id=?",
                        (canonical(result).decode(), self.clock(), job_id),
                    )
                    self._event(db, job_id, "completed")
            except Exception as error:  # noqa: BLE001 -- durable worker boundary, no unsafe provider text
                logger.info(
                    "Metadata sync job %s stopped (%s): %s", job_id, type(error).__name__, error
                )
                self._finish_error(job_id, provider, error, plan)
            except BaseException:
                with self._tx() as db:
                    db.execute(
                        "UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END,phase=NULL,worker=NULL,updated_at=? WHERE job_id=?",
                        (self.clock(), job_id),
                    )
                    self._event(db, job_id, "interrupted", provider)
                raise
            return self.get_job(job_id)


class MetadataCheckpointClient:
    """Position-bound response receipts retain every metadata check and its clocks."""

    persistent_cache = True

    def __init__(self, service, job_id, root, client):
        self.service, self.job_id, self.root, self.client = service, job_id, root, client
        self.position, self.resources, self.evidence = 0, [], []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

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
        identity = {
            "method": method,
            "url": str(httpx.Request(method, url, params=params).url),
            "request_body": json,
            "cache_scope": self.client.cache_scope,
        }
        key = hashlib.sha256(canonical(identity)).hexdigest()
        path, raw = self.root / f"{self.position:05d}.json", self.root / f"{self.position:05d}.bin"
        self.position += 1
        if path.exists():
            document = _receipt(path)
            body = raw.read_bytes()
            if (
                document["request_key"] != key
                or document["body_sha256"] != hashlib.sha256(body).hexdigest()
            ):
                raise RetrievalError("Metadata HTTP checkpoint identity differs")
            accepted = {field.name for field in fields(Resource)} - {"body"}
            resource = Resource(
                body=body,
                **{name: value for name, value in document["resource"].items() if name in accepted},
            )
            resource = replace(resource, cache_hit=True)
            self.client.mark_replayed(resource)
        else:
            resource = self.client.request(method, url, params=params, json=json)
            value = asdict(resource)
            value.pop("body")
            document = {
                "request_key": key,
                "body_sha256": hashlib.sha256(resource.body).hexdigest(),
                "resource": value,
            }
            from ssdu.connector_cache import _atomic

            _atomic(raw, resource.body)
            _atomic_json(path, _signed(document))
        provenance = asdict(resource)
        provenance.pop("body")
        evidence = {
            **provenance,
            "body_sha256": document["body_sha256"],
            "checkpoint_file": raw.relative_to(self.service.root).as_posix(),
        }
        self.resources.append(resource)
        self.evidence.append(evidence)
        return resource
