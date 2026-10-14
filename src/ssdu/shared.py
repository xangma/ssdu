"""One retained national deprivation source; local selections reference it."""

import fcntl
import hashlib
import json

from ssdu.adapters import deprivation
from ssdu.http import HttpClient, Resource
from ssdu.models import Acquisition, RetrievalError
from ssdu.registry import Registry, dataset_key
from ssdu.store import _atomic_json, canonical


class SharedDeprivation:
    def __init__(self, store):
        self.store = store
        self.registry = Registry(store.root)
        self.key = dataset_key(deprivation.PROVIDER, deprivation.DATASET_ID)

    def source(self, *, refresh=False, offline=False, client=None):
        if refresh and offline:
            raise ValueError("Offline preparation cannot refresh providers")
        with (self.store.root / ".deprivation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            index_path = self.store.root / "sources.json"
            index = json.loads(index_path.read_bytes()) if index_path.exists() else {}
            if self.key in index and not refresh:
                manifest = self.store.verify(index[self.key])
                self._validate_source(manifest)
                self.registry.register(self.store, manifest, make_current=True)
                return manifest
            origins = {}
            resource = None
            if not refresh:
                # Deduplicated reversions can make an older capture current.
                # Recover that explicit registry choice before using capture timestamps.
                current_id = self.registry.get(self.key)["current_snapshot_id"]
                if current_id:
                    current = self.store.verify(current_id)
                    if current.get("kind") == "source":
                        self._validate_source(current)
                        self.registry.register(self.store, current, make_current=True)
                        index[self.key] = current_id
                        _atomic_json(index_path, index)
                        self.registry.check(self.key, "index_recovered", current_id)
                        return current
                # Reuse the national raw file already retained by a legacy LAD capture.
                retained, _ = self.store._scan_manifests()
                for saved in sorted(retained, key=lambda m: m["created_at"], reverse=True):
                    if (
                        saved["provider"] != deprivation.PROVIDER
                        or saved["dataset_id"] != deprivation.DATASET_ID
                    ):
                        continue
                    if saved.get("kind") == "source":
                        manifest = self.store.verify(saved["product_id"])
                        self._validate_source(manifest)
                        self.registry.register(self.store, manifest, make_current=True)
                        index[self.key] = manifest["product_id"]
                        _atomic_json(index_path, index)
                        self.registry.check(self.key, "index_recovered", manifest["product_id"])
                        return manifest
                    for record in saved["resources"]:
                        if record["url"] != deprivation.SOURCE_URL:
                            continue
                        self.store.verify(saved["product_id"])
                        path = self.store.path(saved["product_id"]) / record["file"]
                        resource = Resource(
                            path.read_bytes(),
                            record["url"],
                            record["method"],
                            record["request_body"],
                            record["content_type"],
                            record["response_headers"],
                        )
                        origins[0] = path
                        break
                    if resource:
                        break
            contacted = resource is None
            if contacted and offline:
                raise RetrievalError("No retained national deprivation CSV; prepare online once")
            try:
                if resource is None:
                    if client is None:
                        with HttpClient() as transport:
                            resource = transport.get(deprivation.SOURCE_URL)
                    else:
                        resource = client.get(deprivation.SOURCE_URL)
                request = deprivation.national_request()
                acquisition = deprivation.from_resource(request, resource, national=True)
                key = hashlib.sha256(canonical(request.to_dict())).hexdigest()
                manifest = self.store._publish(
                    request, key, acquisition, [resource], kind="source", raw_paths=origins
                )
                self.registry.register(self.store, manifest, make_current=True)
                index[self.key] = manifest["product_id"]
                _atomic_json(index_path, index)
                self.registry.check(
                    self.key,
                    "refresh_success" if contacted else "local_import",
                    manifest["product_id"],
                    provider_contact=contacted,
                )
                return manifest
            except Exception:
                self.registry.check(
                    self.key,
                    "refresh_failed" if contacted else "import_failed",
                    provider_contact=contacted,
                    message="Preparation failed; prior source preserved",
                )
                raise

    @staticmethod
    def _validate_source(manifest):
        if (
            manifest.get("kind") != "source"
            or manifest["request"] != deprivation.national_request().to_dict()
            or manifest["source_url"] != deprivation.SOURCE_URL
        ):
            raise RetrievalError("Source index points to a different dataset or selection")

    def inspect_legacy(self, snapshot_id):
        """Verify retained national raw bytes without publishing or contacting a provider."""
        saved = self.store.verify(snapshot_id)
        if (
            saved["provider"] != deprivation.PROVIDER
            or saved["dataset_id"] != deprivation.DATASET_ID
        ):
            raise RetrievalError("Legacy source identifies a different dataset")
        for record in saved["resources"]:
            if record["url"] == deprivation.SOURCE_URL:
                path = self.store.path(snapshot_id) / record["file"]
                resource = Resource(
                    path.read_bytes(),
                    record["url"],
                    record["method"],
                    record["request_body"],
                    record["content_type"],
                    record["response_headers"],
                )
                acquisition = deprivation.from_resource(
                    deprivation.national_request(), resource, national=True
                )
                return acquisition, resource, path
        raise RetrievalError("Legacy capture has no retained national source bytes")

    def source_from_legacy(self, snapshot_id):
        """Materialise a pinned legacy source locally without changing current source pointers."""
        acquisition, resource, path = self.inspect_legacy(snapshot_id)
        request = deprivation.national_request()
        manifest = self.store._publish(
            request,
            hashlib.sha256(canonical(request.to_dict())).hexdigest(),
            acquisition,
            [resource],
            kind="source",
            raw_paths={0: path},
            index_request=False,
        )
        self.registry.register(self.store, manifest, preserve_current=True)
        self.registry.check(
            self.key, "local_import", manifest["product_id"], provider_contact=False
        )
        return manifest

    def fetch(self, request, *, refresh=False, offline=False, client=None, source_snapshot_id=None):
        if source_snapshot_id is not None and (refresh or not offline):
            raise ValueError("Explicit source selections must be offline and cannot refresh")
        if request == deprivation.national_request():
            if source_snapshot_id is not None:
                raise ValueError("Explicit source is only for local authority selections")
            return self.source(refresh=refresh, offline=offline, client=client)
        codes = deprivation._selectors(request)
        source = (
            self.store.verify(source_snapshot_id)
            if source_snapshot_id is not None
            else self.source(refresh=refresh, offline=offline, client=client)
        )
        self._validate_source(source)
        frame = self.store.replay(source["product_id"])
        missing = sorted(set(codes) - set(frame["local_authority_code"]))
        if missing:
            raise RetrievalError(
                f"LAD codes absent from the IoD 2025 release: {', '.join(missing)}"
            )
        frame = frame.loc[frame["local_authority_code"].isin(codes)].copy().reset_index(drop=True)
        if len(frame) > request.max_rows:
            raise RetrievalError(
                f"Complete LAD selection has {len(frame)} rows, exceeding max_rows={request.max_rows}"
            )
        metadata = json.loads(
            (self.store.path(source["product_id"]) / "metadata.json").read_bytes()
        )
        metadata.update(
            local_authority_codes=codes,
            product_rows=len(frame),
            product_scope="All LSOAs in the requested Local Authority Districts",
        )
        acquisition = Acquisition(
            "deprivation",
            deprivation.DATASET_ID,
            source["title"],
            frame,
            metadata,
            source["release"],
            source["licence"],
            source["citation"],
            source["source_url"],
        )
        from ssdu.normalization import normalize

        acquisition.dataset = normalize(
            acquisition,
            request=request,
            raw_frame=self.store._normalization_raw_frame(source),
        )
        key = hashlib.sha256(canonical(request.to_dict())).hexdigest()
        manifest = self.store._publish(
            request, key, acquisition, [], kind="selection", parents=[source["product_id"]]
        )
        self.registry.register(self.store, manifest)
        return manifest
