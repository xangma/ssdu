"""Verified, bounded ZIP transport for immutable collections and their snapshot lineage."""

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

from pyarrow import parquet

from ssdu.collection_models import CollectionLock
from ssdu.models import RetrievalError
from ssdu.store import Store, canonical

MAX_SNAPSHOTS = 128
MAX_DEPTH = 8
MAX_MEMBERS = 10_000
MAX_BYTES = 500 * 1024 * 1024
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
LOCK_MEMBER = "collection.lock.json"
BUNDLE_MEMBER = "bundle.json"


def _error(message):
    return RetrievalError(f"Invalid collection bundle: {message}")


def _safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise _error("unsafe member path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in {".", ".."} or ":" in part for part in name.split("/"))
    ):
        raise _error("unsafe member path")
    return path


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _error("duplicate JSON field")
        result[key] = value
    return result


def _document(raw):
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise _error("JSON document exceeds its size budget")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(value, dict):
            raise _error("expected a JSON object")
        canonical(value)  # Reject non-finite numbers accepted by Python's JSON decoder.
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise _error("malformed JSON document") from exc


def _lock(value):
    if isinstance(value, CollectionLock):
        value = value.model_dump()
    return CollectionLock.model_validate(value)


def _record(record):
    if (
        not isinstance(record, dict)
        or set(record) != {"sha256", "bytes"}
        or not isinstance(record["sha256"], str)
        or not SHA256.fullmatch(record["sha256"])
        or type(record["bytes"]) is not int
        or not 0 <= record["bytes"] <= MAX_BYTES
    ):
        raise _error("invalid member integrity record")


def _manifest(raw, snapshot_id):
    manifest = _document(raw)
    if manifest.get("product_id") != snapshot_id:
        raise _error("snapshot manifest identity mismatch")
    identity = {k: v for k, v in manifest.items() if k != "product_id"}
    if hashlib.sha256(canonical(identity)).hexdigest() != snapshot_id:
        raise _error("snapshot manifest hash mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or not {"data.parquet", "metadata.json"}.issubset(files):
        raise _error("snapshot lacks its data or metadata file")
    for name, record in files.items():
        _safe_path(name)
        if name == "manifest.json":
            raise _error("manifest cannot list itself as a data file")
        _record(record)
    parents = manifest.get("parent_snapshot_ids", [])
    if (
        not isinstance(parents, list)
        or any(not isinstance(p, str) or not SHA256.fullmatch(p) for p in parents)
        or len(set(parents)) != len(parents)
        or len(parents) > MAX_SNAPSHOTS
    ):
        raise _error("invalid parent snapshot identifiers")
    if (
        type(manifest.get("rows")) is not int
        or manifest["rows"] < 1
        or not isinstance(manifest.get("columns"), dict)
        or not manifest["columns"]
        or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in manifest["columns"].items()
        )
    ):
        raise _error("invalid snapshot row or schema declaration")
    return manifest


def _closure(roots, load):
    """Bound traversal before calling Store.verify, which recursively verifies parents."""
    manifests, heights, visiting = {}, {}, set()

    def visit(snapshot_id, depth):
        if depth > MAX_DEPTH:
            raise _error("snapshot lineage exceeds depth budget")
        if snapshot_id in visiting:
            raise _error("cyclic snapshot lineage")
        if snapshot_id in heights:
            if depth + heights[snapshot_id] > MAX_DEPTH:
                raise _error("snapshot lineage exceeds depth budget")
            return heights[snapshot_id]
        if len(manifests) >= MAX_SNAPSHOTS:
            raise _error("snapshot closure exceeds count budget")
        manifest = load(snapshot_id)
        manifests[snapshot_id] = manifest
        visiting.add(snapshot_id)
        height = max(
            (1 + visit(parent, depth + 1) for parent in manifest.get("parent_snapshot_ids", [])),
            default=0,
        )
        visiting.remove(snapshot_id)
        heights[snapshot_id] = height
        return height

    for snapshot_id in sorted(set(roots)):
        visit(snapshot_id, 0)
    return manifests


