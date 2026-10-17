"""Rebuildable current discovery metadata, separate from pinned data interpretations."""

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ssdu.metadata_contract import record_version
from ssdu.store import canonical


def _time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Metadata timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _stamp(value):
    return _time(value).isoformat() if value else None


def _page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if type(offset) is not int or not 0 <= offset <= 100_000:
        raise ValueError("offset must be an integer between 0 and 100000")


def _scope(report):
    return hashlib.sha256(canonical(report.get("scope", {}))).hexdigest()


def _rank(checked, captured, snapshot_id):
    # Replaying cached evidence today is not a new check of the provider.
    return bool(checked), checked or captured, captured, snapshot_id


def _current_rank(row):
    rank = _rank(row["checked_at"], row["captured_at"], row["snapshot_id"])
    if row["status"] != "active":
        checked = row["status_at"] if row["checked_at"] or row["status"] == "not_observed" else None
        rank = max(rank, _rank(checked, row["status_at"], row["status_snapshot_id"]))
    return rank


def _detail_rank(document, report):
    """Prefer a verified description when source check times are equal."""
    source = document.get("source_metadata", {})
    detail = report.get("scope", {}).get("detail")
    described = (
        isinstance(source.get("descriptor"), dict)
        if "descriptor" in source
        else detail in {"dataset", "codes"}
    )
    version = str(document.get("mapping_version", "")).split(".")
    version = (
        tuple(map(int, version))
        if len(version) == 3 and all(part.isdigit() for part in version)
        else (0, 0, 0)
    )
    return described, version, detail == "codes"


def _inventory(report, count):
    completeness = report["completeness"]
    scope = report.get("scope", {})
    return (
        completeness.get("status") == "complete"
        and completeness.get("catalogue_complete") is True
        and completeness.get("inventory_authoritative") is True
        and not scope.get("query")
        and not scope.get("search")
        and not scope.get("dataset_ids")
        and count > 0
        and report["freshness"].get("last_checked_at") is not None
    )


PROJECTION_VERSION = "1.0.0"
_DESCRIPTOR_FIELDS = (
    "description",
    "kind",
    "dimensions",
    "measures",
    "distributions",
    "release",
    "coverage",
    "access",
    "licence",
    "publishers",
    "subjects",
    "languages",
    "related",
)


