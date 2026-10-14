"""Immutable, portable literature metadata and explicitly attributed dataset links.

This store performs no acquisition and makes no claim that source review is an
independent human review. Its JSON index is the verified snapshot itself.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
# Full bounded literature harvests retain readable citations on every row.
# Keep the document ceiling explicit while allowing the expanded UK capture.
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RESOURCES = 2_000
MAX_ROWS = 100_000
SHA256 = re.compile(r"[0-9a-f]{64}")
RELATIONSHIP_TYPES = {"verified_use", "source_asserted_use", "citation", "inferred_mention"}
REVIEW_STATUSES = {"pending", "source_reviewed", "human_reviewed"}


class LiteratureError(ValueError):
    """Malformed, incomplete or corrupted literature evidence."""


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _record(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _text(value, label, *, nullable=False, empty=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise LiteratureError(f"{label} must be {'a string or null' if nullable else 'a string'}")


def _safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise LiteratureError("Unsafe resource/member path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in {".", ".."} or ":" in part for part in name.split("/"))
    ):
        raise LiteratureError("Unsafe resource/member path")
    return name


def _resource_names(names):
    paths = {_safe_name(name) for name in names}
    for name in paths:
        if any(parent.as_posix() in paths for parent in PurePosixPath(name).parents):
            raise LiteratureError("Resource file paths conflict with directories")
    return paths


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LiteratureError("Duplicate JSON field")
        result[key] = value
    return result


def _json(raw):
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise LiteratureError("JSON document exceeds size budget")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(value, dict):
            raise LiteratureError("Expected JSON object")
        _canonical(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise LiteratureError(f"Invalid JSON document: {exc}") from exc


def _rows(value, label):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise LiteratureError(f"Invalid {label} array")
    result = {}
    for row in value:
        if not isinstance(row, dict):
            raise LiteratureError(f"Invalid {label} record")
        _text(row.get("id"), f"{label} id")
        if row["id"] in result:
            raise LiteratureError(f"Duplicate {label} id: {row['id']}")
        result[row["id"]] = row
    return result


def _references(row, sources, label):
    refs = row.get("references")
    if not isinstance(refs, list) or not refs:
        raise LiteratureError(f"{label} must retain readable references")
    seen = set()
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or not isinstance(ref.get("id"), str)
            or ref["id"] not in sources
        ):
            raise LiteratureError(f"Unknown {label} reference")
        if ref["id"] in seen:
            raise LiteratureError(f"Duplicate {label} reference")
        seen.add(ref["id"])
        source = sources[ref["id"]]
        for key in ("title", "url"):
            if ref.get(key) != source[key]:
                raise LiteratureError(f"{label} reference {key} disagrees with source")
        for key in ("type", "published_at"):
            if key in ref and key in source and ref[key] != source[key]:
                raise LiteratureError(f"{label} reference {key} disagrees with source")
    row["references"] = sorted(refs, key=lambda ref: ref["id"])


def _source_ids(row, sources, label):
    ids = row.get("source_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, str) or item not in sources for item in ids)
        or len(ids) != len(set(ids))
    ):
        raise LiteratureError(f"Invalid {label} source_ids")
    row["source_ids"] = sorted(ids)


def validate_document(document, resource_names=None):
    """Return a canonical-order copy without dropping fields or filling unknown facts."""
    try:
        raw = _canonical(document)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LiteratureError("Document is not finite JSON") from exc
    value = _json(raw)
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise LiteratureError("Unsupported literature document schema_version")
    sources = _rows(value.get("sources"), "sources")
    papers = _rows(value.get("papers"), "papers")
    datasets = _rows(value.get("datasets"), "datasets")
    relationships = _rows(value.get("relationships"), "relationships")
    if not sources:
        raise LiteratureError("Literature document requires sources")
    names = _resource_names(resource_names) if resource_names is not None else None
    for source in sources.values():
        _text(source.get("title"), "source title")
        _text(source.get("url"), "source url")
        for key in ("licence", "version"):
            if key in source:
                _text(source[key], f"source {key}", nullable=True)
        if "resource_paths" in source:
            paths = source["resource_paths"]
            if not isinstance(paths, list):
                raise LiteratureError("Invalid source resource_paths")
            _resource_names(paths)
            if len(paths) != len(set(paths)):
                raise LiteratureError("Duplicate source resource_paths")
            for path in paths:
                _safe_name(path)
                if names is not None and path not in names:
                    raise LiteratureError(f"Missing source raw resource: {path}")
            source["resource_paths"] = sorted(paths)
    for paper in papers.values():
        for key in ("title", "url"):
            _text(paper.get(key), f"paper {key}")
        for key in ("doi", "abstract", "published_at"):
            if key not in paper:
                raise LiteratureError(f"paper must explicitly declare {key}, including null")
            _text(paper[key], f"paper {key}", nullable=True)
        _source_ids(paper, sources, "paper")
        _references(paper, sources, "paper")
    for dataset in datasets.values():
        for key in ("title", "provider"):
            _text(dataset.get(key), f"dataset {key}")
        _text(dataset.get("description"), "dataset description", empty=True)
        for key in ("doi", "version", "native_id"):
            if key not in dataset:
                raise LiteratureError(f"dataset must explicitly declare {key}, including null")
            _text(dataset[key], f"dataset {key}", nullable=True)
        if not isinstance(dataset.get("coverage"), dict):
            raise LiteratureError("Dataset coverage must be an object")
        _source_ids(dataset, sources, "dataset")
        _references(dataset, sources, "dataset")
    for relationship in relationships.values():
        if (
            not isinstance(relationship.get("paper_id"), str)
            or relationship["paper_id"] not in papers
            or not isinstance(relationship.get("dataset_id"), str)
            or relationship["dataset_id"] not in datasets
        ):
            raise LiteratureError("Relationship refers to an unknown paper or dataset")
        if (
            not isinstance(relationship.get("relationship_type"), str)
            or relationship["relationship_type"] not in RELATIONSHIP_TYPES
        ):
            raise LiteratureError("Invalid relationship_type")
        _text(relationship.get("asserted_by"), "relationship asserted_by")
        if "dataset_version" not in relationship:
            raise LiteratureError("Relationship must explicitly declare dataset_version")
        _text(relationship["dataset_version"], "relationship dataset_version", nullable=True)
        evidence = relationship.get("evidence")
        if (
            not isinstance(evidence, dict)
            or not isinstance(evidence.get("source_id"), str)
            or evidence["source_id"] not in sources
        ):
            raise LiteratureError("Relationship evidence must name a known source")
        for key in ("location", "summary", "url"):
            _text(evidence.get(key), f"relationship evidence {key}")
        review = relationship.get("review")
        if (
            not isinstance(review, dict)
            or not isinstance(review.get("status"), str)
            or review["status"] not in REVIEW_STATUSES
        ):
            raise LiteratureError("Invalid relationship review status")
        if "reviewer" not in review:
            raise LiteratureError("Relationship must explicitly declare reviewer")
        _text(review["reviewer"], "relationship reviewer", nullable=review["status"] == "pending")
        if review["status"] == "human_reviewed" and review.get("reviewer_type") != "human":
            raise LiteratureError("human_reviewed requires an explicit human reviewer_type")
        if review["status"] == "human_reviewed" and review.get("method") in ("agent", "generated"):
            raise LiteratureError("Agent/generated review cannot claim human_reviewed")
        _references(relationship, sources, "relationship")
        if evidence["source_id"] not in {ref["id"] for ref in relationship["references"]}:
            raise LiteratureError("Relationship references must retain its evidence source")
    for key, rows in (
        ("sources", sources),
        ("papers", papers),
        ("datasets", datasets),
        ("relationships", relationships),
    ):
        value[key] = sorted(rows.values(), key=lambda row: row["id"])
    if len(_canonical(value)) + 1 > MAX_DOCUMENT_BYTES:
        raise LiteratureError("Normalized document exceeds size budget")
    return value


def _manifest(raw, snapshot_id):
    manifest = _json(raw)
    if manifest.get("snapshot_id") != snapshot_id or not SHA256.fullmatch(snapshot_id):
        raise LiteratureError("Manifest snapshot identity mismatch")
    identity = {key: value for key, value in manifest.items() if key != "snapshot_id"}
    if hashlib.sha256(_canonical(identity)).hexdigest() != snapshot_id:
        raise LiteratureError("Manifest hash mismatch")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest.get("kind") != "literature"
        or manifest.get("document") != "document.json"
    ):
        raise LiteratureError("Unsupported literature manifest schema")
    files = manifest.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_RESOURCES + 1:
        raise LiteratureError("Invalid manifest file closure")
    if "document.json" not in files:
        raise LiteratureError("Manifest lacks document.json")
    total = 0
    for name, record in files.items():
        _safe_name(name)
        if name != "document.json" and not name.startswith("resources/"):
            raise LiteratureError("Unexpected manifest file path")
        budget = MAX_DOCUMENT_BYTES if name == "document.json" else MAX_RESOURCE_BYTES
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "bytes"}
            or not isinstance(record["sha256"], str)
            or not SHA256.fullmatch(record["sha256"])
            or type(record["bytes"]) is not int
            or not 0 <= record["bytes"] <= budget
        ):
            raise LiteratureError("Invalid file integrity record")
        total += record["bytes"]
    _resource_names(name.removeprefix("resources/") for name in files if name != "document.json")
    if total > MAX_TOTAL_BYTES:
        raise LiteratureError("Snapshot exceeds total size budget")
    counts = manifest.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"sources", "papers", "datasets", "relationships"}
        or any(type(count) is not int or not 0 <= count <= MAX_ROWS for count in counts.values())
    ):
        raise LiteratureError("Invalid manifest counts")
    return manifest


def _copy_verified(source, destination, expected):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        if size > expected["bytes"]:
            raise LiteratureError("File exceeds declared size")
        digest.update(chunk)
        destination.write(chunk)
    if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise LiteratureError("File size or hash mismatch")


class LiteratureStore:
    """A content-addressed evidence namespace within an SSDU store root."""

    def __init__(self, root="data"):
        self.root = Path(root).resolve()
        self.directory = self.root / "literature"
        self.snapshots = self.directory / "snapshots"
        for path in (self.directory, self.snapshots):
            if path.is_symlink():
                raise LiteratureError("Literature store directories cannot be symlinks")
            path.mkdir(parents=True, exist_ok=True)

    def path(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not SHA256.fullmatch(snapshot_id):
            raise LiteratureError("Literature snapshot IDs must be full lowercase SHA-256 hashes")
        path = self.snapshots / snapshot_id
        if path.is_symlink() or not path.is_dir():
            raise LiteratureError(f"Literature snapshot missing or unsafe: {snapshot_id}")
        return path

    def _verified(self, snapshot_id, path=None):
        path = self.path(snapshot_id) if path is None else path
        try:
            manifest_path = path / "manifest.json"
            if manifest_path.is_symlink() or manifest_path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise LiteratureError("Unsafe or oversized manifest")
            manifest = _manifest(manifest_path.read_bytes(), snapshot_id)
            expected = {"manifest.json", *manifest["files"]}
            actual = set()
            for parent, directories, names in os.walk(path, followlinks=False):
                for name in directories:
                    if (Path(parent) / name).is_symlink():
                        raise LiteratureError("Snapshot contains a symlink directory")
                for name in names:
                    target = Path(parent) / name
                    if target.is_symlink() or not target.is_file():
                        raise LiteratureError("Snapshot contains unsafe file")
                    actual.add(target.relative_to(path).as_posix())
            if actual != expected:
                raise LiteratureError("Snapshot contains missing or extra members")
            document_raw = None
            for name, integrity in manifest["files"].items():
                target = path / name
                if target.stat().st_size != integrity["bytes"]:
                    raise LiteratureError(f"File size mismatch: {name}")
                digest = hashlib.sha256()
                count = 0
                raw = bytearray() if name == "document.json" else None
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        count += len(chunk)
                        if count > integrity["bytes"]:
                            raise LiteratureError(f"File exceeds declared size: {name}")
                        digest.update(chunk)
                        if raw is not None:
                            raw.extend(chunk)
                            if len(raw) > MAX_DOCUMENT_BYTES:
                                raise LiteratureError("Document exceeds size budget")
                if count != integrity["bytes"] or digest.hexdigest() != integrity["sha256"]:
                    raise LiteratureError(f"File hash mismatch: {name}")
                if raw is not None:
                    document_raw = bytes(raw)
            resource_names = [
                name[len("resources/") :]
                for name in manifest["files"]
                if name.startswith("resources/")
            ]
            document = validate_document(_json(document_raw), resource_names)
            if document_raw != _canonical(document) + b"\n":
                raise LiteratureError("Snapshot document is not canonical")
            if manifest["counts"] != {key: len(document[key]) for key in manifest["counts"]}:
                raise LiteratureError("Snapshot counts disagree with document")
            return manifest, document
        except (OSError, TypeError, KeyError, RecursionError) as exc:
            raise LiteratureError(f"Invalid literature snapshot: {exc}") from exc

    def verify(self, snapshot_id):
        return self._verified(snapshot_id)[0]

    def read(self, snapshot_id):
        return self._verified(snapshot_id)[1]

    def _publish_stage(self, stage, snapshot_id):
        self._verified(snapshot_id, stage)
        lock_path = self.directory / ".publication.lock"
        if lock_path.is_symlink():
            raise LiteratureError("Unsafe publication lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            target = self.snapshots / snapshot_id
            if target.exists() or target.is_symlink():
                self.verify(snapshot_id)
                return snapshot_id
            os.rename(stage, target)
            return snapshot_id

    def publish(self, document, resources):
        if not isinstance(resources, dict) or len(resources) > MAX_RESOURCES:
            raise LiteratureError("Invalid raw resource mapping")
        _resource_names(resources)
        for name, raw in resources.items():
            _safe_name(name)
            if not isinstance(raw, bytes) or len(raw) > MAX_RESOURCE_BYTES:
                raise LiteratureError("Raw resources must be bounded bytes")
        normalized = validate_document(document, resources)
        raw_document = _canonical(normalized) + b"\n"
        files = {"document.json": raw_document}
        files.update({f"resources/{name}": raw for name, raw in resources.items()})
        if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
            raise LiteratureError("Snapshot exceeds total size budget")
        identity = {
            "schema_version": SCHEMA_VERSION,
            "kind": "literature",
            "document": "document.json",
            "counts": {
                key: len(normalized[key])
                for key in ("sources", "papers", "datasets", "relationships")
            },
            "files": {name: _record(raw) for name, raw in sorted(files.items())},
        }
        snapshot_id = hashlib.sha256(_canonical(identity)).hexdigest()
        manifest = {**identity, "snapshot_id": snapshot_id}
        stage = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".pending-"))
        try:
            for name, raw in files.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            (stage / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
            return self._publish_stage(stage, snapshot_id)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def list(self):
        summaries = []
        for path in sorted(self.snapshots.iterdir()):
            if SHA256.fullmatch(path.name):
                manifest = self.verify(path.name)
                summaries.append({"snapshot_id": path.name, "counts": manifest["counts"]})
        return summaries

    def search(
        self, snapshot_id, query="", dataset_id=None, relationship_type=None, limit=20, offset=0
    ):
        _text(query, "query", empty=True)
        if len(query) > 2_000:
            raise LiteratureError("Query exceeds size budget")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise LiteratureError("limit must be 1–100 and offset nonnegative")
        if relationship_type is not None and (
            not isinstance(relationship_type, str) or relationship_type not in RELATIONSHIP_TYPES
        ):
            raise LiteratureError("Invalid relationship_type filter")
        document = self.read(snapshot_id)
        datasets = {row["id"]: row for row in document["datasets"]}
        if dataset_id is not None and (
            not isinstance(dataset_id, str) or dataset_id not in datasets
        ):
            raise LiteratureError("Unknown dataset_id filter")
        by_paper = {}
        for relationship in document["relationships"]:
            if dataset_id is not None and relationship["dataset_id"] != dataset_id:
                continue
            if (
                relationship_type is not None
                and relationship["relationship_type"] != relationship_type
            ):
                continue
            by_paper.setdefault(relationship["paper_id"], []).append(
                {"relationship": relationship, "dataset": datasets[relationship["dataset_id"]]}
            )
        terms = query.casefold().split()
        rows = []
        for paper in document["papers"]:
            links = by_paper.get(paper["id"], [])
            if (dataset_id is not None or relationship_type is not None) and not links:
                continue
            searchable = _canonical({"paper": paper, "links": links}).decode().casefold()
            if any(term not in searchable for term in terms):
                continue
            rows.append({"paper": paper, "relationships": links, "references": paper["references"]})
        return {
            "snapshot_id": snapshot_id,
            "query": query,
            "filters": {"dataset_id": dataset_id, "relationship_type": relationship_type},
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "rows": rows[offset : offset + limit],
        }

    def export(self, snapshot_id, output):
        manifest = self.verify(snapshot_id)
        source = self.path(snapshot_id)
        output = Path(output)
        if output.exists() or output.is_symlink():
            raise FileExistsError(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output = output.parent.resolve() / output.name
        if output.is_relative_to(source):
            raise LiteratureError("Cannot export into an immutable snapshot")
        fd, name = tempfile.mkstemp(dir=output.parent, prefix=".literature-export-", suffix=".zip")
        os.close(fd)
        temporary = Path(name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for member in sorted({"manifest.json", *manifest["files"]}):
                    target = source / member
                    budget = (
                        MAX_DOCUMENT_BYTES
                        if member == "manifest.json"
                        else manifest["files"][member]["bytes"]
                    )
                    with target.open("rb") as stream:
                        raw = stream.read(budget + 1)
                    if member != "manifest.json" and _record(raw) != manifest["files"][member]:
                        raise LiteratureError("Snapshot changed during export")
                    if member == "manifest.json" and _manifest(raw, snapshot_id) != manifest:
                        raise LiteratureError("Manifest changed during export")
                    info = zipfile.ZipInfo(member, date_time=(2020, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(info, raw)
            os.link(temporary, output)  # Atomic publication without an overwrite race.
            return {"snapshot_id": snapshot_id, "path": str(output), **_record(output.read_bytes())}
        finally:
            temporary.unlink(missing_ok=True)

    def import_bundle(self, input_path):
        stage = None
        try:
            with zipfile.ZipFile(input_path) as archive:
                infos = archive.infolist()
                if not 2 <= len(infos) <= MAX_RESOURCES + 2:
                    raise LiteratureError("Archive member count exceeds budget")
                indexed = {}
                for info in infos:
                    _safe_name(info.filename)
                    mode = info.external_attr >> 16
                    if (
                        info.filename in indexed
                        or info.is_dir()
                        or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    ):
                        raise LiteratureError("Unsafe or duplicate archive member")
                    indexed[info.filename] = info
                info = indexed.get("manifest.json")
                if info is None or info.file_size > MAX_DOCUMENT_BYTES:
                    raise LiteratureError("Archive lacks a bounded manifest")
                with archive.open(info) as stream:
                    raw = stream.read(MAX_DOCUMENT_BYTES + 1)
                preview = _json(raw)
                snapshot_id = preview.get("snapshot_id")
                if not isinstance(snapshot_id, str):
                    raise LiteratureError("Archive manifest lacks snapshot_id")
                manifest = _manifest(raw, snapshot_id)
                if set(indexed) != {"manifest.json", *manifest["files"]}:
                    raise LiteratureError("Archive contains missing or extra members")
                for member, integrity in manifest["files"].items():
                    if indexed[member].file_size != integrity["bytes"]:
                        raise LiteratureError("Archive member size differs from manifest")
                stage = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".pending-"))
                (stage / "manifest.json").write_bytes(raw)
                for member, integrity in manifest["files"].items():
                    target = stage / member
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(indexed[member]) as source, target.open("wb") as destination:
                        _copy_verified(source, destination, integrity)
                return self._publish_stage(stage, snapshot_id)
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise LiteratureError(f"Invalid literature archive: {exc}") from exc
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)
