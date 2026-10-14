"""Named, versioned collections with pinned metadata and offline query replay."""

import fcntl
import hashlib
import json
import re
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from ssdu import __version__
from ssdu.collection_models import CollectionLock, CollectionSpec, result_hash
from ssdu.models import Request, RetrievalError
from ssdu.registry import Registry, dataset_key, snapshot_descriptor
from ssdu.service import Service
from ssdu.store import Store, _atomic_json, canonical

MAX_DOCUMENT_BYTES = 2_000_000
NAME = re.compile(r"[a-z][a-z0-9_-]{0,79}")
SHA = re.compile(r"[0-9a-f]{64}")


def read_document(path):
    """Read bounded JSON/YAML using safe YAML, without executing provider content."""
    with Path(path).open("rb") as stream:
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Collection document exceeds 2 MB")
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid JSON/YAML document") from exc
    if not isinstance(document, dict):
        raise TypeError("Expected a JSON/YAML object")
    return document


class Collections:
    def __init__(self, store="data"):
        self.store = store if isinstance(store, Store) else Store(store)
        self.root = self.store.root / "collections"

    def get(self, ref):
        if isinstance(ref, CollectionLock):
            return CollectionLock.model_validate(ref.model_dump())
        if isinstance(ref, dict):
            return CollectionLock.model_validate(ref)
        value = str(ref)
        if NAME.fullmatch(value) and not SHA.fullmatch(value):
            pointer = read_document(self.root / value / "current.json")
            lock_id = pointer.get("lock_id")
            if not isinstance(lock_id, str) or not SHA.fullmatch(lock_id):
                raise RetrievalError("Invalid collection current pointer")
            path = self.root / value / "locks" / f"{lock_id}.json"
            lock = CollectionLock.model_validate(read_document(path))
            if lock.spec.name != value or lock.lock_id != lock_id:
                raise RetrievalError("Collection pointer targets a different collection or lock")
            return lock
        if SHA.fullmatch(value):
            paths = list(self.root.glob(f"*/locks/{value}.json"))
            if len(paths) != 1:
                raise RetrievalError("Collection lock missing or ambiguous")
            lock = CollectionLock.model_validate(read_document(paths[0]))
            if lock.lock_id != value or paths[0].parent.parent.name != lock.spec.name:
                raise RetrievalError("Collection lock identity differs from its path")
            return lock
        return CollectionLock.model_validate(read_document(ref))

    def list(self):
        if not self.root.exists():
            return []
        result = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not NAME.fullmatch(directory.name):
                continue
            if not (directory / "current.json").exists():
                continue
            lock = self.get(directory.name)
            result.append(
                {
                    "name": directory.name,
                    "current_lock_id": lock.lock_id,
                    "entries": list(lock.entries),
                    "lock_ids": sorted(
                        p.stem
                        for p in (directory / "locks").glob("*.json")
                        if SHA.fullmatch(p.stem)
                    ),
                }
            )
        return result

    def resolve(self, spec, *, offline=False, refresh=False, activate=True, snapshot_ids=None):
        if offline and refresh:
            raise ValueError("Offline collection resolution cannot refresh providers")
        if isinstance(spec, (str, Path)):
            spec = read_document(spec)
        spec = CollectionSpec.model_validate(spec)
        if snapshot_ids is not None and set(snapshot_ids) != set(spec.entries):
            raise ValueError("Resolved snapshot names differ from collection entries")
        entries = {}
        service = Service(self.store.root, index_store=False)
        for name, entry in spec.entries.items():
            manifest = (
                self.store.verify(snapshot_ids[name])
                if snapshot_ids is not None
                else (
                    self.store.fetch(
                        Request(**entry.request.model_dump()), offline=offline, refresh=refresh
                    )
                    if entry.request
                    else self.store.verify(entry.snapshot_id)
                )
            )
            if snapshot_ids is not None and (
                (entry.snapshot_id is not None and entry.snapshot_id != manifest["product_id"])
                or (entry.request is not None and entry.request.model_dump() != manifest["request"])
            ):
                raise RetrievalError("Resolved snapshot differs from the collection input")
            metadata = snapshot_descriptor(self.store, manifest)
            result = service.query_data(manifest["product_id"], entry.query)
            entries[name] = {
                "snapshot_id": manifest["product_id"],
                "dataset_id": dataset_key(manifest["provider"], manifest["dataset_id"]),
                "request": manifest["request"],
                "metadata_version": hashlib.sha256(canonical(metadata)).hexdigest(),
                "metadata": metadata,
                "query": entry.query.model_dump(),
                "result_sha256": result_hash(result),
            }
        identity = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "spec": spec.model_dump(),
            "entries": entries,
            "query_engine": "ssdu-query-v1",
            "software": {"ssdu-retrieval": __version__, "duckdb": version("duckdb")},
        }
        pointer = self.root / spec.name / "current.json"
        if pointer.exists():
            previous = self.get(spec.name)
            if previous.model_dump(exclude={"lock_id", "created_at"}) == {
                k: v for k, v in identity.items() if k != "created_at"
            }:
                self.install_lock(previous, activate=activate)
                return previous
        if not activate:
            # Jobs may propose a version without replacing an existing current lock.
            # Repeated cached jobs should reuse that same immutable proposal.
            for path in sorted((self.root / spec.name / "locks").glob("*.json")):
                previous = self.get(path)
                if previous.model_dump(exclude={"lock_id", "created_at"}) == {
                    k: v for k, v in identity.items() if k != "created_at"
                }:
                    self.install_lock(previous, activate=False)
                    return previous
        lock = CollectionLock.model_validate(
            {**identity, "lock_id": hashlib.sha256(canonical(identity)).hexdigest()}
        )
        self.install_lock(lock, activate=activate)
        return lock

    def _verify_entry(self, entry):
        manifest = self.store.verify(entry.snapshot_id)
        key = dataset_key(manifest["provider"], manifest["dataset_id"])
        if key != entry.dataset_id or manifest["request"] != entry.request:
            raise RetrievalError("Locked request or dataset differs from the retained snapshot")
        physical = pq.read_schema(self.store.path(entry.snapshot_id) / "data.parquet")
        if (
            pq.read_metadata(self.store.path(entry.snapshot_id) / "data.parquet").num_rows
            != manifest["rows"]
        ):
            raise RetrievalError("Snapshot row count differs from its manifest")
        if set(physical.names) != set(manifest["columns"]):
            raise RetrievalError("Snapshot columns differ from its manifest")
        metadata = json.loads((self.store.path(entry.snapshot_id) / "metadata.json").read_bytes())
        fixed = {
            k: manifest[k]
            for k in ("provider", "title", "source_url", "licence", "citation", "release")
        }
        fixed.update(dataset_id=key, native_id=manifest["dataset_id"], source_metadata=metadata)
        if any(entry.metadata.get(k) != v for k, v in fixed.items()):
            raise RetrievalError("Pinned metadata differs from the retained source evidence")
        columns = entry.metadata.get("columns", [])
        if not isinstance(columns, list) or any(not isinstance(c, dict) for c in columns):
            raise RetrievalError("Invalid pinned column metadata")
        actual = {field.name: str(field.type) for field in physical}
        if {column.get("name"): column.get("storage_dtype") for column in columns} != actual or len(
            columns
        ) != len(actual):
            raise RetrievalError("Pinned metadata storage types differ from the snapshot")
        return manifest

    def replay(self, ref):
        lock = self.get(ref)
        service = Service(self.store.root, index_store=False)
        results = {}
        for name, entry in lock.entries.items():
            self._verify_entry(entry)
            result = service.query_data(entry.snapshot_id, entry.query)
            if result_hash(result) != entry.result_sha256:
                raise RetrievalError(f"Locked query result changed: {name}")
            # Query execution uses physical types; interpretation comes from the pinned document.
            result["provenance"]["metadata_version"] = entry.metadata_version
            result["provenance"]["collection_lock_id"] = lock.lock_id
            results[name] = {**result, "metadata": entry.metadata}
        return {
            "name": lock.spec.name,
            "lock_id": lock.lock_id,
            "offline": True,
            "entries": results,
        }

    def install_lock(self, lock, *, activate=False, _publication_locked=False):
        # A collection must not commit references to captures an importing process
        # can still roll back. Importers already hold this same publication lock.
        publication = (
            nullcontext()
            if _publication_locked
            else (self.store.root / ".publication.lock").open("a")
        )
        with publication as handle:
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            return self._install_lock(lock, activate=activate)

    def _install_lock(self, lock, *, activate=False):
        lock = self.get(lock)
        document = lock.model_dump()
        if len(canonical(document)) > MAX_DOCUMENT_BYTES:
            raise ValueError("Collection lock exceeds 2 MB")
        self.replay(lock)
        directory = self.root / lock.spec.name
        locks = directory / "locks"
        lock_path = locks / f"{lock.lock_id}.json"
        pointer_path = directory / "current.json"
        catalogue_path = directory / f"catalog-{lock.lock_id}.yaml"
        registry = Registry(self.store.root)
        with (self.store.root / ".collections.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            old_pointer = read_document(pointer_path) if pointer_path.exists() else None
            old_pointer_bytes = pointer_path.read_bytes() if old_pointer is not None else None
            if old_pointer is not None:
                self.get(lock.spec.name)
            existing = lock_path.exists()
            if existing and self.get(lock_path).model_dump() != document:
                raise RetrievalError("Existing lock differs; refusing to overwrite it")
            ids = [entry.snapshot_id for entry in lock.entries.values()]
            with tempfile.TemporaryDirectory(
                dir=self.store.root, prefix=".collection-stage-"
            ) as stage:
                staged_catalogue = self.store.catalogue(ids, Path(stage) / "catalog.yaml")
                locks.mkdir(parents=True, exist_ok=True)
                old_catalogue = catalogue_path.read_bytes() if catalogue_path.exists() else None
                make_current = activate or old_pointer is None
                pointer_written = False
                try:
                    with registry.connect() as db:
                        seen = set()

                        def register(snapshot_id):
                            if snapshot_id in seen:
                                return
                            seen.add(snapshot_id)
                            manifest = self.store.verify(snapshot_id)
                            for parent in manifest.get("parent_snapshot_ids", []):
                                register(parent)
                            registry.register(self.store, manifest, preserve_current=True, db=db)

                        for snapshot_id in ids:
                            register(snapshot_id)
                        for entry in lock.entries.values():
                            registry.retain_metadata(entry.metadata, db=db)
                        if not existing:
                            _atomic_json(lock_path, document)
                        staged_catalogue.replace(catalogue_path)
                        if make_current:
                            # An interrupt may arrive after replace but before return.
                            pointer_written = True
                            _atomic_json(pointer_path, {"lock_id": lock.lock_id})
                except BaseException:
                    if pointer_written:
                        if old_pointer is None:
                            pointer_path.unlink(missing_ok=True)
                        elif (
                            not pointer_path.exists()
                            or pointer_path.read_bytes() != old_pointer_bytes
                        ):
                            _atomic_json(pointer_path, old_pointer)
                    if not existing:
                        lock_path.unlink(missing_ok=True)
                    if old_catalogue is None:
                        catalogue_path.unlink(missing_ok=True)
                    else:
                        catalogue_path.write_bytes(old_catalogue)
                    raise
        return {
            "name": lock.spec.name,
            "lock_id": lock.lock_id,
            "lock_path": str(lock_path),
            "current": make_current or old_pointer.get("lock_id") == lock.lock_id,
            "catalogue": str(catalogue_path),
        }