def _physical(store, snapshot_id, manifest):
    """Check actual Parquet dimensions and its preserved pandas dtype declarations."""
    try:
        data = parquet.ParquetFile(store.path(snapshot_id) / "data.parquet")
        columns = manifest["columns"]
        names = data.schema_arrow.names
        if (
            data.metadata.num_rows != manifest["rows"]
            or len(names) != len(set(names))
            or set(names) != set(columns)
        ):
            raise _error("physical Parquet row count or columns differ from manifest")
        pandas_metadata = data.schema_arrow.pandas_metadata
        if not pandas_metadata:
            raise _error("Parquet lacks its saved dtype declarations")
        declared = {c["field_name"]: c for c in pandas_metadata["columns"]}
        for name, dtype in columns.items():
            saved = declared[name]
            actual = saved["numpy_type"]
            if dtype.endswith("[pyarrow]"):
                # Arrow decimals can have numpy_type='object'; Arrow timezone
                # timestamps already include their timezone in the dtype string.
                actual = f"{data.schema_arrow.field(name).type}[pyarrow]"
            elif saved["pandas_type"] == "categorical":
                actual = "category"
            elif saved["pandas_type"] == "datetimetz":
                actual = f"{actual[:-1]}, {saved['metadata']['timezone']}]"
            if dtype != actual:
                raise _error("physical Parquet dtype declarations differ from manifest")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _error("unreadable or inconsistent Parquet data") from exc


def _collections(store):
    # Import lazily: the core collections manager is independent of ZIP transport.
    from ssdu.collections import Collections

    return Collections(store)


def _copy_member(source, destination, expected):
    digest, count = hashlib.sha256(), 0
    while chunk := source.read(CHUNK_BYTES):
        count += len(chunk)
        if count > expected["bytes"]:
            raise _error("member exceeds declared byte size")
        digest.update(chunk)
        destination.write(chunk)
    if count != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise _error("member byte size or hash mismatch")


def _bytes_record(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def export_bundle(store, lock: CollectionLock | dict, output) -> dict:
    """Create a new archive atomically, retaining only the exact lock and complete lineage."""
    lock = _lock(lock)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    output = output.parent.resolve() / output.name
    lock_raw = canonical(lock.model_dump()) + b"\n"
    from ssdu.collections import MAX_DOCUMENT_BYTES as lock_budget

    if len(lock_raw) > min(MAX_DOCUMENT_BYTES, lock_budget):
        raise _error("collection lock exceeds size budget")
    raw_manifests = {}
    member_count, byte_count = 2, len(lock_raw)

    def load(snapshot_id):
        nonlocal member_count, byte_count
        path = store.path(snapshot_id) / "manifest.json"
        if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise _error("unsafe or oversized snapshot manifest")
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        manifest = _manifest(raw, snapshot_id)
        member_count += 1 + len(manifest["files"])
        byte_count += len(raw) + sum(r["bytes"] for r in manifest["files"].values())
        if member_count > MAX_MEMBERS or byte_count > MAX_BYTES:
            raise _error("archive exceeds member or byte budget")
        raw_manifests[snapshot_id] = raw
        return manifest

    manifests = _closure((e.snapshot_id for e in lock.entries.values()), load)
    members = {LOCK_MEMBER: _bytes_record(lock_raw)}
    sources = {}
    for snapshot_id, manifest in sorted(manifests.items()):
        prefix = f"snapshots/{snapshot_id}/"
        members[prefix + "manifest.json"] = _bytes_record(raw_manifests[snapshot_id])
        sources[prefix + "manifest.json"] = store.path(snapshot_id) / "manifest.json"
        for name, record in manifest["files"].items():
            members[prefix + name] = record
            sources[prefix + name] = store.path(snapshot_id) / name
    if len(members) + 1 > MAX_MEMBERS or sum(r["bytes"] for r in members.values()) > MAX_BYTES:
        raise _error("archive exceeds member or byte budget")
    bundle = {
        "format": 1,
        "lock_id": lock.lock_id,
        "snapshot_ids": sorted(manifests),
        "members": members,
    }
    bundle["bundle_id"] = hashlib.sha256(canonical(bundle)).hexdigest()
    bundle_raw = canonical(bundle) + b"\n"
    if len(bundle_raw) > MAX_DOCUMENT_BYTES:
        raise _error("bundle documents exceed size budget")
    if byte_count + len(bundle_raw) > MAX_BYTES:
        raise _error("archive exceeds byte budget")
    for snapshot_id, manifest in manifests.items():
        if store.verify(snapshot_id) != manifest:
            raise _error("snapshot changed during export")
        _physical(store, snapshot_id, manifest)
    _collections(store).replay(lock)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".bundle-", suffix=".zip", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted({BUNDLE_MEMBER, *members}):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                with archive.open(info, "w") as target:
                    if name == BUNDLE_MEMBER:
                        target.write(bundle_raw)
                    elif name == LOCK_MEMBER:
                        target.write(lock_raw)
                    else:
                        path = sources[name]
                        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                            _copy_member(source, target, members[name])
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # Atomic exclusive publication: a concurrent writer cannot be overwritten.
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "lock_id": lock.lock_id,
        "name": lock.spec.name,
        "snapshot_ids": sorted(manifests),
        "bundle_id": bundle["bundle_id"],
        "path": str(output),
    }


