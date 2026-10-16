"""Append-only, verified, portable metadata snapshots without observation acquisition."""

import fcntl
import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from .metadata_contract import (
    MAX_DOCUMENT_BYTES,
    MAX_PROVIDERS,
    MAX_RECORDS,
    SHA256,
    SUPPORTED_CONTRACT_VERSIONS,
    MetadataError,
    canonical,
    parse_json,
    resource_names,
    safe_name,
    validate_document,
)

MAX_RESOURCE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RESOURCES = 20_000
MANIFEST_VERSION = 1


def _integrity(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _manifest(raw, snapshot_id):
    value = parse_json(raw)
    if not isinstance(snapshot_id, str) or not SHA256.fullmatch(snapshot_id):
        raise MetadataError("Metadata snapshot IDs must be full lowercase SHA-256 hashes")
    if set(value) != {
        "schema_version",
        "contract_version",
        "kind",
        "document",
        "counts",
        "files",
        "snapshot_id",
    }:
        raise MetadataError("Unsupported metadata manifest fields")
    identity = {key: item for key, item in value.items() if key != "snapshot_id"}
    if (
        value["snapshot_id"] != snapshot_id
        or hashlib.sha256(canonical(identity)).hexdigest() != snapshot_id
    ):
        raise MetadataError("Metadata manifest identity mismatch")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != MANIFEST_VERSION
        or value["contract_version"] not in SUPPORTED_CONTRACT_VERSIONS
        or value["kind"] != "provider_metadata"
        or value["document"] != "document.json"
    ):
        raise MetadataError("Unsupported metadata manifest schema")
    files = value["files"]
    if (
        not isinstance(files, dict)
        or not 1 <= len(files) <= MAX_RESOURCES + 1
        or "document.json" not in files
    ):
        raise MetadataError("Invalid metadata manifest inventory")
    total = 0
    for name, record in files.items():
        safe_name(name)
        if name != "document.json" and not name.startswith("resources/"):
            raise MetadataError("Unexpected metadata manifest file path")
        budget = MAX_DOCUMENT_BYTES if name == "document.json" else MAX_RESOURCE_BYTES
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "bytes"}
            or not isinstance(record["sha256"], str)
            or not SHA256.fullmatch(record["sha256"])
            or type(record["bytes"]) is not int
            or not 0 <= record["bytes"] <= budget
        ):
            raise MetadataError("Invalid metadata file integrity record")
        total += record["bytes"]
    resource_names(name[len("resources/") :] for name in files if name != "document.json")
    if total > MAX_TOTAL_BYTES:
        raise MetadataError("Metadata snapshot exceeds total size budget")
    counts = value["counts"]
    if (
        not isinstance(counts, dict)
        or set(counts) != {"providers", "records"}
        or type(counts["providers"]) is not int
        or not 1 <= counts["providers"] <= MAX_PROVIDERS
        or type(counts["records"]) is not int
        or not 0 <= counts["records"] <= MAX_RECORDS
    ):
        raise MetadataError("Invalid metadata manifest counts")
    return value


def _copy(source, destination, expected):
    digest, length = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        length += len(chunk)
        if length > expected["bytes"]:
            raise MetadataError("Metadata resource exceeds declared size")
        digest.update(chunk)
        destination.write(chunk)
    if length != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise MetadataError("Metadata resource hash or size mismatch")


