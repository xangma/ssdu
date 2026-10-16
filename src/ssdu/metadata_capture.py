"""Durable whole-catalogue capture assembled from bounded metadata-sync jobs.

Captures retain immutable inventory and description snapshots. They describe an
observed connector scope, never an authoritative census of an organisation's
holdings. No read or planning operation contacts a provider.
"""

import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pydantic import TypeAdapter

from ssdu.contracts import SnapshotId
from ssdu.metadata_capture_models import CaptureInput, CapturePlan
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_mapping import FIELD_STATUSES, MAPPING_FIELDS
from ssdu.metadata_store import MetadataStore
from ssdu.metadata_sync import MetadataSync
from ssdu.metadata_sync import _implementation as sync_implementation
from ssdu.models import RetrievalError
from ssdu.retrieval_models import JobId, SubmitInput
from ssdu.store import Store, _atomic_json, canonical

TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_BUNDLE_BYTES = 4_000_000_000
MAX_BUNDLE_MEMBERS = 50_006
MAX_MANIFEST_BYTES = 32_000_000


def _implementation():
    root = Path(__file__).parent
    return hashlib.sha256(
        canonical(
            {
                "metadata_sync": sync_implementation(),
                **{
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ("metadata_capture.py", "metadata_capture_models.py")
                },
            }
        )
    ).hexdigest()


def _signed(document, key="capture_id"):
    return {key: hashlib.sha256(canonical(document)).hexdigest(), **document}


def _validate_manifest(document):
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "capture_id",
            "schema_version",
            "kind",
            "input",
            "implementation",
            "snapshots",
            "report",
        }
        or document.get("schema_version") != 1
        or document.get("kind") != "metadata_capture"
    ):
        raise RetrievalError("Unsupported metadata capture manifest")
    identity = TypeAdapter(SnapshotId).validate_python(document["capture_id"])
    if (
        identity
        != hashlib.sha256(
            canonical({key: value for key, value in document.items() if key != "capture_id"})
        ).hexdigest()
    ):
        raise RetrievalError("Metadata capture manifest hash mismatch")
    if CaptureInput.model_validate(document["input"]).model_dump() != document["input"]:
        raise RetrievalError("Metadata capture input is not canonical")
    TypeAdapter(SnapshotId).validate_python(document["implementation"])
    snapshots = document["snapshots"]
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) < MAX_BUNDLE_MEMBERS:
        raise RetrievalError("Invalid metadata capture snapshot inventory")
    identities = set()
    for item in snapshots:
        if not isinstance(item, dict) or set(item) != {"provider", "stage", "batch", "snapshot_id"}:
            raise RetrievalError("Invalid metadata capture snapshot entry")
        TypeAdapter(SnapshotId).validate_python(item["snapshot_id"])
        if (
            item["provider"] not in document["input"]["spec"]["providers"]
            or item["stage"] not in {"inventory", "descriptions"}
            or type(item["batch"]) is not int
            or item["batch"] < 0
        ):
            raise RetrievalError("Invalid metadata capture snapshot scope")
        key = item["provider"], item["stage"], item["batch"]
        if key in identities:
            raise RetrievalError("Duplicate metadata capture batch")
        identities.add(key)
    return document


