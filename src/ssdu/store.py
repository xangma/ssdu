"""Local immutable products: raw bytes, normalized Parquet and hashed manifests."""

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import intake
import pandas as pd
from intake.readers import datatypes, entry, readers

from ssdu import __version__
from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.providers import adapter, get_provider, provider_ids

PROVIDERS = provider_ids()


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalization_paths(provider):
    if provider == "local":
        from ssdu.records import implementation_paths

        return implementation_paths()
    return get_provider(provider).normalization_paths()


def _write_json(path, value):
    path.write_bytes(canonical(value) + b"\n")


def _atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".index-")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root="data"):
        self.root = Path(root).resolve()
        self.snapshots = self.root / "snapshots"
        self.snapshots.mkdir(parents=True, exist_ok=True)

    def _index(self):
        path = self.root / "latest.json"
        try:
            index = json.loads(path.read_bytes()) if path.exists() else {}
            if not isinstance(index, dict) or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-f]{64}", value)
                for key, value in index.items()
            ):
                raise ValueError("Expected SHA-256 request keys and snapshot IDs")
            return index
        except (OSError, ValueError) as exc:
            raise RetrievalError(
                "Invalid request index; inspect with ssdu cache --verify, "
                "then repair with ssdu cache --rebuild-index"
            ) from exc

    def _snapshot_paths(self):
        return sorted(
            p
            for p in self.snapshots.iterdir()
            if p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name)
        )

    def _scan_manifests(self):
        """Read per-capture metadata, reporting damage without disabling other captures."""
        manifests, errors = [], []
        for path in self._snapshot_paths():
            try:
                manifest = json.loads((path / "manifest.json").read_bytes())
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("product_id") != path.name
                    or not isinstance(manifest.get("request"), dict)
                ):
                    raise ValueError("Invalid manifest identity or request")
                datetime.fromisoformat(manifest["created_at"])
                manifests.append(manifest)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"product_id": path.name, "error": str(exc)})
        return manifests, errors

    def path(self, product_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12,64}", product_id):
            raise ValueError("Snapshot IDs are 12–64 lowercase hexadecimal characters")
        matches = [
            p
            for p in self.snapshots.iterdir()
            if p.is_dir() and p.name.startswith(product_id) and not p.name.startswith(".")
        ]
        if len(matches) != 1:
            raise RetrievalError(f"Snapshot missing or ID ambiguous: {product_id}")
        return matches[0]

    def list(self) -> list[dict]:
        manifests, errors = self._scan_manifests()
        if errors:
            raise RetrievalError("Unreadable snapshot manifests; inspect with ssdu cache --verify")
        return manifests

    def verify(self, product_id: str) -> dict:
        return self._verify(product_id, {}, set(), 0)[0]

    @staticmethod
    def _validate_dataset_binding(manifest, descriptor):
        marker = manifest.get("dataset_contract")
        expected = {
            "version": descriptor.contract_version,
            "profile": descriptor.profile,
            "descriptor": "dataset.json",
            "transform_id": descriptor.transform.id,
            "transform_version": descriptor.transform.version,
        }
        if marker != expected:
            raise RetrievalError("Dataset contract marker differs from its descriptor")
        source = descriptor.source
        if (
            descriptor.dataset_id != f"{manifest['provider']}:{manifest['dataset_id']}"
            or source.provider != manifest["provider"]
            or source.dataset_id != manifest["dataset_id"]
            or source.url != manifest["source_url"]
            or source.release != manifest["release"]
            or source.licence != manifest["licence"]
            or source.citation != manifest["citation"]
            or source.effective_request != manifest["request"]
            or source.parent_snapshot_ids != manifest.get("parent_snapshot_ids", [])
        ):
            raise RetrievalError("Dataset contract provenance differs from its snapshot")
        if descriptor.quality.source_rows != manifest["rows"]:
            raise RetrievalError(
                "Dataset source-row declaration differs from physical Parquet snapshot"
            )
        required = {"dataset.json", *(table.path for table in descriptor.tables)}
        if not required.issubset(manifest["files"]):
            raise RetrievalError(
                "Dataset contract files are absent from snapshot integrity records"
            )
        if {raw.path for raw in source.raw_inputs} != {
            resource["file"] for resource in manifest["resources"]
        }:
            raise RetrievalError("Dataset raw-input inventory differs from snapshot resources")
        for raw in source.raw_inputs:
            if manifest["files"].get(raw.path) != {"sha256": raw.sha256, "bytes": raw.bytes}:
                raise RetrievalError("Dataset raw-input binding differs from snapshot integrity")

    def _verify(self, product_id, verified, active, depth):
        if depth > 8:
            raise RetrievalError("Snapshot lineage exceeds depth budget")
        path = self.path(product_id)
        if path.name in active:
            raise RetrievalError("Cyclic snapshot lineage")
        if path.name in verified:
            manifest, height = verified[path.name]
            if depth + height > 8:
                raise RetrievalError("Snapshot lineage exceeds depth budget")
            return manifest, height
        if len(verified) + len(active) >= 128:
            raise RetrievalError("Snapshot lineage exceeds count budget")
        active.add(path.name)
        try:
            manifest = json.loads((path / "manifest.json").read_bytes())
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
                raise RetrievalError("Invalid manifest structure")
            identity = {k: v for k, v in manifest.items() if k != "product_id"}
            if hashlib.sha256(canonical(identity)).hexdigest() != path.name:
                raise RetrievalError(f"Manifest hash mismatch: {path.name}")
            if manifest["product_id"] != path.name:
                raise RetrievalError("Snapshot ID does not match its directory")
            for relative, record in manifest["files"].items():
                target = path / relative
                if not target.resolve().is_relative_to(path) or target.is_symlink():
                    raise RetrievalError(f"Unsafe snapshot path: {relative}")
                if not target.is_file() or checksum(target) != record["sha256"]:
                    raise RetrievalError(f"Snapshot file missing or hash mismatch: {relative}")
                if target.stat().st_size != record["bytes"]:
                    raise RetrievalError(f"Snapshot size mismatch: {relative}")
            if "dataset_contract" in manifest or "dataset.json" in manifest["files"]:
                from ssdu.dataset_contract import Dataset

                self._validate_dataset_binding(manifest, Dataset.inspect(path))
            parents = manifest.get("parent_snapshot_ids", [])
            if (
                not isinstance(parents, list)
                or len(parents) > 128
                or len(set(parents)) != len(parents)
                or any(
                    not isinstance(p, str) or not re.fullmatch(r"[a-f0-9]{64}", p) for p in parents
                )
            ):
                raise RetrievalError("Invalid parent snapshot identifiers")
            height = max(
                (1 + self._verify(p, verified, active, depth + 1)[1] for p in parents), default=0
            )
            verified[path.name] = manifest, height
            return manifest, height
        except (OSError, KeyError, ValueError, TypeError, RecursionError) as exc:
            raise RetrievalError(f"Invalid snapshot {product_id}: {exc}") from exc
        finally:
            active.discard(path.name)

    def _cached_manifest(self, request, product_id):
        manifest = self.verify(product_id)
        if canonical(manifest["request"]) != canonical(request.to_dict()):
            raise RetrievalError("Cache index points to a snapshot for a different request")
        return manifest

    def _recover_cached(self, request, key):
        # Match the complete request, never substitute a similar selection or ignore corruption.
        with (self.root / ".publication.lock").open("a") as publication:
            fcntl.flock(publication, fcntl.LOCK_EX)
            with (self.root / ".index.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                index = self._index()
                if key in index:
                    return self._cached_manifest(request, index[key])
                manifests, _ = self._scan_manifests()
                matches = [m for m in manifests if m["request"] == request.to_dict()]
                if not matches:
                    return None
                chosen = self._preferred_capture(matches)
                manifest = self._cached_manifest(request, chosen["product_id"])
                index[key] = manifest["product_id"]
                _atomic_json(self.root / "latest.json", index)
                return manifest

    def _preferred_capture(self, matches):
        # A source can revert to a deduplicated older capture; retain its explicit pointer.
        matches = [m for m in matches if m.get("kind") != "normalization"] or matches
        sources_path = self.root / "sources.json"
        source_matches = [m for m in matches if m.get("kind") == "source"]
        if source_matches and sources_path.exists():
            try:
                sources = json.loads(sources_path.read_bytes())
                if not isinstance(sources, dict):
                    raise TypeError("Expected a source index object")
                for manifest in source_matches:
                    key = f"{manifest['provider']}:{manifest['dataset_id']}"
                    if sources.get(key) == manifest["product_id"]:
                        return manifest
            except (OSError, ValueError, TypeError) as exc:
                raise RetrievalError("Invalid source index; request recovery stopped") from exc
        return max(matches, key=lambda m: (m["created_at"], m["product_id"]))

    def fetch(self, request: Request, *, refresh=False, offline=False, client=None) -> dict:
        if offline and refresh:
            raise ValueError("Offline retrieval cannot refresh providers")
        key = hashlib.sha256(canonical(request.to_dict())).hexdigest()
        # Coalesce identical concurrent cache misses across local processes.
        locks = self.root / ".request-locks"
        locks.mkdir(exist_ok=True)
        with (locks / key).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._fetch(request, key, refresh=refresh, offline=offline, client=client)

    def fetch_dataset(self, request: Request, *, refresh=False, offline=False, client=None):
        """Acquire or reuse a selection and return the common versioned Dataset API.

        Cached historical products are normalized locally into a separate derived
        snapshot. Their bytes, request pointers and collection locks stay intact.
        """
        manifest = self.fetch(request, refresh=refresh, offline=offline, client=client)
        if "dataset_contract" in manifest:
            return self.open_dataset(manifest["product_id"])
        return self.migrate_dataset(manifest["product_id"])

    def import_dataset(self, path, spec):
        """Import a local CSV or Parquet file and return its standardized Dataset."""
        from ssdu.local_import import import_local

        manifest = import_local(self, path, spec)
        return self.open_dataset(manifest["product_id"])

    def open_dataset(self, snapshot_id: str):
        """Verify and open a retained standardized dataset without acquisition or mutation."""
        from ssdu.dataset_contract import Dataset

        manifest = self.verify(snapshot_id)
        snapshot_id = manifest["product_id"]
        if "dataset_contract" not in manifest:
            raise RetrievalError(
                "Historical snapshot has no dataset contract; use migrate_dataset(snapshot_id)"
            )
        dataset = Dataset.read(self.path(snapshot_id), snapshot_id=snapshot_id)
        self._validate_dataset_binding(manifest, dataset.descriptor)
        return dataset

    def _normalization_raw_frame(self, manifest):
        """Read original interpretation inputs through the registered provider recipe."""
        if manifest["provider"] not in PROVIDERS:
            return None
        reader = getattr(adapter(manifest["provider"]), "normalization_source", None)
        return reader(self, manifest) if reader is not None else None

    def migrate_dataset(self, snapshot_id: str):
        """Normalize historical evidence offline; publish a new immutable derived snapshot."""
        from ssdu.dataset_contract import CONTRACT_VERSION, RECORDS_CONTRACT_VERSION

        manifest = self.verify(snapshot_id)
        snapshot_id = manifest["product_id"]
        if "dataset_contract" in manifest:
            return self.open_dataset(snapshot_id)
        provider = manifest["provider"]
        if provider == "local":
            from ssdu.records import TRANSFORM_ID, TRANSFORM_VERSION

            contract_version = RECORDS_CONTRACT_VERSION
            profile = "records"
            transform_id, transform_version = TRANSFORM_ID, TRANSFORM_VERSION
        elif provider in PROVIDERS:
            registration = get_provider(provider)
            profile = registration.profile
            contract_version = (
                RECORDS_CONTRACT_VERSION if profile == "records" else CONTRACT_VERSION
            )
            transform_id, transform_version = (
                registration.transform_id,
                registration.transform_version,
            )
        else:
            raise RetrievalError("No standardized dataset profile exists for this provider")
        implementation = {p.name: checksum(p) for p in _normalization_paths(provider)}
        marker = {
            "version": contract_version,
            "profile": profile,
            "descriptor": "dataset.json",
            "transform_id": transform_id,
            "transform_version": transform_version,
        }
        cache_key = hashlib.sha256(
            canonical({"parent": snapshot_id, "contract": marker, "implementation": implementation})
        ).hexdigest()
        locks = self.root / ".normalization-locks"
        locks.mkdir(exist_ok=True)

        def matches(candidate):
            return (
                candidate.get("kind") == "normalization"
                and candidate.get("parent_snapshot_ids") == [snapshot_id]
                and candidate.get("dataset_contract") == marker
                and candidate.get("request") == manifest["request"]
                and all(
                    candidate.get("implementation_sha256", {}).get(n) == h
                    for n, h in implementation.items()
                )
            )

        with (locks / cache_key).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index = self._normalization_index()
            if cache_key in index:
                retained = self.verify(index[cache_key])
                if not matches(retained):
                    raise RetrievalError(
                        "Normalization index points to a different source or transform"
                    )
                return self.open_dataset(retained["product_id"])
            retained, _ = self._scan_manifests()
            candidates = [m for m in retained if matches(m)]
            if candidates:
                dataset = self.open_dataset(
                    max(candidates, key=lambda m: m["product_id"])["product_id"]
                )
            else:
                dataset = self._migrate_dataset(manifest)
            with (self.root / ".normalization-index.lock").open("a") as index_lock:
                fcntl.flock(index_lock, fcntl.LOCK_EX)
                index = self._normalization_index()
                index[cache_key] = dataset.snapshot_id
                _atomic_json(self.root / "normalizations.json", index)
            return dataset

    def _normalization_index(self):
        path = self.root / "normalizations.json"
        try:
            value = json.loads(path.read_bytes()) if path.exists() else {}
            if not isinstance(value, dict) or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not isinstance(target, str)
                or not re.fullmatch(r"[0-9a-f]{64}", target)
                for key, target in value.items()
            ):
                raise ValueError("Expected SHA-256 normalization keys and snapshot IDs")
            return value
        except (OSError, ValueError) as exc:
            raise RetrievalError("Invalid normalization cache index") from exc

    def _migrate_dataset(self, manifest):
        from ssdu.normalization import normalize

        snapshot_id = manifest["product_id"]
        acquisition = Acquisition(
            provider=manifest["provider"],
            dataset_id=manifest["dataset_id"],
            title=manifest["title"],
            frame=self.replay(snapshot_id),
            metadata=json.loads((self.path(snapshot_id) / "metadata.json").read_bytes()),
            release=manifest["release"],
            licence=manifest["licence"],
            citation=manifest["citation"],
            source_url=manifest["source_url"],
        )
        request = Request(**manifest["request"])
        if manifest["provider"] == "local":
            from ssdu.records import normalize_records, retained_frame

            acquisition.frame = retained_frame(manifest, self.path(snapshot_id))
            acquisition.dataset = normalize_records(acquisition, request=request)
        else:
            acquisition.dataset = normalize(
                acquisition, request=request, raw_frame=self._normalization_raw_frame(manifest)
            )
        transform = acquisition.dataset.descriptor.transform
        migrated = self._publish(
            request,
            hashlib.sha256(canonical(request.to_dict())).hexdigest(),
            acquisition,
            [],
            kind="normalization",
            parents=[snapshot_id],
            transformation_version=(
                f"dataset-contract-{acquisition.dataset.descriptor.contract_version}:"
                f"{transform.id}:{transform.version}"
            ),
            implementation_paths=_normalization_paths(manifest["provider"]),
            index_request=False,
        )
        return self.open_dataset(migrated["product_id"])

    def _fetch(self, request, key, *, refresh, offline, client):
        cached = self._index().get(key)
        if cached and not refresh:
            # Frozen cache hits never contact a provider or silently repair corrupted bytes.
            return self._cached_manifest(request, cached)
        if not refresh:
            recovered = self._recover_cached(request, key)
            if recovered:
                return recovered
        registration = get_provider(request.provider)
        retained_fetch = getattr(adapter(request.provider), "retained_fetch", None)
        if retained_fetch is not None:
            return retained_fetch(self, request, refresh=refresh, offline=offline, client=client)
        if registration.workflow:
            from ssdu.connector_cache import CachedClient, seed_from_snapshots

            if offline:
                seed_from_snapshots(self, provider=request.provider)
            if getattr(client, "persistent_cache", False) and not offline:
                return self._acquire(request, key, client)
            with CachedClient(
                self.root,
                client=client,
                refresh=refresh,
                offline=offline,
                allowed_hosts=registration.allowed_hosts,
            ) as cached_client:
                return self._acquire(request, key, cached_client)
        if offline:
            raise RetrievalError("No retained snapshot for this exact request; fetch online once")
        if client is None:
            with HttpClient() as transport:
                return self._acquire(request, key, transport)
        return self._acquire(request, key, client)

    def _acquire(self, request, key, client):
        start = len(client.resources)
        acquisition = adapter(request.provider).fetch(request, client)
        return self._publish(request, key, acquisition, client.resources[start:])

    def _publish(
        self,
        request,
        key,
        acquisition,
        resources,
        *,
        kind=None,
        parents=(),
        raw_paths=None,
        transformation_version="iod-normalise-v1",
        implementation_paths=(),
        index_request=True,
    ):
        """Publish a capture; v2 products deduplicate unchanged content before publication."""
        frame = acquisition.frame
        if frame.empty or len(frame) > request.max_rows:
            raise RetrievalError("Adapter returned an empty or oversized product")
        if acquisition.provider != request.provider or acquisition.dataset_id != request.dataset_id:
            raise RetrievalError("Adapter returned a different provider or dataset")
        temporary = Path(tempfile.mkdtemp(dir=self.snapshots, prefix=".staging-"))
        try:
            frame.to_parquet(temporary / "data.parquet", index=False)
            _write_json(temporary / "metadata.json", acquisition.metadata)
            raw_dir = temporary / "raw"
            raw_dir.mkdir()
            records = []
            for i, resource in enumerate(resources):
                relative = f"raw/{i:04d}.bin"
                origin = (raw_paths or {}).get(i)
                if origin:
                    os.link(origin, temporary / relative)
                else:
                    (temporary / relative).write_bytes(resource.body)
                records.append(
                    {
                        "file": relative,
                        "url": resource.url,
                        "method": resource.method,
                        "request_body": resource.request_body,
                        "content_type": resource.content_type,
                        "response_headers": resource.headers,
                        **({"cache_scope": resource.cache_scope} if resource.cache_scope else {}),
                    }
                )
            dataset = acquisition.dataset
            if dataset is not None:
                from ssdu.dataset_contract import Dataset

                descriptor = dataset.contract
                descriptor["source"]["effective_request"] = request.to_dict()
                descriptor["source"]["parent_snapshot_ids"] = list(parents)
                descriptor["source"]["raw_inputs"] = [
                    {
                        "path": record["file"],
                        "sha256": checksum(temporary / record["file"]),
                        "bytes": (temporary / record["file"]).stat().st_size,
                    }
                    for record in records
                ]
                dataset = Dataset.from_tables(
                    descriptor, {name: dataset.to_arrow(name) for name in dataset.table_names}
                )
                dataset.write(temporary)
            files = {
                str(p.relative_to(temporary)): {
                    "sha256": checksum(p),
                    "bytes": p.stat().st_size,
                }
                for p in sorted(temporary.rglob("*"))
                if p.is_file()
            }
            identity = {
                "schema_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "provider": acquisition.provider,
                "dataset_id": acquisition.dataset_id,
                "title": acquisition.title,
                "request": request.to_dict(),
                "release": acquisition.release,
                "licence": acquisition.licence,
                "citation": acquisition.citation,
                "source_url": acquisition.source_url,
                "rows": len(frame),
                "columns": {str(c): str(t) for c, t in frame.dtypes.items()},
                "software": {
                    "ssdu-retrieval": __version__,
                    **{p: version(p) for p in ("intake", "pandas", "pyarrow", "httpx")},
                },
                "runtime": {"python": platform.python_version(), "platform": platform.system()},
                "implementation_sha256": {
                    p.name: checksum(p)
                    for p in (
                        Path(__file__),
                        Path(__file__).with_name("http.py"),
                        Path(__file__).with_name("rate_limit.py"),
                        Path(__file__).with_name("models.py"),
                        *(
                            get_provider(request.provider).implementation_paths()
                            if request.provider in PROVIDERS
                            else []
                        ),
                        *([Path(__file__).with_name("shared.py")] if kind else []),
                        *implementation_paths,
                    )
                },
                "resources": records,
                "files": files,
                "validation": {"complete": True, "all_files_hashed": True},
            }
            if dataset is not None:
                descriptor = dataset.descriptor
                identity["dataset_contract"] = {
                    "version": descriptor.contract_version,
                    "profile": descriptor.profile,
                    "descriptor": "dataset.json",
                    "transform_id": descriptor.transform.id,
                    "transform_version": descriptor.transform.version,
                }
                identity["implementation_sha256"].update(
                    {p.name: checksum(p) for p in _normalization_paths(request.provider)}
                )
            if kind:
                identity.update(
                    schema_version=2,
                    kind=kind,
                    parent_snapshot_ids=list(parents),
                    transformation_version=transformation_version,
                )
                fingerprint = {
                    k: identity[k]
                    for k in (
                        "provider",
                        "dataset_id",
                        "title",
                        "request",
                        "release",
                        "licence",
                        "citation",
                        "source_url",
                        "files",
                        "kind",
                        "parent_snapshot_ids",
                        "transformation_version",
                    )
                }
                if dataset is not None:
                    fingerprint["dataset_contract"] = identity["dataset_contract"]
                    fingerprint["normalization_implementation"] = {
                        p.name: identity["implementation_sha256"][p.name]
                        for p in _normalization_paths(request.provider)
                    }
                identity["content_fingerprint"] = hashlib.sha256(canonical(fingerprint)).hexdigest()
            if dataset is not None:
                self._validate_dataset_binding(identity, dataset.descriptor)
            return self._finish_publication(temporary, identity, key, index_request=index_request)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _finish_publication(self, temporary, identity, key, *, index_request=True):
        # Concurrent equivalent selections must also deduplicate atomically.
        with (self.root / ".publication.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if "content_fingerprint" in identity:
                saved_manifests, _ = self._scan_manifests()
                for saved in saved_manifests:
                    if saved.get("content_fingerprint") == identity["content_fingerprint"]:
                        try:
                            saved = self.verify(saved["product_id"])
                        except RetrievalError:
                            # An explicit acquisition may publish valid replacement bytes.
                            # The damaged capture remains identifiable and is never overwritten.
                            continue
                        if index_request:
                            self._update_index(key, saved["product_id"])
                        return saved
            product_id = hashlib.sha256(canonical(identity)).hexdigest()
            manifest = {"product_id": product_id, **identity}
            _write_json(temporary / "manifest.json", manifest)
            final = self.snapshots / product_id
            # Publication is a directory rename; no partially written product is visible.
            os.rename(temporary, final)
            self.verify(product_id)
            if index_request:
                self._update_index(key, product_id)
            return manifest

    def _update_index(self, key, product_id):
        with (self.root / ".index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index = self._index()
            index[key] = product_id
            _atomic_json(self.root / "latest.json", index)

    def cache_status(self, *, verify=False):
        """Inspect retained files and request pointers without provider access or deletion."""
        manifests, invalid = self._scan_manifests()
        by_id = {m["product_id"]: m for m in manifests}
        verified = 0
        if verify:
            for manifest in manifests:
                try:
                    self.verify(manifest["product_id"])
                    verified += 1
                except RetrievalError as exc:
                    invalid.append({"product_id": manifest["product_id"], "error": str(exc)})
        index_errors = []
        try:
            index = self._index()
        except RetrievalError as exc:
            index, index_errors = {}, [str(exc)]
        bad_ids = {item["product_id"] for item in invalid}
        for key, product_id in index.items():
            manifest = by_id.get(product_id)
            if manifest is None or product_id in bad_ids:
                index_errors.append(
                    f"Request {key} points to missing or invalid snapshot {product_id}"
                )
            elif hashlib.sha256(canonical(manifest["request"])).hexdigest() != key:
                index_errors.append(f"Request {key} points to a different request: {product_id}")
        logical_bytes, unique_files = 0, {}
        for path in self._snapshot_paths():
            for file in path.rglob("*"):
                if (
                    file.is_file()
                    and not file.is_symlink()
                    and file.resolve().is_relative_to(self.snapshots)
                ):
                    stat = file.stat()
                    logical_bytes += stat.st_size
                    unique_files[(stat.st_dev, stat.st_ino)] = stat.st_size
        return {
            "snapshot_count": len(self._snapshot_paths()),
            "verified_count": verified if verify else None,
            "invalid_snapshots": invalid,
            "index_errors": index_errors,
            "request_entries": len(index),
            "logical_bytes": logical_bytes,
            "unique_file_bytes": sum(unique_files.values()),
            "unindexed_snapshot_ids": sorted(set(by_id) - set(index.values())),
            "incomplete_staging_count": len(list(self.snapshots.glob(".staging-*"))),
        }

    def rebuild_index(self):
        """Reconstruct verified request pointers locally; never rewrite retained products."""
        with (self.root / ".publication.lock").open("a") as publication:
            fcntl.flock(publication, fcntl.LOCK_EX)
            with (self.root / ".index.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                manifests, invalid = self._scan_manifests()
                groups = {}
                for manifest in manifests:
                    try:
                        manifest = self.verify(manifest["product_id"])
                        key = hashlib.sha256(canonical(manifest["request"])).hexdigest()
                        groups.setdefault(key, []).append(manifest)
                    except RetrievalError as exc:
                        invalid.append({"product_id": manifest["product_id"], "error": str(exc)})
                repaired = []
                try:
                    old_index = self._index()
                except RetrievalError as exc:
                    old_index = {}
                    repaired.append(str(exc))
                rebuilt, preserved = {}, 0
                for key, matches in groups.items():
                    old_id = old_index.get(key)
                    if old_id and any(m["product_id"] == old_id for m in matches):
                        rebuilt[key] = old_id
                        preserved += 1
                    else:
                        rebuilt[key] = self._preferred_capture(matches)["product_id"]
                for key, old_id in old_index.items():
                    if rebuilt.get(key) != old_id:
                        repaired.append(
                            f"Removed or corrected invalid request pointer {key}: {old_id}"
                        )
                _atomic_json(self.root / "latest.json", rebuilt)
                return {
                    "request_entries": len(rebuilt),
                    "preserved_entries": preserved,
                    "recovered_entries": len(rebuilt) - preserved,
                    "invalid_snapshots": invalid,
                    "index_errors": [],
                    "repaired_index_errors": repaired,
                    "selection_policy": "Preserve valid request pointers; otherwise explicit current "
                    "source, then most recent verified capture",
                }

    def replay(self, product_id: str) -> pd.DataFrame:
        self.verify(product_id)
        return pd.read_parquet(self.path(product_id) / "data.parquet")

    def catalogue(self, product_ids=None, output=None) -> Path:
        manifests = (
            [self.verify(m["product_id"]) for m in self.list()]
            if product_ids is None
            else [self.verify(product_id) for product_id in product_ids]
        )
        catalogue = entry.Catalog(metadata={"title": "SSDU reproducible snapshots", "format": 1})
        for manifest in manifests:
            product_id = manifest["product_id"]
            metadata = {
                k: manifest[k]
                for k in (
                    "product_id",
                    "provider",
                    "dataset_id",
                    "title",
                    "rows",
                    "release",
                    "licence",
                    "citation",
                    "created_at",
                )
            }
            data = datatypes.Parquet(
                url=str(self.path(product_id) / "data.parquet"), metadata=metadata
            )
            name = f"{manifest['provider']}_{product_id[:12]}"
            catalogue.add_entry(readers.PandasParquet(data), name=name)
        target = Path(output).resolve() if output else self.root / "catalog.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        catalogue.to_yaml_file(str(target))
        return target

    def read_intake(self, product_id):
        self.verify(product_id)
        catalogue = self.catalogue([product_id], self.root / f"catalog-{product_id}.yaml")
        manifest = self.verify(product_id)
        name = f"{manifest['provider']}_{manifest['product_id'][:12]}"
        return intake.from_yaml_file(str(catalogue))[name].read()