def _revision_value(value):
    if not isinstance(value, str):
        return canonical(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else parsed.isoformat()


def _compatibility(current, descriptor):
    """Identity alone permits a labelled projection, never revision validation."""
    comparisons = []
    for key in ("provider_modified_at",):
        if current.get(key) and descriptor.get(key):
            comparisons.append(_revision_value(current[key]) == _revision_value(descriptor[key]))
    a, b = current.get("release"), descriptor.get("release")
    if isinstance(a, dict) and isinstance(b, dict):
        for key in ("revision_id", "revision", "version", "edition", "release_id"):
            if a.get(key) is not None and b.get(key) is not None:
                comparisons.append(_revision_value(a[key]) == _revision_value(b[key]))
    if comparisons:
        return "revision_match" if all(comparisons) else "revision_conflict"
    return "identity_only"


def _search_columns(document):
    """Bounded public prose/labels; native documents and provenance never become terms."""
    parts, remaining = [], 100_000

    def add(value):
        nonlocal remaining
        if isinstance(value, str) and remaining > 0:
            text = value[: min(8192, remaining)].strip()
            if text:
                parts.append(text)
                remaining -= len(text) + 1

    def labels(value, depth=0):
        if remaining <= 0 or depth > 8:
            return
        if isinstance(value, list):
            for item in value[:10_000]:
                labels(item, depth + 1)
        elif isinstance(value, dict):
            for key in ("id", "label", "name", "title", "description", "code", "unit"):
                item = value.get(key)
                if isinstance(item, str):
                    add(item)
                elif key == "unit":
                    labels(item, depth + 1)
            for key in ("values", "categories", "dimensions", "areas", "periods", "levels"):
                labels(value.get(key), depth + 1)

    for key in ("subjects", "publishers", "dimensions", "measures", "distributions", "related"):
        labels(document.get(key))
    coverage = document.get("coverage")
    if isinstance(coverage, dict):
        labels(coverage.get("spatial"))
        labels(coverage.get("temporal"))
    licence = document.get("licence")
    add(licence) if isinstance(licence, str) else labels(licence)
    for language in document.get("languages", []):
        add(language)
    return (
        document.get("title", "")[:8192],
        (document.get("description") or "")[:100_000],
        "\n".join(parts),
    )


class MetadataIndex:
    """Offline lexical discovery over verified immutable metadata snapshots.

    This index never writes ``Registry`` or changes metadata pinned to observations.
    It retains historical versions and treats absence as an inference, not deletion.
    """

    def __init__(self, root="data", *, clock=None, _path=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = Path(_path) if _path else self.root / "metadata-index.sqlite"
        self.clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS imports (
                    snapshot_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    metadata_version TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    snapshot_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
                    metadata_version TEXT NOT NULL, PRIMARY KEY(snapshot_id,dataset_id)
                );
                CREATE INDEX IF NOT EXISTS observations_dataset ON observations(dataset_id);
                CREATE TABLE IF NOT EXISTS current (
                    dataset_id TEXT PRIMARY KEY, provider TEXT NOT NULL, native_id TEXT NOT NULL,
                    metadata_version TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL, checked_at TEXT, captured_at TEXT NOT NULL,
                    status TEXT NOT NULL, status_snapshot_id TEXT NOT NULL, status_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_runs (
                    provider TEXT NOT NULL, scope_key TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    checked_at TEXT, captured_at TEXT NOT NULL, report TEXT NOT NULL,
                    PRIMARY KEY(provider,scope_key,snapshot_id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS discovery_search USING fts5(
                    dataset_id UNINDEXED, title, description, metadata_text
                );
                CREATE TABLE IF NOT EXISTS projection_state (version TEXT PRIMARY KEY);
            """)
        # Only derived rows are migrated. Immutable documents and import/history
        # provenance remain byte-for-byte unchanged.
        with self._locked(), self.connect() as db:
            if (
                db.execute(
                    "SELECT 1 FROM projection_state WHERE version=?", (PROJECTION_VERSION,)
                ).fetchone()
                is None
            ):
                db.execute("DELETE FROM discovery_search")
                for row in db.execute(
                    "SELECT c.*,v.document FROM current c JOIN versions v "
                    "ON c.metadata_version=v.metadata_version"
                ).fetchall():
                    self._refresh_search(db, row)
                db.execute("DELETE FROM projection_state")
                db.execute("INSERT INTO projection_state VALUES (?)", (PROJECTION_VERSION,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _locked(self):
        with self.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _age_limit(max_age_seconds):
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or max_age_seconds < 0
        ):
            raise ValueError("max_age_seconds must be finite and nonnegative")

    def _freshness(self, checked, captured, modified, max_age_seconds):
        self._age_limit(max_age_seconds)
        current = self.clock()
        if isinstance(current, str):
            current = _time(current)
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            current = datetime.fromtimestamp(current, UTC)
        else:
            if current.tzinfo is None:
                raise ValueError("Metadata clock must include a timezone")
            current = current.astimezone(UTC)
        age = (current - _time(checked)).total_seconds() if checked else None
        state = (
            "unknown"
            if age is None or age < 0
            else ("fresh" if age <= max_age_seconds else "stale")
        )
        return {
            "state": state,
            "last_checked_at": checked,
            "captured_at": captured,
            "provider_modified_at": modified,
            "age_seconds": age,
            "max_age_seconds": max_age_seconds,
        }

    def import_snapshot(self, snapshot_id):
        """Verify every retained byte before atomically updating discovery views."""
        from ssdu.metadata_store import MetadataStore

        store = MetadataStore(self.root)
        manifest = store.verify(snapshot_id)
        document = store.read(snapshot_id)
        reports = {row["provider"]: row for row in document["providers"]}
        resources = manifest.get("files", {})
        with self._locked(), self.connect() as db:
            previous = db.execute(
                "SELECT report FROM imports WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if previous:
                return {**json.loads(previous[0]), "reused": True}
            updated, retired, ignored = 0, 0, 0
            affected = set()
            for source in document["records"]:
                row = copy.deepcopy(source)
                provider = row["provider"]
                report = reports[provider]
                key = row["dataset_id"]
                affected.add(key)
                checked = _stamp(
                    row.get("last_checked_at", report["freshness"].get("last_checked_at"))
                )
                captured = _stamp(row.get("captured_at") or document["captured_at"])
                row["metadata_version"] = record_version(
                    row, contract_version=document["contract_version"]
                )
                row["raw_resource_hashes"] = {
                    path: resources.get(path, resources.get("resources/" + path, {})).get("sha256")
                    for path in row.get("raw_resources", [])
                }
                version = hashlib.sha256(canonical(row)).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO versions VALUES (?,?,?)",
                    (version, key, canonical(row).decode()),
                )
                db.execute("INSERT INTO observations VALUES (?,?,?)", (snapshot_id, key, version))
                old = db.execute("SELECT * FROM current WHERE dataset_id=?", (key,)).fetchone()
                candidate = _rank(checked, captured, snapshot_id)
                previous = _current_rank(old) if old else None
                if (
                    old
                    and old["status"] == "active"
                    and not row.get("deleted")
                    and not row.get("tombstone")
                    and candidate[:2] == previous[:2]
                ):
                    prior_document = json.loads(
                        db.execute(
                            "SELECT document FROM versions WHERE metadata_version=?",
                            (old["metadata_version"],),
                        ).fetchone()[0]
                    )
                    prior_report = json.loads(
                        db.execute(
                            "SELECT report FROM provider_runs WHERE provider=? AND snapshot_id=?",
                            (provider, old["snapshot_id"]),
                        ).fetchone()[0]
                    )
                    candidate = (*candidate[:2], _detail_rank(row, report), *candidate[2:])
                    previous = (
                        *previous[:2],
                        _detail_rank(prior_document, prior_report),
                        *previous[2:],
                    )
                if old and candidate <= previous:
                    ignored += 1
                    continue
                status = "deleted" if row.get("deleted") or row.get("tombstone") else "active"
                db.execute(
                    """INSERT OR REPLACE INTO current VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        key,
                        provider,
                        row["native_id"],
                        version,
                        snapshot_id,
                        _scope(report),
                        checked,
                        captured,
                        status,
                        snapshot_id,
                        checked or captured,
                    ),
                )
                updated += 1
            for provider, report in reports.items():
                checked = _stamp(report["freshness"].get("last_checked_at"))
                captured = _stamp(report["freshness"].get("captured_at") or document["captured_at"])
                scope_key = _scope(report)
                db.execute(
                    "INSERT INTO provider_runs VALUES (?,?,?,?,?,?)",
                    (
                        provider,
                        scope_key,
                        snapshot_id,
                        checked,
                        captured,
                        canonical(report).decode(),
                    ),
                )
                inventories = []
                for run in db.execute(
                    "SELECT * FROM provider_runs WHERE provider=? AND scope_key=?",
                    (provider, scope_key),
                ).fetchall():
                    retained = json.loads(run["report"])
                    if _inventory(retained, retained["completeness"]["discovered_count"]):
                        inventories.append(run)
                if not inventories:
                    continue
                inventory = max(
                    inventories,
                    key=lambda row: _rank(
                        row["checked_at"], row["captured_at"], row["snapshot_id"]
                    ),
                )
                inventory_id = inventory["snapshot_id"]
                inventory_rank = _rank(
                    inventory["checked_at"], inventory["captured_at"], inventory_id
                )
                inventory_keys = {
                    row[0]
                    for row in db.execute(
                        "SELECT dataset_id FROM observations WHERE snapshot_id=?", (inventory_id,)
                    )
                }
                for old in db.execute(
                    "SELECT * FROM current WHERE provider=? AND scope_key=? AND status='active'",
                    (provider, scope_key),
                ).fetchall():
                    if old["dataset_id"] in inventory_keys or inventory_rank <= _current_rank(old):
                        continue
                    db.execute(
                        """UPDATE current SET status='not_observed', status_snapshot_id=?,status_at=?
                        WHERE dataset_id=?""",
                        (inventory_id, inventory["checked_at"], old["dataset_id"]),
                    )
                    affected.add(old["dataset_id"])
                    retired += 1
            # Descriptors arriving after a newer inventory still contribute to the
            # projection, even when they correctly lose the membership rank.
            for key in affected:
                row = db.execute(
                    "SELECT c.*,v.document FROM current c JOIN versions v "
                    "ON c.metadata_version=v.metadata_version WHERE c.dataset_id=?",
                    (key,),
                ).fetchone()
                self._refresh_search(db, row)
            result = {
                "snapshot_id": snapshot_id,
                "indexed": len(document["records"]),
                "updated": updated,
                "ignored_older": ignored,
                "not_observed": retired,
                "reused": False,
            }
            db.execute(
                "INSERT INTO imports VALUES (?,?,?)",
                (snapshot_id, document["captured_at"], canonical(result).decode()),
            )
        return result

    @staticmethod
    def _profile(document, report):
        detail = report["scope"].get("detail", "unknown")
        source = document.get("source_metadata", {})
        described = (
            isinstance(source.get("descriptor"), dict)
            if "descriptor" in source
            else (detail in {"dataset", "codes"})
        )
        return {
            "requested_detail": detail,
            "description_available": described,
            "descriptor_available": described,
            "description_text_available": bool((document.get("description") or "").strip()),
            "scope": report["scope"],
            "completeness": report["completeness"],
        }

    def _project(self, db, row):
        """Separate the current observation from independently checked rich fields."""
        document = json.loads(row["document"])
        report = json.loads(
            db.execute(
                "SELECT report FROM provider_runs WHERE provider=? AND snapshot_id=?",
                (document["provider"], row["snapshot_id"]),
            ).fetchone()[0]
        )
        profile = self._profile(document, report)
        if row["status"] == "historical":
            return document, profile
        current_source = {
            "metadata_snapshot_id": row["snapshot_id"],
            "metadata_version": document["metadata_version"],
            "last_checked_at": row["checked_at"],
            "captured_at": row["captured_at"],
            "provider_modified_at": document.get("provider_modified_at"),
            "mapping_version": document.get("mapping_version"),
            "provider_timestamps": copy.deepcopy(
                document.get("source_metadata", {}).get("provider_timestamps", {})
            ),
        }
        descriptor, descriptor_source, retained = None, None, []
        if profile["description_available"]:
            descriptor, descriptor_source = (
                document,
                {
                    **current_source,
                    "compatibility": "current",
                    "revision_status": "current",
                    "current": True,
                },
            )
        else:
            for prior in db.execute(
                """SELECT o.snapshot_id,v.document,r.report FROM observations o
                JOIN versions v ON o.metadata_version=v.metadata_version
                JOIN provider_runs r ON r.snapshot_id=o.snapshot_id AND r.provider=?
                WHERE o.dataset_id=? AND o.snapshot_id!=?""",
                (document["provider"], document["dataset_id"], row["snapshot_id"]),
            ):
                old, old_report = json.loads(prior["document"]), json.loads(prior["report"])
                if (
                    not self._profile(old, old_report)["description_available"]
                    or old.get("deleted")
                    or old.get("tombstone")
                ):
                    continue
                checked = _stamp(
                    old.get("last_checked_at", old_report["freshness"].get("last_checked_at"))
                )
                captured = _stamp(old.get("captured_at") or old_report["freshness"]["captured_at"])
                rank = _rank(checked, captured, prior["snapshot_id"])
                rank = (*rank[:2], _detail_rank(old, old_report), *rank[2:])
                compatibility = _compatibility(document, old)
                retained.append(
                    (
                        rank,
                        old,
                        {
                            "metadata_snapshot_id": prior["snapshot_id"],
                            "metadata_version": old["metadata_version"],
                            "last_checked_at": checked,
                            "captured_at": captured,
                            "provider_modified_at": old.get("provider_modified_at"),
                            "mapping_version": old.get("mapping_version"),
                            "provider_timestamps": copy.deepcopy(
                                old.get("source_metadata", {}).get("provider_timestamps", {})
                            ),
                            "compatibility": compatibility,
                            "revision_status": {
                                "revision_match": "matched",
                                "identity_only": "unverified",
                                "revision_conflict": "conflicting",
                            }[compatibility],
                            "current": False,
                        },
                    )
                )
            eligible = [
                item for item in retained if item[2]["compatibility"] != "revision_conflict"
            ]
            if eligible and row["status"] == "active":
                _, descriptor, descriptor_source = max(eligible, key=lambda item: item[0])
        field_sources = {key: row["snapshot_id"] for key in _DESCRIPTOR_FIELDS if key in document}
        if descriptor is not None:
            for key in _DESCRIPTOR_FIELDS:
                # A descriptor can omit prose which the catalogue supplies. An
                # absent rich value must not erase a useful current assertion.
                if key in descriptor and (
                    descriptor[key] not in (None, "", [], {})
                    or document.get(key) in (None, "", [], {})
                ):
                    document[key] = copy.deepcopy(descriptor[key])
                    field_sources[key] = descriptor_source["metadata_snapshot_id"]
            for key, status in descriptor.get("field_status", {}).items():
                if (
                    field_sources.get(key.split(".")[0])
                    == descriptor_source["metadata_snapshot_id"]
                ):
                    document.setdefault("field_status", {})[key] = copy.deepcopy(status)
        profile["descriptor_available"] = descriptor is not None
        profile["description_text_available"] = bool((document.get("description") or "").strip())
        document["metadata_projection"] = {
            "version": PROJECTION_VERSION,
            "catalogue": current_source,
            "descriptor": descriptor_source,
            "field_sources": field_sources,
        }
        if retained:
            # Legacy pointer remains explicitly non-current; enriched values have
            # precise compatibility and clocks in metadata_projection.
            best = descriptor_source or max(retained, key=lambda item: item[0])[2]
            document["retained_description"] = {
                **best,
                "compatibility": "unverified"
                if best["compatibility"] == "identity_only"
                else best["compatibility"],
            }
        return document, profile

    def _refresh_search(self, db, row):
        document, _ = self._project(db, row)
        db.execute("DELETE FROM discovery_search WHERE dataset_id=?", (row["dataset_id"],))
        db.execute(
            "INSERT INTO discovery_search VALUES (?,?,?,?)",
            (row["dataset_id"], *_search_columns(document)),
        )

    def _document(self, row, max_age_seconds):
        with self.connect() as db:
            document, profile = self._project(db, row)
        projection = document.get("metadata_projection")
        if projection:
            for source in (projection["catalogue"], projection["descriptor"]):
                if source:
                    source["freshness"] = self._freshness(
                        source["last_checked_at"],
                        source["captured_at"],
                        source["provider_modified_at"],
                        max_age_seconds,
                    )
        document.update(
            {
                "metadata_snapshot_id": row["snapshot_id"],
                "metadata_profile": profile,
                "status": row["status"],
                "status_evidence": {
                    "snapshot_id": row["status_snapshot_id"],
                    "as_of": row["status_at"],
                    "assertion": "inferred_absence"
                    if row["status"] == "not_observed"
                    else ("provider_deletion" if row["status"] == "deleted" else "observed"),
                },
                "freshness": self._freshness(
                    row["checked_at"],
                    row["captured_at"],
                    document.get("provider_modified_at"),
                    max_age_seconds,
                ),
            }
        )
        return document

    def get(self, dataset_id, *, snapshot_id=None, max_age_seconds=86400):
        """Current discovery metadata, or an explicitly identified retained capture.

        Native identifiers must be unambiguous. Historical descriptions retain their
        own capture clocks and never inherit a newer catalogue check time.
        """
        with self.connect() as db:
            rows = db.execute(
                """SELECT c.*,v.document FROM current c JOIN versions v
                ON c.metadata_version=v.metadata_version WHERE c.dataset_id=?""",
                (dataset_id,),
            ).fetchall()
            if not rows:
                rows = db.execute(
                    """SELECT c.*,v.document FROM current c JOIN versions v
                    ON c.metadata_version=v.metadata_version WHERE c.native_id=?""",
                    (dataset_id,),
                ).fetchall()
            if not rows:
                raise KeyError(dataset_id)
            if len(rows) != 1:
                raise ValueError("Dataset identifier is ambiguous; use provider:native_id")
            row = rows[0]
            if snapshot_id is not None:
                capture = db.execute(
                    """SELECT o.snapshot_id,v.document,r.report FROM observations o
                    JOIN versions v ON o.metadata_version=v.metadata_version
                    JOIN provider_runs r ON r.snapshot_id=o.snapshot_id AND r.provider=?
                    WHERE o.dataset_id=? AND o.snapshot_id=?""",
                    (row["provider"], row["dataset_id"], snapshot_id),
                ).fetchone()
                if capture is None:
                    raise KeyError(f"{row['dataset_id']} in metadata snapshot {snapshot_id}")
                document, report = json.loads(capture["document"]), json.loads(capture["report"])
                checked = _stamp(
                    document.get("last_checked_at", report["freshness"].get("last_checked_at"))
                )
                captured = _stamp(document.get("captured_at") or report["freshness"]["captured_at"])
                row = {
                    "document": capture["document"],
                    "snapshot_id": snapshot_id,
                    "status": "historical",
                    "status_snapshot_id": snapshot_id,
                    "status_at": checked or captured,
                    "checked_at": checked,
                    "captured_at": captured,
                }
        return self._document(row, max_age_seconds)

    def search(
        self,
        query="",
        *,
        provider=None,
        limit=20,
        offset=0,
        include_retired=False,
        max_age_seconds=86400,
    ):
        _page(limit, offset)
        self._age_limit(max_age_seconds)
        if not isinstance(query, str) or len(query) > 4096:
            raise ValueError("query must be a string of at most 4096 characters")
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if len(tokens) > 64:
            raise ValueError("query must contain at most 64 terms")
        clauses, params = [], []
        join, order = "", "c.dataset_id"
        if tokens:
            join = "JOIN discovery_search f ON f.dataset_id=c.dataset_id"
            clauses.append("discovery_search MATCH ?")
            params.append(" AND ".join('"' + value + '"*' for value in tokens))
            order = "bm25(discovery_search),c.dataset_id"
        if provider is not None:
            clauses.append("c.provider=?")
            params.append(provider)
        if not include_retired:
            clauses.append("c.status='active'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base = f"FROM current c {join}{where}"
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            rows = db.execute(
                f"""SELECT c.*,v.document FROM current c JOIN versions v
                ON c.metadata_version=v.metadata_version {join}{where}
                ORDER BY {order} LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
        return {
            "items": [self._document(row, max_age_seconds) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def history(self, dataset_id, *, limit=20, offset=0):
        """Page capture provenance without changing the current discovery record."""
        _page(limit, offset)
        key = self.get(dataset_id)["dataset_id"]
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM observations WHERE dataset_id=?", (key,)
            ).fetchone()[0]
            rows = db.execute(
                """SELECT o.snapshot_id,v.document,i.captured_at FROM observations o
                JOIN versions v ON o.metadata_version=v.metadata_version
                JOIN imports i ON o.snapshot_id=i.snapshot_id WHERE o.dataset_id=?
                ORDER BY i.captured_at DESC,o.snapshot_id LIMIT ? OFFSET ?""",
                (key, limit, offset),
            ).fetchall()
        return {
            "items": [
                {
                    "snapshot_id": row["snapshot_id"],
                    "captured_at": row["captured_at"],
                    "metadata_version": json.loads(row["document"])["metadata_version"],
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def status(self, provider=None, *, max_age_seconds=86400):
        """Report observed freshness and scope; partial runs never imply complete coverage."""
        self._age_limit(max_age_seconds)
        with self.connect() as db:
            runs = db.execute(
                "SELECT * FROM provider_runs" + (" WHERE provider=?" if provider else ""),
                (provider,) if provider else (),
            ).fetchall()
            counts = db.execute(
                "SELECT provider,status,COUNT(*) n FROM current GROUP BY provider,status"
            ).fetchall()
            snapshot_count = db.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        latest, scopes = {}, {}
        for run in runs:
            key = run["provider"]
            rank = _rank(run["checked_at"], run["captured_at"], run["snapshot_id"])
            for target, target_key in ((latest, key), (scopes, (key, run["scope_key"]))):
                old = target.get(target_key)
                if old is None or rank > _rank(
                    old["checked_at"], old["captured_at"], old["snapshot_id"]
                ):
                    target[target_key] = run
        providers = []
        for key, run in sorted(latest.items()):
            report = json.loads(run["report"])
            report["snapshot_id"] = run["snapshot_id"]
            report["freshness"] = self._freshness(
                run["checked_at"],
                run["captured_at"],
                report["freshness"].get("provider_modified_at"),
                max_age_seconds,
            )
            report["indexed_counts"] = {
                row["status"]: row["n"] for row in counts if row["provider"] == key
            }
            report["scope_count"] = sum(1 for p, _ in scopes if p == key)
            providers.append(report)
        return {
            "providers": providers,
            "dataset_count": sum(sum(p["indexed_counts"].values()) for p in providers),
            "snapshot_count": snapshot_count,
        }

    def rebuild(self):
        """Atomically replace only this derived index from verified retained snapshots."""
        from ssdu.metadata_store import MetadataStore

        with self._locked():
            store = MetadataStore(self.root)
            snapshot_ids, offset = [], 0
            while True:
                page = store.list(limit=100, offset=offset)
                snapshot_ids.extend(row["snapshot_id"] for row in page["rows"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            for snapshot_id in snapshot_ids:
                store.verify(snapshot_id)
            handle, temporary = tempfile.mkstemp(prefix=".metadata-index-", dir=self.root)
            os.close(handle)
            temporary = Path(temporary)
            try:
                rebuilt = MetadataIndex(self.root, clock=self.clock, _path=temporary)
                for snapshot_id in sorted(snapshot_ids):
                    rebuilt.import_snapshot(snapshot_id)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
                temporary.with_suffix(".lock").unlink(missing_ok=True)
        return {"snapshot_count": len(snapshot_ids), "status": "rebuilt"}