def _inventory(archive):
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise _error("archive exceeds member count budget")
    members, total = {}, 0
    for info in infos:
        _safe_path(info.filename)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (
            info.filename in members
            or info.is_dir()
            or mode not in {0, stat.S_IFREG}
            or info.flag_bits & 1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            raise _error("duplicate or unsupported archive member")
        total += info.file_size
        if total > MAX_BYTES or info.file_size > MAX_BYTES:
            raise _error("archive exceeds uncompressed byte budget")
        if info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 1000:
            raise _error("archive compression ratio exceeds budget")
        members[info.filename] = info
    return members


def _bundle_document(archive, infos):
    if BUNDLE_MEMBER not in infos or infos[BUNDLE_MEMBER].file_size > MAX_DOCUMENT_BYTES:
        raise _error("missing or oversized bundle manifest")
    bundle = _document(archive.read(BUNDLE_MEMBER))
    if set(bundle) != {"format", "lock_id", "snapshot_ids", "members", "bundle_id"}:
        raise _error("invalid bundle manifest fields")
    if type(bundle["format"]) is not int or bundle["format"] != 1:
        raise _error("unsupported bundle format")
    for key in ("bundle_id", "lock_id"):
        if not isinstance(bundle[key], str) or not SHA256.fullmatch(bundle[key]):
            raise _error("invalid bundle or lock identifier")
    identity = {k: v for k, v in bundle.items() if k != "bundle_id"}
    if hashlib.sha256(canonical(identity)).hexdigest() != bundle["bundle_id"]:
        raise _error("bundle manifest hash mismatch")
    snapshots = bundle["snapshot_ids"]
    if (
        not isinstance(snapshots, list)
        or not 1 <= len(snapshots) <= MAX_SNAPSHOTS
        or any(not isinstance(s, str) or not SHA256.fullmatch(s) for s in snapshots)
        or snapshots != sorted(set(snapshots))
    ):
        raise _error("invalid snapshot closure identifiers")
    records = bundle["members"]
    if not isinstance(records, dict) or LOCK_MEMBER not in records:
        raise _error("missing lock member")
    if set(infos) != {BUNDLE_MEMBER, *records}:
        raise _error("extra or missing archive members")
    for name, record in records.items():
        _safe_path(name)
        _record(record)
        if infos[name].file_size != record["bytes"]:
            raise _error("member size differs from bundle manifest")
        if name != LOCK_MEMBER:
            parts = PurePosixPath(name).parts
            if len(parts) < 3 or parts[0] != "snapshots" or parts[1] not in snapshots:
                raise _error("member outside declared snapshot closure")
    return bundle


def _read_member(archive, name, record):
    if record["bytes"] > MAX_DOCUMENT_BYTES:
        raise _error("JSON document exceeds its size budget")
    raw = archive.read(name)
    if _bytes_record(raw) != record:
        raise _error("member byte size or hash mismatch")
    return raw


def _extract(archive, bundle, staging):
    records = bundle["members"]
    lock = _lock(_document(_read_member(archive, LOCK_MEMBER, records[LOCK_MEMBER])))
    if lock.lock_id != bundle["lock_id"]:
        raise _error("bundle and lock identifiers differ")
    manifests = {}
    expected = {LOCK_MEMBER}
    for snapshot_id in bundle["snapshot_ids"]:
        name = f"snapshots/{snapshot_id}/manifest.json"
        if name not in records:
            raise _error("snapshot manifest is missing")
        manifest = _manifest(_read_member(archive, name, records[name]), snapshot_id)
        manifests[snapshot_id] = manifest
        expected.add(name)
        for relative, record in manifest["files"].items():
            member = f"snapshots/{snapshot_id}/{relative}"
            if records.get(member) != record:
                raise _error("snapshot file differs from bundle integrity record")
            expected.add(member)
    if expected != set(records):
        raise _error("extra or missing snapshot files")

    def load(snapshot_id):
        if snapshot_id not in manifests:
            raise _error("snapshot parent missing from archive")
        return manifests[snapshot_id]

    reachable = _closure((e.snapshot_id for e in lock.entries.values()), load)
    if set(reachable) != set(manifests):
        raise _error("archive contains unrelated snapshots")
    for name in sorted(expected - {LOCK_MEMBER}):
        target = staging.root.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, target.open("xb") as destination:
            _copy_member(source, destination, records[name])
    for snapshot_id, manifest in manifests.items():
        staging.verify(snapshot_id)
        _physical(staging, snapshot_id, manifest)
        if "dataset_contract" in manifest:
            staging.open_dataset(snapshot_id)
    _collections(staging).install_lock(lock)
    return lock


def _publish_import(store, staging, lock, snapshot_ids, activate):
    published = []
    with (store.root / ".publication.lock").open("a") as publication_lock:
        fcntl.flock(publication_lock, fcntl.LOCK_EX)
        missing = []
        for snapshot_id in snapshot_ids:
            destination = store.snapshots / snapshot_id
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise _error("destination snapshot is not a regular directory")
                store.verify(snapshot_id)
                if (destination / "manifest.json").read_bytes() != (
                    staging.path(snapshot_id) / "manifest.json"
                ).read_bytes():
                    raise _error("destination snapshot manifest differs")
            else:
                missing.append(snapshot_id)
        try:
            for snapshot_id in missing:
                destination = store.snapshots / snapshot_id
                source = staging.path(snapshot_id)
                # Track before rename: an interrupt can arrive after the OS moves the
                # directory but before Python observes the successful return.
                published.append((source, destination))
                os.rename(source, destination)
            installed = _collections(store).install_lock(
                lock, activate=activate, _publication_locked=True
            )
        except BaseException:
            # An interrupt after install returns can precede assignment above.
            # A retained matching lock means its captures must remain available.
            manager = _collections(store)
            lock_path = manager.root / lock.spec.name / "locks" / f"{lock.lock_id}.json"
            try:
                committed = manager.get(lock_path).model_dump() == lock.model_dump()
            except (OSError, ValueError, TypeError, RetrievalError):
                committed = False
            if not committed:
                for source, destination in reversed(published):
                    if not source.exists() and destination.exists():
                        shutil.rmtree(destination)
            raise
    return installed, len(published)


def import_bundle(store, path, *, activate=False) -> dict:
    """Validate fully offline, publish complete captures, then install the collection lock."""
    if type(activate) is not bool:
        raise ValueError("activate must be a boolean")
    path = Path(path).resolve()
    if path.stat().st_size > MAX_BYTES + 16 * 1024 * 1024:
        raise _error("compressed archive exceeds size budget")
    try:
        with tempfile.TemporaryDirectory(prefix=".bundle-import-", dir=store.root) as temporary:
            staging = Store(temporary)
            with zipfile.ZipFile(path, "r") as archive:
                infos = _inventory(archive)
                bundle = _bundle_document(archive, infos)
                lock = _extract(archive, bundle, staging)
            installed, count = _publish_import(
                store, staging, lock, bundle["snapshot_ids"], activate
            )
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        if isinstance(exc, RetrievalError):
            raise
        raise _error("archive could not be imported") from exc
    report = installed if isinstance(installed, dict) else {}
    return {
        **report,
        "lock_id": lock.lock_id,
        "name": lock.spec.name,
        "snapshot_ids": bundle["snapshot_ids"],
        "bundle_id": bundle["bundle_id"],
        "imported_snapshot_count": count,
        "path": str(path),
    }