class MetadataCapture:
    """Capture inventories first, then describe their IDs in resumable batches.

    The campaign budget spans every child, retry and resumed invocation. Child
    jobs retain the SDK's shared HTTP cache, rate limits, cooldowns and immutable
    response checkpoints. ``max_batches`` optionally yields between children.
    """

    def __init__(self, root="data", *, clock=None, client_factory=None):
        self.store = root if isinstance(root, Store) else Store(root)
        self.clock = clock or time.time
        self.sync = MetadataSync(self.store, clock=self.clock, client_factory=client_factory)
        self.snapshots = MetadataStore(self.store.root)
        self._inventories = {}
        self.root = self.store.root / "metadata-capture"
        self.root.mkdir(exist_ok=True)
        self.captures = self.root / "captures"
        self.captures.mkdir(exist_ok=True)
        self.path = self.store.root / "metadata-capture.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans(plan_id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL, created_at REAL NOT NULL, document TEXT NOT NULL);
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

    def plan_capture(self, spec, *, offline=False, refresh=False, budget=None, batch_size=10):
        value = {"spec": spec, "offline": offline, "refresh": refresh, "batch_size": batch_size}
        if budget is not None:
            value["budget"] = budget
        args = CaptureInput.model_validate(value)
        content = {
            "schema_version": 1,
            "input": args.model_dump(),
            "implementation": _implementation(),
            "ready": True,
            "provider_access_required": not args.offline,
            "estimated_requests": 0 if args.offline else None,
            "estimated_bytes": 0 if args.offline else None,
            "warnings": [
                "Planning makes no provider requests; full capture cost is unknown.",
                "Completeness applies to the configured service scope, not all organisational holdings.",
                "Inventories are observed traversals, not atomic upstream snapshots or deletion authority.",
                "Cached replay preserves source check times and does not refresh metadata.",
                "Descriptions and separately requested codelists have independent completeness.",
            ],
        }
        plan = CapturePlan.model_validate(_signed(content, "plan_id")).model_dump()
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO plans VALUES(?,?)",
                (plan["plan_id"], canonical(plan).decode()),
            )
        return plan

    def get_plan(self, plan_id):
        TypeAdapter(SnapshotId).validate_python(plan_id)
        with self._db() as db:
            row = db.execute("SELECT document FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        plan = CapturePlan.model_validate(json.loads(row[0])).model_dump()
        if plan["plan_id"] != plan_id:
            raise RetrievalError("Metadata capture plan identity differs")
        return plan

    def submit_plan(self, plan_id, idempotency_key):
        args = SubmitInput(plan_id=plan_id, idempotency_key=idempotency_key)
        plan = self.get_plan(args.plan_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT job_id,plan_id FROM jobs WHERE idempotency_key=?", (args.idempotency_key,)
            ).fetchone()
            if row:
                if row["plan_id"] != plan_id:
                    raise ValueError("Idempotency key already refers to another capture plan")
                job_id = row["job_id"]
            else:
                if plan["implementation"] != _implementation():
                    raise ValueError("Implementation changed; create a new metadata capture plan")
                job_id, now = "job_" + uuid.uuid4().hex, self.clock()
                job = {
                    "job_id": job_id,
                    "plan_id": plan_id,
                    "status": "queued",
                    "phase": None,
                    "cancel_requested": False,
                    "created_at": now,
                    "updated_at": now,
                    "retry_at": None,
                    "requests": 0,
                    "bytes": 0,
                    "error": None,
                    "result": None,
                    "children": [],
                    "active_child": None,
                    "providers": {
                        name: {"state": "pending"} for name in plan["input"]["spec"]["providers"]
                    },
                }
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?)",
                    (job_id, plan_id, args.idempotency_key, now, canonical(job).decode()),
                )
        return self.get_job(job_id)

    def get_job(self, job_id):
        TypeAdapter(JobId).validate_python(job_id)
        with self._db() as db:
            row = db.execute("SELECT document FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = json.loads(row[0])
        result["budget"] = self.get_plan(result["plan_id"])["input"]["budget"]
        return result

    def _save(self, job):
        job = {key: value for key, value in job.items() if key != "budget"}
        job["updated_at"] = self.clock()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(
                db.execute("SELECT document FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[
                    0
                ]
            )
            if current["cancel_requested"]:
                job["cancel_requested"] = True
                if job["status"] != "running":
                    job["status"], job["phase"] = "cancelled", None
            db.execute(
                "UPDATE jobs SET document=? WHERE job_id=?",
                (canonical(job).decode(), job["job_id"]),
            )
        return self.get_job(job["job_id"])

    def list_jobs(self, limit=20, offset=0):
        MetadataSync._bounds(limit, offset)
        with self._db() as db:
            rows = db.execute(
                "SELECT document FROM jobs ORDER BY created_at,job_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
        fields = ("job_id", "plan_id", "status", "created_at", "updated_at", "retry_at")
        return {
            "items": [
                {key: value for key, value in json.loads(row[0]).items() if key in fields}
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        }

    def cancel_job(self, job_id):
        self.get_job(job_id)
        active_child = None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = json.loads(
                db.execute("SELECT document FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            )
            if job["status"] not in TERMINAL and job["phase"] != "finalizing":
                job["cancel_requested"] = True
                if job["status"] != "running":
                    job["status"], job["phase"] = "cancelled", None
                job["updated_at"] = self.clock()
                active_child = job["active_child"]
                db.execute(
                    "UPDATE jobs SET document=? WHERE job_id=?", (canonical(job).decode(), job_id)
                )
        if active_child:
            self.sync.cancel_job(active_child)
        return self.get_job(job_id)

    def _begin_finalize(self, job):
        """Commit publication intent atomically with the cancellation boundary."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = json.loads(
                db.execute("SELECT document FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[
                    0
                ]
            )
            if current["cancel_requested"]:
                job.update(cancel_requested=True, status="cancelled", phase=None, active_child=None)
                return False
            job["phase"] = "finalizing"
            job["updated_at"] = self.clock()
            db.execute(
                "UPDATE jobs SET document=? WHERE job_id=?",
                (canonical(job).decode(), job["job_id"]),
            )
        return True

    def retry_job(self, job_id):
        job = self.get_job(job_id)
        if job["status"] != "failed":
            raise ValueError("Only failed metadata capture jobs can be retried")
        self._account(job)
        if (
            job["requests"] >= job["budget"]["max_requests"]
            or job["bytes"] >= job["budget"]["max_total_bytes"]
        ):
            raise ValueError("Metadata capture budget exhausted; create a new plan/job explicitly")
        if job["active_child"]:
            child = self.sync.get_job(job["active_child"])
            if child["status"] == "failed":
                self.sync.retry_job(child["job_id"])
        job.update(status="queued", phase=None, error=None, retry_at=None)
        return self._save(job)

    def _account(self, job):
        job["requests"] = job["bytes"] = 0
        for child in job["children"]:
            state = self.sync.get_job(child["job_id"])
            child.update(status=state["status"], requests=state["requests"], bytes=state["bytes"])
            if state["result"]:
                child["snapshot_id"] = state["result"]["snapshot_id"]
            job["requests"] += state["requests"]
            job["bytes"] += state["bytes"]

    def _task(self, job, plan):
        for provider, options in plan["input"]["spec"]["providers"].items():
            children = [item for item in job["children"] if item["provider"] == provider]
            inventory = next((item for item in children if item["stage"] == "inventory"), None)
            if inventory is None:
                scope = {
                    **options,
                    "dataset_ids": [],
                    "detail": "catalogue",
                    "catalogue_mode": "inventory",
                }
                return provider, "inventory", 0, scope
            if inventory["status"] != "succeeded":
                return inventory
            sid = inventory["snapshot_id"]
            if sid not in self._inventories:
                document = self.snapshots.read(sid)
                self._inventories[sid] = sorted(
                    record["native_id"]
                    for record in document["records"]
                    if record["provider"] == provider
                )
            ids = self._inventories[sid]
            size = plan["input"]["batch_size"]
            description_children = sorted(
                (item for item in children if item["stage"] == "descriptions"),
                key=lambda item: item["batch"],
            )
            for child in description_children:
                if child["status"] != "succeeded":
                    return child
            batch = len(description_children)
            start = batch * size
            if start < len(ids):
                scope = {
                    **options,
                    "query": "",
                    "dataset_ids": ids[start : start + size],
                    "max_datasets": size,
                }
                return provider, "descriptions", batch, scope
            job["providers"][provider] = {
                "state": "succeeded",
                "inventory_records": len(ids),
                "description_batches": sum(item["stage"] == "descriptions" for item in children),
            }
        return None

    def _new_child(self, job, plan, task):
        remaining_requests = job["budget"]["max_requests"] - job["requests"]
        remaining_bytes = job["budget"]["max_total_bytes"] - job["bytes"]
        if remaining_requests <= 0 or remaining_bytes <= 0:
            raise RetrievalError("Metadata capture budget exhausted")
        provider, stage, batch, options = task
        child_budget = {
            **job["budget"],
            "max_requests": min(128, remaining_requests),
            "max_total_bytes": min(100_000_000, remaining_bytes),
        }
        spec = {"providers": {provider: options}}
        child_plan = self.sync.plan_sync(
            spec,
            offline=plan["input"]["offline"],
            refresh=plan["input"]["refresh"],
            budget=child_budget,
        )
        child = self.sync.submit_plan(
            child_plan["plan_id"], f"capture:{job['job_id']}:{provider}:{stage}:{batch}"
        )
        entry = {
            "provider": provider,
            "stage": stage,
            "batch": batch,
            "job_id": child["job_id"],
            "plan_id": child_plan["plan_id"],
            "status": child["status"],
            "requests": 0,
            "bytes": 0,
        }
        job["children"].append(entry)
        return entry

    def run_job(self, job_id=None, *, max_batches=None):
        if max_batches is not None and (
            type(max_batches) is not int or not 1 <= max_batches <= 1000
        ):
            raise ValueError("max_batches must be an integer between 1 and 1000")
        if job_id is None:
            offset = 0
            while True:
                page = self.list_jobs(limit=100, offset=offset)
                for item in page["items"]:
                    if item["status"] not in TERMINAL and (
                        item["retry_at"] is None or item["retry_at"] <= self.clock()
                    ):
                        result = self._run_locked(item["job_id"], max_batches)
                        if result is not None:
                            return result
                if page["next_offset"] is None:
                    return {"status": "idle"}
                offset = page["next_offset"]
        self.get_job(job_id)
        return self._run_locked(job_id, max_batches) or self.get_job(job_id)

    def _run_locked(self, job_id, max_batches):
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
            plan = self.get_plan(job["plan_id"])
            job.update(status="running", phase="capturing", retry_at=None, error=None)
            self._save(job)
            completed, recovered = 0, False
            recovery = ExitStack()
            try:
                if plan["implementation"] != _implementation():
                    raise RetrievalError("Metadata implementation changed; replan before resuming")
                while True:
                    self._account(job)
                    if self.get_job(job_id)["cancel_requested"]:
                        job.update(status="cancelled", phase=None, active_child=None)
                        break
                    task = self._task(job, plan)
                    if task is None:
                        if not self._begin_finalize(job):
                            break
                        manifest = self._publish(job, plan)
                        job.update(
                            status="succeeded",
                            phase=None,
                            active_child=None,
                            result={
                                "capture_id": manifest["capture_id"],
                                "snapshots": len(manifest["snapshots"]),
                                "records": manifest["report"]["records"],
                                "providers": len(manifest["report"]["providers"]),
                                "completeness": manifest["report"]["status"],
                            },
                        )
                        break
                    if max_batches is not None and completed >= max_batches:
                        job.update(status="queued", phase=None, active_child=None)
                        break
                    entry = task if isinstance(task, dict) else self._new_child(job, plan, task)
                    job["active_child"] = entry["job_id"]
                    self._save(job)
                    if plan["input"]["offline"] and not recovered:
                        recovery.enter_context(self.sync._recovered_snapshots())
                        recovered = True
                    state = self.sync.run_job(entry["job_id"])
                    self._account(job)
                    if state["status"] != "succeeded":
                        job.update(
                            status=state["status"],
                            phase=None,
                            retry_at=state["retry_at"],
                            error=state["error"],
                        )
                        break
                    completed += 1
                    job["active_child"] = None
                    self._save(job)
            except Exception as error:  # noqa: BLE001 -- durable worker boundary
                self._account(job)
                job.update(
                    status="failed",
                    phase=None,
                    retry_at=None,
                    error={"code": "metadata_capture_failed", "message": str(error)[:512]},
                )
            except BaseException:
                self._account(job)
                job.update(status="queued", phase=None)
                self._save(job)
                raise
            finally:
                recovery.close()
            return self._save(job)

    @staticmethod
    def _snapshot_scope(document, provider, options):
        if (
            document["scope"] != {"providers": {provider: options}}
            or len(document["providers"]) != 1
            or document["providers"][0]["provider"] != provider
            or document["providers"][0]["scope"] != options
            or any(row["provider"] != provider for row in document["records"])
        ):
            raise RetrievalError("Capture scope differs from immutable child snapshot")

    def _report(self, snapshots, args):
        providers = {}
        total_records = 0
        for provider, options in args["spec"]["providers"].items():
            entries = [item for item in snapshots if item["provider"] == provider]
            inventory = [item for item in entries if item["stage"] == "inventory"]
            if len(inventory) != 1:
                raise RetrievalError("Capture must contain exactly one provider inventory")
            document = self.snapshots.read(inventory[0]["snapshot_id"])
            expected_inventory = {
                **options,
                "detail": "catalogue",
                "catalogue_mode": "inventory",
                "dataset_ids": [],
            }
            if inventory[0]["batch"] != 0:
                raise RetrievalError("Capture inventory batch differs")
            self._snapshot_scope(document, provider, expected_inventory)
            native = document["providers"][0]
            completeness = native["completeness"]
            discovered = {
                row["native_id"] for row in document["records"] if row["provider"] == provider
            }
            description_entries = [item for item in entries if item["stage"] == "descriptions"]
            size = args["batch_size"]
            ids = sorted(discovered)
            expected_batches = (len(ids) + size - 1) // size
            if sorted(item["batch"] for item in description_entries) != list(
                range(expected_batches)
            ):
                raise RetrievalError("Capture description batch inventory differs")
            described, represented, failures, code_complete = set(), set(), [], True
            field_counts = {
                field: {status: 0 for status in sorted(FIELD_STATUSES)}
                for field in sorted(MAPPING_FIELDS)
            }
            resource_sizes, normalized_bytes, checks = {}, 0, []
            for entry in entries:
                sid = entry["snapshot_id"]
                manifest = self.snapshots.verify(sid)
                normalized_bytes += manifest["files"]["document.json"]["bytes"]
                for name, integrity in manifest["files"].items():
                    if name.startswith("resources/"):
                        resource_sizes[integrity["sha256"]] = integrity["bytes"]
                current = self.snapshots.read(sid)
                if entry["stage"] != "descriptions":
                    continue
                offset = entry["batch"] * size
                expected_description = {
                    **options,
                    "query": "",
                    "dataset_ids": ids[offset : offset + size],
                    "max_datasets": size,
                }
                self._snapshot_scope(current, provider, expected_description)
                for report in current["providers"]:
                    checks.append(report["freshness"].get("last_checked_at"))
                    failures.extend(report["completeness"].get("errors", []))
                    code_complete = code_complete and report["completeness"].get(
                        "codes_complete", False
                    )
                for row in current["records"]:
                    identity = row["native_id"]
                    if (
                        row["provider"] != provider
                        or identity not in discovered
                        or identity in represented
                    ):
                        raise RetrievalError(
                            "Description capture has unexpected or repeated catalogue membership"
                        )
                    represented.add(identity)
                    if row.get("source_metadata", {}).get("descriptor") is not None:
                        described.add(identity)
                    for field, counts in field_counts.items():
                        state = row.get("field_status", {}).get(field, {}).get("status", "unmapped")
                        counts[state] += 1
            missing = sorted(discovered - represented)
            rejected = sorted(discovered - described)
            expected = native.get("source_metadata", {}).get("catalogue_total")
            exhausted = native.get("source_metadata", {}).get("next_offset") is None
            inventory_complete = (
                completeness.get("catalogue_complete") is True
                and exhausted
                and (expected is None or expected == len(discovered))
            )
            descriptions_complete = not rejected and not missing
            complete = (
                inventory_complete
                and descriptions_complete
                and not failures
                and (options["detail"] != "codes" or code_complete)
            )
            inventory_checked = native["freshness"].get("last_checked_at")
            descriptors_checked = min(checks) if checks and all(checks) else None
            required_checks = [inventory_checked]
            if discovered:
                required_checks.append(descriptors_checked)
            last_checked = min(required_checks) if all(required_checks) else None
            providers[provider] = {
                "scope": options,
                "status": "complete" if complete else "partial",
                "catalogue_records": len(discovered),
                "reported_total": expected,
                "catalogue_exhausted": exhausted,
                "catalogue_complete": inventory_complete,
                "duplicate_count": completeness.get("duplicate_count", 0),
                "inventory_errors": completeness.get("errors", []),
                "described_records": len(described),
                "represented_records": len(represented),
                "descriptions_complete": descriptions_complete,
                "descriptor_coverage_percent": 100.0 * len(described) / len(discovered)
                if discovered
                else None,
                "catalogue_coverage_percent": 100.0 * len(discovered) / expected
                if expected
                else None,
                "codes_requested": options["detail"] == "codes",
                "codes_complete": options["detail"] == "codes" and code_complete,
                "missing_records": missing,
                "quarantined_ids": rejected,
                "description_errors": failures,
                "field_status_counts": field_counts,
                "field_status_denominator": len(represented),
                "raw_metadata_bytes": sum(resource_sizes.values()),
                "normalized_snapshot_bytes": normalized_bytes,
                "snapshots": len(entries),
                "inventory_last_checked_at": inventory_checked,
                "descriptors_last_checked_at": descriptors_checked,
                "last_checked_at": last_checked,
                "inventory_authoritative": False,
                "all_provider_holdings_percent": None,
            }
            total_records += len(discovered)
        return {
            "status": "complete"
            if all(item["status"] == "complete" for item in providers.values())
            else "partial",
            "records": total_records,
            "providers": providers,
            "limitations": [
                "Coverage is relative to each declared connector service and optional query, not all organisational holdings.",
                "Catalogue exhaustion and matching reported totals do not establish an atomic upstream inventory.",
                "Metadata field status describes normalization, independently of catalogue membership and data retrieval support.",
                "Separately paginated codelists are captured only when codes detail is requested; observations and distribution files are not downloaded.",
                "Cached replay retains original provider check times; it does not establish current freshness or infer deletions.",
            ],
        }

    def _publish(self, job, plan):
        snapshots = [
            {key: child[key] for key in ("provider", "stage", "batch", "snapshot_id")}
            for child in job["children"]
        ]
        manifest = _signed(
            {
                "schema_version": 1,
                "kind": "metadata_capture",
                "input": plan["input"],
                "implementation": plan["implementation"],
                "snapshots": snapshots,
                "report": self._report(snapshots, plan["input"]),
            }
        )
        self._retain(manifest)
        return manifest

    def _retain(self, manifest):
        _validate_manifest(manifest)
        raw = canonical(manifest) + b"\n"
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RetrievalError("Metadata capture manifest exceeds size bound")
        destination = self.captures / f"{manifest['capture_id']}.json"
        if destination.exists():
            if destination.read_bytes() != raw:
                raise RetrievalError("Metadata capture identity collision")
        else:
            _atomic_json(destination, manifest)

    def report(self, capture_id):
        TypeAdapter(SnapshotId).validate_python(capture_id)
        path = self.captures / f"{capture_id}.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RetrievalError("Metadata capture missing or unsafe")
        manifest = _validate_manifest(json.loads(path.read_bytes()))
        if manifest["capture_id"] != capture_id:
            raise RetrievalError("Metadata capture identity differs")
        if self._report(manifest["snapshots"], manifest["input"]) != manifest["report"]:
            raise RetrievalError("Metadata capture report differs from verified snapshots")
        return manifest

    @staticmethod
    def _zip_write(archive, name, raw):
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, raw)

    def export_capture(self, capture_id, destination):
        manifest = self.report(capture_id)
        target = Path(destination).resolve()
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ssdu-capture-", dir=target.parent) as temporary:
            root = Path(temporary)
            files = {}
            for sid in sorted({item["snapshot_id"] for item in manifest["snapshots"]}):
                path = root / f"{sid}.zip"
                self.snapshots.export(sid, path)
                raw = path.read_bytes()
                files[f"snapshots/{sid}.zip"] = {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                }
            bundle = {"schema_version": 1, "capture": manifest, "files": files}
            if sum(item["bytes"] for item in files.values()) > MAX_BUNDLE_BYTES:
                raise RetrievalError("Metadata capture bundle exceeds size bound")
            bundle_raw = canonical(bundle) + b"\n"
            if len(bundle_raw) > MAX_MANIFEST_BYTES:
                raise RetrievalError("Metadata capture bundle manifest exceeds size bound")
            staged = root / "capture.zip"
            with zipfile.ZipFile(staged, "w") as archive:
                self._zip_write(archive, "manifest.json", bundle_raw)
                for name in sorted(files):
                    self._zip_write(archive, name, (root / Path(name).name).read_bytes())
            # Atomic, exclusive publication leaves no partial destination on failure.
            os.link(staged, target)
        return {
            "capture_id": capture_id,
            "path": str(target),
            "snapshots": len(files),
            "bytes": target.stat().st_size,
        }

    def import_capture(self, source):
        path = Path(source)
        if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
            raise RetrievalError("Metadata capture bundle missing or oversized")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_BUNDLE_MEMBERS or len({item.filename for item in infos}) != len(
                infos
            ):
                raise RetrievalError(
                    "Metadata capture bundle contains duplicate or excessive members"
                )
            names = {item.filename: item for item in infos}
            if (
                "manifest.json" not in names
                or names["manifest.json"].file_size > MAX_MANIFEST_BYTES
            ):
                raise RetrievalError("Metadata capture bundle manifest missing or oversized")
            bundle = json.loads(archive.read("manifest.json"))
            if (
                not isinstance(bundle, dict)
                or set(bundle) != {"schema_version", "capture", "files"}
                or bundle["schema_version"] != 1
                or not isinstance(bundle["files"], dict)
            ):
                raise RetrievalError("Invalid metadata capture bundle")
            manifest = _validate_manifest(bundle["capture"])
            expected = {f"snapshots/{item['snapshot_id']}.zip" for item in manifest["snapshots"]}
            if set(bundle["files"]) != expected or set(names) != expected | {"manifest.json"}:
                raise RetrievalError("Metadata capture bundle inventory differs")
            total = 0
            for name, integrity in bundle["files"].items():
                if (
                    not isinstance(integrity, dict)
                    or set(integrity) != {"sha256", "bytes"}
                    or type(integrity["bytes"]) is not int
                    or not 0 <= integrity["bytes"] <= 600_000_000
                ):
                    raise RetrievalError("Invalid metadata capture bundle file bounds")
                TypeAdapter(SnapshotId).validate_python(integrity["sha256"])
                total += integrity["bytes"]
                if total > MAX_BUNDLE_BYTES or names[name].file_size != integrity["bytes"]:
                    raise RetrievalError("Metadata capture bundle exceeds declared bounds")
            with tempfile.TemporaryDirectory(prefix="ssdu-capture-import-") as temporary:
                staging = MetadataStore(Path(temporary) / "store")
                for name in sorted(expected):
                    integrity = bundle["files"][name]
                    target = Path(temporary) / Path(name).name
                    digest, count = hashlib.sha256(), 0
                    with archive.open(name) as member, target.open("xb") as output:
                        while block := member.read(1024 * 1024):
                            count += len(block)
                            if count > integrity["bytes"]:
                                raise RetrievalError(
                                    "Metadata capture member exceeds declared size"
                                )
                            digest.update(block)
                            output.write(block)
                    if count != integrity["bytes"] or digest.hexdigest() != integrity["sha256"]:
                        raise RetrievalError("Metadata capture member hash differs")
                    sid = Path(name).stem
                    imported = staging.import_bundle(target)
                    if imported != sid:
                        raise RetrievalError("Metadata capture nested snapshot identity differs")
                # Validate the whole campaign before writing anything to the destination.
                verifier = MetadataCapture(Path(temporary) / "store", clock=self.clock)
                if verifier._report(manifest["snapshots"], manifest["input"]) != manifest["report"]:
                    raise RetrievalError("Metadata capture report differs from bundled snapshots")
                for name in sorted(expected):
                    sid = self.snapshots.import_bundle(Path(temporary) / Path(name).name)
                    MetadataIndex(self.store.root).import_snapshot(sid)
                self._retain(manifest)
        return {
            "capture_id": manifest["capture_id"],
            "snapshots": len(expected),
            "records": manifest["report"]["records"],
            "completeness": manifest["report"]["status"],
        }
