"""Immutable, chunked, portable metadata graphs and their retained HTTP evidence.

Entities, edges and request records are canonical JSONL shards, never a single
whole-catalogue JSON document. Sorting and reference diagnostics use temporary
SQLite tables, so verification does not load the graph into Python memory.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .connector_cache import CachedClient, _atomic
from .http import Resource
from .metadata_contract import MetadataError, canonical, safe_name
from .metadata_graph import (
    ENTITY_KINDS,
    GRAPH_CONTRACT,
    GRAPH_CONTRACT_VERSION,
    MAX_ROW_BYTES,
    SHA256_PATTERN,
    validate_entity,
    validate_link,
    validate_provenance,
    validate_url,
)

MAX_RESOURCE_BYTES = 25_000_000
MAX_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
MAX_RESOURCES = 500_000
MAX_ENTITIES = 2_000_000
MAX_LINKS = 8_000_000
MAX_CHUNK_BYTES = 16 * 1024 * 1024
MAX_CHUNK_ROWS = 10_000
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
MAX_FILES = MAX_RESOURCES + 50_000
MAX_EXPANSION_RATIO = 1_000
CHUNK_NAME = re.compile(r"(entities|links|resources)/[0-9]{7}\.jsonl")
RAW_NAME = re.compile(r"raw/([0-9a-f]{64})\.bin")


class _Inventory(dict):
    """Track aggregate bytes without rescanning an expanding file inventory."""

    total = 0


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise MetadataError("Duplicate graph JSON field")
        value[key] = item
    return value


def _parse(raw, budget=MAX_ROW_BYTES):
    if not isinstance(raw, bytes) or len(raw) > budget:
        raise MetadataError("Graph JSON exceeds byte budget")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        canonical(value)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise MetadataError(f"Invalid graph JSON: {exc}") from exc


def _integrity(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _hash_id(value):
    if not isinstance(value, str) or not re.fullmatch(SHA256_PATTERN, value):
        raise MetadataError("Graph snapshot and entity IDs require full lowercase SHA-256")
    return value


def _manifest(raw, snapshot_id):
    value = _parse(raw, MAX_MANIFEST_BYTES)
    _hash_id(snapshot_id)
    expected = {
        "schema_version",
        "contract",
        "contract_version",
        "kind",
        "snapshot_id",
        "counts",
        "files",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise MetadataError("Unsupported graph manifest fields")
    identity = {key: item for key, item in value.items() if key != "snapshot_id"}
    if (
        value["snapshot_id"] != snapshot_id
        or hashlib.sha256(canonical(identity)).hexdigest() != snapshot_id
        or raw != canonical(value) + b"\n"
    ):
        raise MetadataError("Graph manifest identity or canonical encoding mismatch")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["contract"] != GRAPH_CONTRACT
        or value["contract_version"] != GRAPH_CONTRACT_VERSION
        or value["kind"] != "metadata_graph"
    ):
        raise MetadataError("Unsupported graph manifest contract")
    files = value["files"]
    if (
        not isinstance(files, dict)
        or not 1 <= len(files) <= MAX_FILES
        or "report.json" not in files
    ):
        raise MetadataError("Invalid graph file inventory")
    total = 0
    chunks = {key: [] for key in ("entities", "links", "resources")}
    for name, record in files.items():
        safe_name(name)
        chunk, raw_file = CHUNK_NAME.fullmatch(name), RAW_NAME.fullmatch(name)
        if name != "report.json" and not chunk and not raw_file:
            raise MetadataError("Unexpected graph manifest path")
        budget = MAX_REPORT_BYTES if name == "report.json" else MAX_CHUNK_BYTES
        if raw_file:
            budget = MAX_RESOURCE_BYTES
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "bytes"}
            or not isinstance(record["sha256"], str)
            or not re.fullmatch(SHA256_PATTERN, record["sha256"])
            or type(record["bytes"]) is not int
            or not 0 <= record["bytes"] <= budget
            or (raw_file and raw_file[1] != record["sha256"])
        ):
            raise MetadataError("Invalid graph file integrity record")
        if chunk:
            chunks[chunk[1]].append(name)
        total += record["bytes"]
    if total > MAX_TOTAL_BYTES:
        raise MetadataError("Graph snapshot exceeds aggregate byte budget")
    for kind, names in chunks.items():
        if sorted(names) != [f"{kind}/{number:07d}.jsonl" for number in range(len(names))]:
            raise MetadataError("Graph shards must be contiguous and numbered from zero")
    counts = value["counts"]
    if not isinstance(counts, dict) or set(counts) != {"entities", "links", "resources", "by_kind"}:
        raise MetadataError("Invalid graph counts")
    for kind, maximum in (
        ("entities", MAX_ENTITIES),
        ("links", MAX_LINKS),
        ("resources", MAX_RESOURCES),
    ):
        if type(counts[kind]) is not int or not 0 <= counts[kind] <= maximum:
            raise MetadataError("Graph count exceeds budget")
    by_kind = counts["by_kind"]
    if (
        not isinstance(by_kind, dict)
        or set(by_kind) - ENTITY_KINDS
        or any(type(count) is not int or count < 0 for count in by_kind.values())
        or sum(by_kind.values()) != counts["entities"]
    ):
        raise MetadataError("Invalid graph entity-kind counts")
    return value


@contextmanager
def _database(directory):
    descriptor, name = tempfile.mkstemp(prefix=".graph-check-", suffix=".sqlite", dir=directory)
    os.close(descriptor)
    db = sqlite3.connect(name)
    try:
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-8192")
        db.executescript(
            "CREATE TABLE entities(id TEXT PRIMARY KEY,kind TEXT NOT NULL,body BLOB NOT NULL);"
            "CREATE TABLE links(id TEXT PRIMARY KEY,source TEXT,target TEXT,body BLOB NOT NULL);"
            "CREATE INDEX inbound ON links(target);CREATE INDEX outbound ON links(source);"
            "CREATE TABLE resources(id TEXT PRIMARY KEY,url TEXT,digest TEXT,captured TEXT,checked TEXT,body BLOB NOT NULL);"
            "CREATE INDEX evidence ON resources(url,digest);"
        )
        yield db
    finally:
        db.close()
        Path(name).unlink(missing_ok=True)


def _insert(db, table, value):
    raw = canonical(value)
    key = value["id"] if table == "entities" else hashlib.sha256(raw).hexdigest()
    old = db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
    if old is not None:
        if old[0] != raw:
            raise MetadataError("Conflicting graph entities share a qualified identity")
        return False
    if table == "entities":
        db.execute("INSERT INTO entities VALUES(?,?,?)", (key, value["kind"], raw))
    elif table == "links":
        db.execute(
            "INSERT INTO links VALUES(?,?,?,?)", (key, value["source"], value["target"], raw)
        )
    else:
        db.execute(
            "INSERT INTO resources VALUES(?,?,?,?,?,?)",
            (
                key,
                value["url"],
                value["body_sha256"],
                value["captured_at"],
                value["checked_at"],
                raw,
            ),
        )
    return True


def _resource_record(resource):
    if not isinstance(resource, Resource) or not isinstance(resource.body, bytes):
        raise MetadataError("Graph resources must be retained HTTP Resource instances")
    if len(resource.body) > MAX_RESOURCE_BYTES:
        raise MetadataError("Graph raw response exceeds 25 MB byte budget")
    record = {
        "url": resource.url,
        "method": resource.method,
        "request_body": resource.request_body,
        "cache_scope": resource.cache_scope,
        "content_type": resource.content_type,
        "headers": resource.headers,
        "body_sha256": hashlib.sha256(resource.body).hexdigest(),
        "captured_at": resource.captured_at,
        "checked_at": resource.checked_at,
    }
    return _validate_resource(record)


def _validate_resource(value):
    if not isinstance(value, dict) or set(value) != {
        "url",
        "method",
        "request_body",
        "cache_scope",
        "content_type",
        "headers",
        "body_sha256",
        "captured_at",
        "checked_at",
    }:
        raise MetadataError("Invalid graph retained request fields")
    validate_url(value["url"])
    validate_provenance(
        [
            {key: value[key] for key in ("url", "body_sha256", "captured_at", "checked_at")}
            | {"pointer": ""}
        ]
    )
    if (
        not isinstance(value["method"], str)
        or value["method"] not in {"GET", "POST"}
        or not isinstance(value["content_type"], str)
        or not isinstance(value["headers"], dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value["headers"].items()
        )
        or (
            value["cache_scope"] is not None
            and (
                not isinstance(value["cache_scope"], str)
                or not 1 <= len(value["cache_scope"]) <= 500
            )
        )
    ):
        raise MetadataError("Invalid graph retained HTTP request metadata")
    if len(canonical(value)) > MAX_ROW_BYTES:
        raise MetadataError("Graph retained request exceeds row budget")
    return value


def _json_pointer(value, pointer):
    for segment in pointer.split("/")[1:]:
        key = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif (
            isinstance(value, list)
            and re.fullmatch(r"0|[1-9][0-9]*", key)
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            raise MetadataError("Graph evidence pointer does not resolve in retained response")


def _check_evidence(db, path, provenance, parsed_cache):
    for ref in provenance:
        rows = db.execute(
            "SELECT captured,checked FROM resources WHERE url=? AND digest=?",
            (ref["url"], ref["body_sha256"]),
        )
        if not any(row == (ref["captured_at"], ref["checked_at"]) for row in rows):
            raise MetadataError("Graph evidence lacks a retained URL/hash/capture-clock match")
        if ref["pointer"]:
            digest = ref["body_sha256"]
            if parsed_cache.get("digest") != digest:
                parsed_cache.clear()
                raw = path / "raw" / f"{digest}.bin"
                with raw.open("rb") as stream:
                    body = stream.read(MAX_RESOURCE_BYTES + 1)
                parsed_cache.update(digest=digest, value=_parse(body, MAX_RESOURCE_BYTES))
            _json_pointer(parsed_cache["value"], ref["pointer"])


def _counts(db):
    return {
        "entities": db.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "links": db.execute("SELECT COUNT(*) FROM links").fetchone()[0],
        "resources": db.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
        "by_kind": dict(db.execute("SELECT kind,COUNT(*) FROM entities GROUP BY kind")),
    }


def _diagnostics(db):
    missing = "FROM links l LEFT JOIN entities s ON s.id=l.source LEFT JOIN entities t ON t.id=l.target WHERE s.id IS NULL OR t.id IS NULL"
    count = db.execute(f"SELECT COUNT(*) {missing}").fetchone()[0]
    sample = [
        {"source": source, "target": target}
        for source, target in db.execute(
            f"SELECT l.source,l.target {missing} ORDER BY l.id LIMIT 20"
        )
    ]
    # Kahn's algorithm in temporary SQL tables uses bounded Python working memory.
    # Residual nodes include cycles and their descendants; this is stated explicitly.
    db.executescript(
        "CREATE TEMP TABLE degrees(id TEXT PRIMARY KEY,degree INTEGER NOT NULL);"
        "INSERT INTO degrees SELECT e.id,COUNT(s.id) FROM entities e "
        "LEFT JOIN links l ON l.target=e.id LEFT JOIN entities s ON s.id=l.source GROUP BY e.id;"
        "CREATE INDEX degree_queue ON degrees(degree);"
        "CREATE TEMP TABLE roots(id TEXT PRIMARY KEY);"
    )
    try:
        while True:
            db.execute("INSERT INTO roots SELECT id FROM degrees WHERE degree=0")
            if not db.execute("SELECT 1 FROM roots LIMIT 1").fetchone():
                break
            db.execute(
                "UPDATE degrees SET degree=degree-(SELECT COUNT(*) FROM links l JOIN roots r ON r.id=l.source WHERE l.target=degrees.id) "
                "WHERE id IN (SELECT l.target FROM links l JOIN roots r ON r.id=l.source)"
            )
            db.execute("DELETE FROM degrees WHERE id IN (SELECT id FROM roots)")
            db.execute("DELETE FROM roots")
        residual = db.execute("SELECT COUNT(*) FROM degrees").fetchone()[0]
        cycle_sample = [row[0] for row in db.execute("SELECT id FROM degrees ORDER BY id LIMIT 20")]
    finally:
        db.executescript("DROP TABLE degrees;DROP TABLE roots;")
    return {
        "dangling_links": {"count": count, "sample": sample},
        "cycles": {
            "present": bool(residual),
            "residual_nodes": residual,
            "sample_ids": cycle_sample,
            "scope": "directed links between retained entities; residual nodes are in or downstream of cycles",
        },
    }


def _page(limit, offset):
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or not 0 <= offset <= MAX_LINKS
    ):
        raise MetadataError("Use graph limit=1..100 and a bounded nonnegative integer offset")


class MetadataGraphStore:
    """Append-only deep metadata with bounded verification and portable raw evidence.

    Limits: 2 million entities, 8 million links, 500,000 retained requests,
    25 MB per raw response, 16 MiB/10,000-row JSONL shards, 2 MiB per row,
    4 MiB reports, 128 MiB manifests and 16 GiB aggregate uncompressed files.
    """

    def __init__(self, root="data"):
        self.root = Path(root).resolve()
        self.directory = self.root / "metadata"
        self.snapshots = self.directory / "graphs"
        self._directories()

    def _directories(self):
        for path in (self.directory, self.snapshots):
            if path.is_symlink():
                raise MetadataError("Graph store directories cannot be symlinks")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise MetadataError("Graph store path is not a directory")

    def path(self, snapshot_id):
        self._directories()
        path = self.snapshots / _hash_id(snapshot_id)
        if path.is_symlink() or not path.is_dir():
            raise MetadataError("Graph snapshot missing or unsafe")
        return path

    def _rows(self, path, manifest, table):
        previous = None
        for name in sorted(name for name in manifest["files"] if name.startswith(f"{table}/")):
            count = 0
            with (path / name).open("rb") as stream:
                while raw := stream.readline(MAX_ROW_BYTES + 2):
                    if len(raw) > MAX_ROW_BYTES + 1 or not raw.endswith(b"\n"):
                        raise MetadataError("Oversized or unterminated graph JSONL row")
                    value = _parse(raw[:-1])
                    if not isinstance(value, dict) or raw != canonical(value) + b"\n":
                        raise MetadataError("Graph JSONL rows must be canonical objects")
                    key = (
                        value.get("id")
                        if table == "entities"
                        else hashlib.sha256(canonical(value)).hexdigest()
                    )
                    _hash_id(key)
                    if previous is not None and key <= previous:
                        raise MetadataError("Graph JSONL identities must be unique and sorted")
                    previous = key
                    count += 1
                    if count > MAX_CHUNK_ROWS:
                        raise MetadataError("Graph shard exceeds row budget")
                    yield value
            if not count:
                raise MetadataError("Empty graph JSONL shards are not canonical")

    def _verified(self, snapshot_id, path=None):
        path = self.path(snapshot_id) if path is None else path
        try:
            manifest_path = path / "manifest.json"
            if (
                manifest_path.is_symlink()
                or not manifest_path.is_file()
                or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
            ):
                raise MetadataError("Unsafe or oversized graph manifest")
            manifest = _manifest(manifest_path.read_bytes(), snapshot_id)
            actual = set()
            for parent, directories, names in os.walk(path, followlinks=False):
                if any((Path(parent) / name).is_symlink() for name in directories):
                    raise MetadataError("Graph snapshot contains symlink directories")
                for name in names:
                    target = Path(parent) / name
                    if target.is_symlink() or not target.is_file():
                        raise MetadataError("Graph snapshot contains unsafe files")
                    actual.add(target.relative_to(path).as_posix())
            if actual != {"manifest.json", *manifest["files"]}:
                raise MetadataError("Graph snapshot has missing or extra members")
            for name, expected in manifest["files"].items():
                target = path / name
                if target.stat().st_size != expected["bytes"]:
                    raise MetadataError("Graph file size mismatch")
                with target.open("rb") as stream:
                    _copy(stream, None, expected)
            with _database(self.snapshots) as db:
                raw_hashes = set()
                for resource_count, record in enumerate(self._rows(path, manifest, "resources"), 1):
                    if resource_count > MAX_RESOURCES:
                        raise MetadataError("Graph retained request count exceeds budget")
                    _validate_resource(record)
                    digest = record["body_sha256"]
                    if f"raw/{digest}.bin" not in manifest["files"]:
                        raise MetadataError("Graph resource body is absent from verified inventory")
                    raw_hashes.add(digest)
                    _insert(db, "resources", record)
                if raw_hashes != {
                    RAW_NAME.fullmatch(name)[1]
                    for name in manifest["files"]
                    if RAW_NAME.fullmatch(name)
                }:
                    raise MetadataError("Unreferenced raw response in graph snapshot")
                parsed_cache = {}
                for table, validator, maximum in (
                    ("entities", validate_entity, MAX_ENTITIES),
                    ("links", validate_link, MAX_LINKS),
                ):
                    for count, value in enumerate(self._rows(path, manifest, table), 1):
                        if count > maximum:
                            raise MetadataError(f"Graph {table} count exceeds budget")
                        normalized = validator(value)
                        if canonical(normalized) != canonical(value):
                            raise MetadataError("Graph rows are not normalized")
                        _check_evidence(db, path, value["provenance"], parsed_cache)
                        _insert(db, table, value)
                if _counts(db) != manifest["counts"]:
                    raise MetadataError("Graph manifest counts disagree with rows")
                raw_report = (path / "report.json").read_bytes()
                report = _parse(raw_report, MAX_REPORT_BYTES)
                if not isinstance(report, dict) or raw_report != canonical(report) + b"\n":
                    raise MetadataError("Graph report must be a canonical object")
                if report.get("graph_validation") != _diagnostics(db):
                    raise MetadataError(
                        "Graph report reference diagnostics disagree with retained graph"
                    )
            return manifest
        except (OSError, KeyError, TypeError, RecursionError, sqlite3.Error) as exc:
            raise MetadataError(f"Invalid metadata graph snapshot: {exc}") from exc

    def verify(self, snapshot_id):
        return self._verified(snapshot_id)

    def _install(self, stage, snapshot_id):
        self._verified(snapshot_id, stage)
        lock_path = self.directory / ".graph-publication.lock"
        if lock_path.is_symlink():
            raise MetadataError("Unsafe graph publication lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            destination = self.snapshots / snapshot_id
            if destination.exists() or destination.is_symlink():
                self.verify(snapshot_id)
            else:
                os.rename(stage, destination)
        return snapshot_id

    def _write_shards(self, stage, db, table, files):
        raw, number, rows = bytearray(), 0, 0
        for (body,) in db.execute(f"SELECT body FROM {table} ORDER BY id"):
            line = body + b"\n"
            if raw and (len(raw) + len(line) > MAX_CHUNK_BYTES or rows >= MAX_CHUNK_ROWS):
                name = f"{table}/{number:07d}.jsonl"
                self._write(stage, name, raw, files)
                raw, rows, number = bytearray(), 0, number + 1
            raw.extend(line)
            rows += 1
        if raw:
            self._write(stage, f"{table}/{number:07d}.jsonl", raw, files)

    def _write(self, stage, name, raw, files):
        files.total += len(raw) - files.get(name, {}).get("bytes", 0)
        files[name] = _integrity(raw)
        if len(files) > MAX_FILES or files.total > MAX_TOTAL_BYTES:
            raise MetadataError("Graph snapshot exceeds aggregate file or byte budget")
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def publish(self, entities, links, resources, report):
        """Atomically retain normalized graph rows and exact request evidence.

        Identical entities/edges are deduplicated. Conflicting descriptions for
        the same qualified entity fail explicitly; versions must stay distinct.
        Dangling references and directed cycles are retained and reported.
        """
        if not isinstance(report, dict):
            raise MetadataError("Graph completeness report must be an object")
        if len(canonical(report)) > MAX_REPORT_BYTES:
            raise MetadataError("Graph completeness report exceeds byte budget")
        self._directories()
        stage = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".pending-"))
        try:
            files = _Inventory()
            with _database(self.snapshots) as db:
                counts = {"entities": 0, "links": 0, "resources": 0}
                row_bytes = 0
                for resource in resources:
                    record = _resource_record(resource)
                    inserted = int(_insert(db, "resources", record))
                    counts["resources"] += inserted
                    row_bytes += inserted * (len(canonical(record)) + 1)
                    if counts["resources"] > MAX_RESOURCES:
                        raise MetadataError("Graph retained request count exceeds budget")
                    name = f"raw/{record['body_sha256']}.bin"
                    if name not in files:
                        self._write(stage, name, resource.body, files)
                    if files.total + row_bytes > MAX_TOTAL_BYTES:
                        raise MetadataError("Graph snapshot exceeds aggregate byte budget")
                parsed_cache = {}
                for table, values, validator, maximum in (
                    ("entities", entities, validate_entity, MAX_ENTITIES),
                    ("links", links, validate_link, MAX_LINKS),
                ):
                    for value in values:
                        value = validator(value)
                        _check_evidence(db, stage, value["provenance"], parsed_cache)
                        inserted = int(_insert(db, table, value))
                        counts[table] += inserted
                        row_bytes += inserted * (len(canonical(value)) + 1)
                        if counts[table] > maximum:
                            raise MetadataError(f"Graph {table} count exceeds budget")
                        if files.total + row_bytes > MAX_TOTAL_BYTES:
                            raise MetadataError("Graph snapshot exceeds aggregate byte budget")
                normalized_report = {**report, "graph_validation": _diagnostics(db)}
                raw = canonical(normalized_report) + b"\n"
                if len(raw) > MAX_REPORT_BYTES:
                    raise MetadataError("Graph completeness report exceeds byte budget")
                self._write(stage, "report.json", raw, files)
                for table in ("entities", "links", "resources"):
                    self._write_shards(stage, db, table, files)
                identity = {
                    "schema_version": 1,
                    "contract": GRAPH_CONTRACT,
                    "contract_version": GRAPH_CONTRACT_VERSION,
                    "kind": "metadata_graph",
                    "counts": _counts(db),
                    "files": files,
                }
                snapshot_id = hashlib.sha256(canonical(identity)).hexdigest()
                manifest = canonical({**identity, "snapshot_id": snapshot_id}) + b"\n"
                if len(manifest) > MAX_MANIFEST_BYTES:
                    raise MetadataError("Graph manifest exceeds byte budget")
                (stage / "manifest.json").write_bytes(manifest)
            return self._install(stage, snapshot_id)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def entities(self, snapshot_id, kind=None, limit=20, offset=0):
        _page(limit, offset)
        if kind is not None and (not isinstance(kind, str) or kind not in ENTITY_KINDS):
            raise MetadataError("Unsupported graph entity kind")
        manifest = self.verify(snapshot_id)
        total = (
            manifest["counts"]["entities"]
            if kind is None
            else manifest["counts"]["by_kind"].get(kind, 0)
        )
        items, index = [], 0
        for row in self._rows(self.path(snapshot_id), manifest, "entities"):
            if kind is None or row["kind"] == kind:
                if offset <= index < offset + limit:
                    items.append(row)
                index += 1
                if index >= offset + limit:
                    break
        return {
            "items": items,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def get_entity(self, snapshot_id, entity_id):
        _hash_id(entity_id)
        manifest = self.verify(snapshot_id)
        for row in self._rows(self.path(snapshot_id), manifest, "entities"):
            if row["id"] == entity_id:
                return row
            if row["id"] > entity_id:
                break
        raise MetadataError("Entity is not present in this graph snapshot")

    def links(self, snapshot_id, limit=20, offset=0):
        _page(limit, offset)
        manifest = self.verify(snapshot_id)
        items = []
        for index, row in enumerate(self._rows(self.path(snapshot_id), manifest, "links")):
            if offset <= index < offset + limit:
                items.append(row)
            if index >= offset + limit - 1:
                break
        total = manifest["counts"]["links"]
        return {
            "items": items,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def report(self, snapshot_id):
        self.verify(snapshot_id)
        return _parse((self.path(snapshot_id) / "report.json").read_bytes(), MAX_REPORT_BYTES)

    def list(self, limit=20, offset=0):
        _page(limit, offset)
        self._directories()
        ids = sorted(
            path.name
            for path in self.snapshots.iterdir()
            if re.fullmatch(SHA256_PATTERN, path.name)
        )
        items = [
            {"snapshot_id": item, "counts": self.verify(item)["counts"]}
            for item in ids[offset : offset + limit]
        ]
        return {
            "items": items,
            "total": len(ids),
            "next_offset": offset + limit if offset + limit < len(ids) else None,
        }

    def export(self, snapshot_id, output):
        manifest = self.verify(snapshot_id)
        source, output = self.path(snapshot_id), Path(output)
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output = output.parent.resolve() / output.name
        if output.is_relative_to(source):
            raise MetadataError("Cannot export into immutable graph snapshot")
        descriptor, name = tempfile.mkstemp(
            dir=output.parent, prefix=".graph-export-", suffix=".zip"
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for member in sorted({"manifest.json", *manifest["files"]}):
                    info = zipfile.ZipInfo(member, date_time=(2020, 1, 1, 0, 0, 0))
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    expected = (
                        _integrity(canonical(manifest) + b"\n")
                        if member == "manifest.json"
                        else manifest["files"][member]
                    )
                    info.compress_type = _compression_type(source / member, expected)
                    with (
                        (source / member).open("rb") as stream,
                        archive.open(info, "w", force_zip64=True) as destination,
                    ):
                        _copy(stream, destination, expected)
            os.link(temporary, output)
            return output
        finally:
            temporary.unlink(missing_ok=True)

    def import_bundle(self, input_path):
        self._directories()
        stage = None
        try:
            with zipfile.ZipFile(input_path) as archive:
                infos = archive.infolist()
                if not 2 <= len(infos) <= MAX_FILES + 1:
                    raise MetadataError("Graph archive member count exceeds budget")
                indexed = {}
                for info in infos:
                    safe_name(info.filename)
                    mode = info.external_attr >> 16
                    if (
                        info.filename in indexed
                        or info.is_dir()
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or info.file_size > MAX_MANIFEST_BYTES
                        or (
                            info.file_size > 1024 * 1024
                            and info.file_size > max(info.compress_size, 1) * MAX_EXPANSION_RATIO
                        )
                    ):
                        raise MetadataError("Unsafe or duplicate graph archive member")
                    indexed[info.filename] = info
                info = indexed.get("manifest.json")
                if info is None or info.file_size > MAX_MANIFEST_BYTES:
                    raise MetadataError("Graph archive lacks bounded manifest")
                with archive.open(info) as stream:
                    raw = stream.read(MAX_MANIFEST_BYTES + 1)
                value = _parse(raw, MAX_MANIFEST_BYTES)
                snapshot_id = value.get("snapshot_id") if isinstance(value, dict) else None
                manifest = _manifest(raw, snapshot_id)
                if set(indexed) != {"manifest.json", *manifest["files"]}:
                    raise MetadataError("Graph archive has missing or extra members")
                for name, expected in manifest["files"].items():
                    if indexed[name].file_size != expected["bytes"]:
                        raise MetadataError("Graph archive member size differs from manifest")
                stage = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".pending-"))
                (stage / "manifest.json").write_bytes(raw)
                for name, expected in manifest["files"].items():
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(indexed[name]) as source, target.open("wb") as destination:
                        _copy(source, destination, expected)
                return self._install(stage, snapshot_id)
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise MetadataError(f"Invalid graph archive: {exc}") from exc
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)

    def seed_cache(self, snapshot_id):
        """Recover exact request evidence, including scope and original clocks."""
        manifest = self.verify(snapshot_id)
        path = self.path(snapshot_id)
        for target in (self.root / "connector-cache", self.root / "connector-cache" / "objects"):
            if target.is_symlink():
                raise MetadataError("Graph cache recovery directories cannot be symlinks")
        cache = CachedClient(self.root, offline=True)
        report = {"seeded": 0, "existing": 0}
        # Latest source check wins when one exact request was retained repeatedly.
        with _database(self.snapshots) as db:
            db.execute("CREATE TEMP TABLE chosen(id TEXT PRIMARY KEY,clock REAL,body BLOB)")
            for record in self._rows(path, manifest, "resources"):
                identity = {
                    key: record[key] for key in ("method", "url", "request_body", "cache_scope")
                }
                key = hashlib.sha256(canonical(identity)).hexdigest()
                clock = record["checked_at"] or record["captured_at"]
                stamp = datetime.fromisoformat(clock).timestamp() if clock else float("-inf")
                raw = canonical(record)
                old = db.execute("SELECT clock,body FROM chosen WHERE id=?", (key,)).fetchone()
                if old is None or (stamp, raw) > old:
                    db.execute("INSERT OR REPLACE INTO chosen VALUES(?,?,?)", (key, stamp, raw))
            for key, _, raw in db.execute("SELECT id,clock,body FROM chosen ORDER BY id"):
                record = _parse(raw)
                identity = {
                    item: record[item] for item in ("method", "url", "request_body", "cache_scope")
                }
                target = cache.root / f"{key}.json"
                lock_path = cache.root / f"{key}.lock"
                if target.is_symlink() or lock_path.is_symlink():
                    raise MetadataError("Graph cache recovery rejects symlink capture files")
                with lock_path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if target.exists():
                        if target.stat().st_size > MAX_ROW_BYTES + 1024:
                            raise MetadataError("Existing response capture exceeds byte budget")
                        existing = _resource_record(cache._read_capture(target, identity))
                        prior_clock = existing["checked_at"] or existing["captured_at"]
                        next_clock = record["checked_at"] or record["captured_at"]
                        before = (
                            datetime.fromisoformat(prior_clock).timestamp()
                            if prior_clock
                            else float("-inf")
                        )
                        after = (
                            datetime.fromisoformat(next_clock).timestamp()
                            if next_clock
                            else float("-inf")
                        )
                        if (before, canonical(existing)) >= (after, canonical(record)):
                            report["existing"] += 1
                            continue
                    document = {
                        "request": identity,
                        **{
                            item: record[item]
                            for item in (
                                "url",
                                "body_sha256",
                                "content_type",
                                "headers",
                                "captured_at",
                                "checked_at",
                            )
                        },
                    }
                    document["capture_id"] = hashlib.sha256(canonical(document)).hexdigest()
                    _atomic(
                        cache.root / "objects" / f"{record['body_sha256']}.bin",
                        (path / "raw" / f"{record['body_sha256']}.bin").read_bytes(),
                    )
                    _atomic(target, canonical(document))
                    report["seeded"] += 1
        return report

    def seed_all_cache(self):
        result = {"seeded": 0, "existing": 0, "snapshots": 0}
        ids = sorted(
            path.name
            for path in self.snapshots.iterdir()
            if re.fullmatch(SHA256_PATTERN, path.name)
        )
        # Verify every snapshot before any response cache is changed.
        for snapshot_id in ids:
            self.verify(snapshot_id)
        for snapshot_id in ids:
            report = self.seed_cache(snapshot_id)
            result["seeded"] += report["seeded"]
            result["existing"] += report["existing"]
            result["snapshots"] += 1
        return result


def _copy(source, destination, expected):
    digest, length = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        length += len(chunk)
        if length > expected["bytes"]:
            raise MetadataError("Graph member exceeds declared byte budget")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if length != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise MetadataError("Graph member hash or size mismatch")


def _compression_type(path, expected):
    """Avoid emitting legitimate bundles that our expansion guard would reject."""
    if expected["bytes"] <= 1024 * 1024:
        return zipfile.ZIP_DEFLATED
    compressor = zlib.compressobj(wbits=-15)
    compressed, length = 0, 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            length += len(chunk)
            if length > expected["bytes"]:
                raise MetadataError("Graph member changed during archive compression")
            compressed += len(compressor.compress(chunk))
    compressed += len(compressor.flush())
    return (
        zipfile.ZIP_STORED
        if expected["bytes"] > max(compressed, 1) * MAX_EXPANSION_RATIO
        else zipfile.ZIP_DEFLATED
    )