class MetadataStore:
    """Content-addressed metadata namespace, independent of mutable search indexes.

    Limits: 100,000 records, 128 MiB normalized document, 64 MiB per raw
    response, 20,000 responses and 512 MiB total uncompressed snapshot content.
    Exported ZIP members have deterministic ordering, timestamps and permissions.
    """

    def __init__(self, root="data"):
        self.root = Path(root).resolve()
        self.directory = self.root / "metadata"
        self.snapshots = self.directory / "snapshots"
        self._directories()

    def _directories(self):
        for path in (self.directory, self.snapshots):
            if path.is_symlink():
                raise MetadataError("Metadata store directories cannot be symlinks")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise MetadataError("Metadata store path is not a directory")

    def path(self, snapshot_id):
        self._directories()
        if not isinstance(snapshot_id, str) or not SHA256.fullmatch(snapshot_id):
            raise MetadataError("Metadata snapshot IDs must be full lowercase SHA-256 hashes")
        path = self.snapshots / snapshot_id
        if path.is_symlink() or not path.is_dir():
            raise MetadataError(f"Metadata snapshot missing or unsafe: {snapshot_id}")
        return path

    def _verified(self, snapshot_id, path=None):
        path = self.path(snapshot_id) if path is None else path
        try:
            manifest_path = path / "manifest.json"
            if (
                manifest_path.is_symlink()
                or not manifest_path.is_file()
                or manifest_path.stat().st_size > MAX_DOCUMENT_BYTES
            ):
                raise MetadataError("Unsafe or oversized metadata manifest")
            manifest = _manifest(manifest_path.read_bytes(), snapshot_id)
            expected = {"manifest.json", *manifest["files"]}
            actual = set()
            for parent, directories, names in os.walk(path, followlinks=False):
                for name in directories:
                    if (Path(parent) / name).is_symlink():
                        raise MetadataError("Metadata snapshot contains a symlink directory")
                for name in names:
                    target = Path(parent) / name
                    if target.is_symlink() or not target.is_file():
                        raise MetadataError("Metadata snapshot contains an unsafe file")
                    actual.add(target.relative_to(path).as_posix())
            if actual != expected:
                raise MetadataError("Metadata snapshot contains missing or extra members")
            document_raw = None
            for name, integrity in manifest["files"].items():
                target = path / name
                if target.stat().st_size != integrity["bytes"]:
                    raise MetadataError(f"Metadata file size mismatch: {name}")
                digest, length = hashlib.sha256(), 0
                raw = bytearray() if name == "document.json" else None
                with target.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        length += len(chunk)
                        if length > integrity["bytes"]:
                            raise MetadataError(f"Metadata file exceeds declared size: {name}")
                        digest.update(chunk)
                        if raw is not None:
                            raw.extend(chunk)
                if length != integrity["bytes"] or digest.hexdigest() != integrity["sha256"]:
                    raise MetadataError(f"Metadata file hash mismatch: {name}")
                if raw is not None:
                    document_raw = bytes(raw)
            names = [
                name[len("resources/") :]
                for name in manifest["files"]
                if name.startswith("resources/")
            ]
            document = validate_document(parse_json(document_raw), names)
            if manifest["contract_version"] != document["contract_version"]:
                raise MetadataError("Metadata manifest and document contract versions disagree")
            if document_raw != canonical(document) + b"\n":
                raise MetadataError("Metadata document is not canonical")
            if manifest["counts"] != {key: len(document[key]) for key in ("providers", "records")}:
                raise MetadataError("Metadata manifest counts disagree with document")
            return manifest, document
        except (OSError, TypeError, KeyError, RecursionError) as exc:
            raise MetadataError(f"Invalid metadata snapshot: {exc}") from exc

    def verify(self, snapshot_id):
        return self._verified(snapshot_id)[0]

    def read(self, snapshot_id):
        return self._verified(snapshot_id)[1]

    def _publish_stage(self, stage, snapshot_id):
        self._directories()
        self._verified(snapshot_id, stage)
        lock_path = self.directory / ".publication.lock"
        if lock_path.is_symlink():
            raise MetadataError("Unsafe metadata publication lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            destination = self.snapshots / snapshot_id
            if destination.exists() or destination.is_symlink():
                self.verify(snapshot_id)
                return snapshot_id
            os.rename(stage, destination)
            return snapshot_id

    def publish(self, document, resources=None):
        resources = {} if resources is None else resources
        if not isinstance(resources, dict) or len(resources) > MAX_RESOURCES:
            raise MetadataError("Invalid metadata raw resource mapping")
        resource_names(resources)
        if any(
            not isinstance(raw, bytes) or len(raw) > MAX_RESOURCE_BYTES
            for raw in resources.values()
        ):
            raise MetadataError("Metadata raw resources must be bounded bytes")
        normalized = validate_document(document, resources)
        files = {"document.json": canonical(normalized) + b"\n"}
        files.update({f"resources/{name}": raw for name, raw in resources.items()})
        if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
            raise MetadataError("Metadata snapshot exceeds total size budget")
        identity = {
            "schema_version": MANIFEST_VERSION,
            "contract_version": normalized["contract_version"],
            "kind": "provider_metadata",
            "document": "document.json",
            "counts": {key: len(normalized[key]) for key in ("providers", "records")},
            "files": {name: _integrity(raw) for name, raw in sorted(files.items())},
        }
        snapshot_id = hashlib.sha256(canonical(identity)).hexdigest()
        manifest = {**identity, "snapshot_id": snapshot_id}
        self._directories()
        stage = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".pending-"))
        try:
            for name, raw in {**files, "manifest.json": canonical(manifest) + b"\n"}.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            return self._publish_stage(stage, snapshot_id)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def list(self, limit=20, offset=0):
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= MAX_RECORDS
        ):
            raise MetadataError("Use metadata limit=1..100 and offset=0..100000")
        self._directories()
        ids = sorted(path.name for path in self.snapshots.iterdir() if SHA256.fullmatch(path.name))
        rows = []
        for snapshot_id in ids[offset : offset + limit]:
            manifest, document = self._verified(snapshot_id)
            rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "counts": manifest["counts"],
                    "captured_at": document["captured_at"],
                }
            )
        return {
            "rows": rows,
            "total": len(ids),
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(ids) else None,
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
            raise MetadataError("Cannot export into an immutable metadata snapshot")
        fd, name = tempfile.mkstemp(dir=output.parent, prefix=".metadata-export-", suffix=".zip")
        os.close(fd)
        temporary = Path(name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for member in sorted({"manifest.json", *manifest["files"]}):
                    budget = (
                        MAX_DOCUMENT_BYTES
                        if member == "manifest.json"
                        else manifest["files"][member]["bytes"]
                    )
                    with (source / member).open("rb") as stream:
                        raw = stream.read(budget + 1)
                    if member == "manifest.json":
                        if _manifest(raw, snapshot_id) != manifest:
                            raise MetadataError("Metadata manifest changed during export")
                    elif _integrity(raw) != manifest["files"][member]:
                        raise MetadataError("Metadata snapshot changed during export")
                    info = zipfile.ZipInfo(member, date_time=(2020, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(info, raw)
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
                if not 2 <= len(infos) <= MAX_RESOURCES + 2:
                    raise MetadataError("Metadata archive member count exceeds budget")
                indexed = {}
                for info in infos:
                    safe_name(info.filename)
                    mode = info.external_attr >> 16
                    if (
                        info.filename in indexed
                        or info.is_dir()
                        or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                        or info.flag_bits & 1
                        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    ):
                        raise MetadataError("Unsafe or duplicate metadata archive member")
                    indexed[info.filename] = info
                info = indexed.get("manifest.json")
                if info is None or info.file_size > MAX_DOCUMENT_BYTES:
                    raise MetadataError("Metadata archive lacks a bounded manifest")
                with archive.open(info) as stream:
                    raw = stream.read(MAX_DOCUMENT_BYTES + 1)
                snapshot_id = parse_json(raw).get("snapshot_id")
                manifest = _manifest(raw, snapshot_id)
                if set(indexed) != {"manifest.json", *manifest["files"]}:
                    raise MetadataError("Metadata archive contains missing or extra members")
                for member, record in manifest["files"].items():
                    if indexed[member].file_size != record["bytes"]:
                        raise MetadataError("Metadata archive member size differs from manifest")
                stage = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".pending-"))
                (stage / "manifest.json").write_bytes(raw)
                for member, record in manifest["files"].items():
                    target = stage / member
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(indexed[member]) as source, target.open("wb") as destination:
                        _copy(source, destination, record)
                return self._publish_stage(stage, snapshot_id)
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise MetadataError(f"Invalid metadata archive: {exc}") from exc
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)
