"""Verified, resumable HTTP evidence shared by discovery and acquisition."""

import fcntl
import hashlib
import json as jsonlib
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from ssdu.http import HttpClient, Resource
from ssdu.models import RetrievalError


def _canonical(value):
    return jsonlib.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _atomic(path, body):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".capture-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class CachedClient:
    """Cache successful responses, with explicit refresh and no-network replay.

    Repeated reads within one acquisition still reach the provider online: adapters
    use those reads to detect a release changing during pagination. Durable workers
    put their positional checkpoint client outside this cache to preserve that order.
    """

    def __init__(
        self,
        root,
        *,
        client=None,
        offline=False,
        refresh=False,
        allowed_hosts=None,
        close_client=False,
        clock=None,
    ):
        if offline and refresh:
            raise ValueError("Offline requests cannot refresh providers")
        self.root = Path(root) / "connector-cache"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        self.client = client
        self.offline, self.refresh = offline, refresh
        self.allowed_hosts = frozenset(allowed_hosts or HttpClient.HOSTS)
        self.resources = []
        self.close_client = close_client
        self.seen = set()
        self.cache_scope = None
        self._owned = False
        self.clock = clock or time.time

    persistent_cache = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._owned or (self.close_client and self.client is not None):
            self.client.__exit__(*args)

    def get(self, url, params=None):
        return self.request("GET", url, params=params)

    def post(self, url, *, json):
        return self.request("POST", url, json=json)

    @contextmanager
    def scope(self, token):
        if not isinstance(token, str) or not token or len(token) > 500:
            raise ValueError("Cache scopes require a bounded resolved release identity")
        previous, self.cache_scope = self.cache_scope, token
        try:
            yield self
        finally:
            self.cache_scope = previous

    def mark_replayed(self, resource):
        """Remember a positional worker checkpoint for subsequent revision checks."""
        identity = {
            "method": resource.method,
            "url": resource.url,
            "request_body": resource.request_body,
            "cache_scope": resource.cache_scope,
        }
        self.seen.add(hashlib.sha256(_canonical(identity)).hexdigest())

    def _read_capture(self, path, identity):
        try:
            document = jsonlib.loads(path.read_bytes())
            capture_id = document.pop("capture_id")
            digest = document["body_sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Invalid response digest")
            raw = self.root / "objects" / f"{digest}.bin"
            if raw.stat().st_size > 25_000_000:
                raise ValueError("Cached response exceeds byte budget")
            body = raw.read_bytes()
            if (
                capture_id != hashlib.sha256(_canonical(document)).hexdigest()
                or document["request"] != identity
                or document["body_sha256"] != hashlib.sha256(body).hexdigest()
            ):
                raise ValueError("Capture hash or request mismatch")
            return Resource(
                body,
                document["url"],
                identity["method"],
                identity["request_body"],
                document["content_type"],
                document["headers"],
                identity["cache_scope"],
                captured_at=document.get("captured_at"),
                checked_at=document.get("checked_at"),
                cache_hit=True,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RetrievalError("Connector cache verification failed") from exc

    def request(self, method, url, *, params=None, json=None):
        full_url = str(httpx.Request(method, url, params=params).url)
        parsed = urlparse(full_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise RetrievalError("Provider URL is outside the connector allowlist")
        identity = {
            "method": method,
            "url": full_url,
            "request_body": json,
            "cache_scope": self.cache_scope,
        }
        key = hashlib.sha256(_canonical(identity)).hexdigest()
        with (self.root / f"{key}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / f"{key}.json"
            reuse = path.exists() and (self.offline or (not self.refresh and key not in self.seen))
            if reuse:
                resource = self._read_capture(path, identity)
            else:
                if self.offline:
                    raise RetrievalError(
                        "No retained connector response for this exact request; prepare this metadata or selection online once"
                    )
                if self.client is None:
                    self.client = HttpClient(allowed_hosts=self.allowed_hosts)
                    self._owned = True
                prior = None
                validators = {}
                if (
                    method == "GET"
                    and path.exists()
                    and getattr(self.client, "conditional_requests", False)
                ):
                    prior = self._read_capture(path, identity)
                    if prior.headers.get("etag"):
                        validators["If-None-Match"] = prior.headers["etag"]
                    elif prior.headers.get("last-modified"):
                        validators["If-Modified-Since"] = prior.headers["last-modified"]
                options = {"headers": validators} if validators else {}
                resource = self.client.request(method, url, params=params, json=json, **options)
                checked = datetime.fromtimestamp(self.clock(), UTC).isoformat()
                if resource.status_code == 304:
                    if prior is None or not validators:
                        raise RetrievalError(
                            "Provider returned 304 without verified prior evidence"
                        )
                    resource = replace(
                        prior,
                        headers={**prior.headers, **resource.headers},
                        checked_at=checked,
                        cache_hit=False,
                        revalidated=True,
                    )
                else:
                    resource = replace(
                        resource,
                        cache_scope=self.cache_scope,
                        captured_at=checked,
                        checked_at=checked,
                        cache_hit=False,
                    )
                document = {
                    "request": identity,
                    "body_sha256": hashlib.sha256(resource.body).hexdigest(),
                    "url": resource.url,
                    "content_type": resource.content_type,
                    "headers": resource.headers,
                    "captured_at": resource.captured_at,
                    "checked_at": resource.checked_at,
                }
                document["capture_id"] = hashlib.sha256(_canonical(document)).hexdigest()
                raw = self.root / "objects" / f"{document['body_sha256']}.bin"
                _atomic(raw, resource.body)
                _atomic(path, _canonical(document))
        self.seen.add(key)
        self.resources.append(resource)
        return resource


def _retained_body(store, manifest, record):
    """Never promote a historical resource absent from the hashed file inventory."""
    relative = record["file"]
    if not isinstance(relative, str):
        raise RetrievalError("Invalid retained resource path")
    part = PurePosixPath(relative)
    if (
        part.is_absolute()
        or str(part) != relative
        or ".." in part.parts
        or "\\" in relative
        or not part.parts
    ):
        raise RetrievalError("Unsafe retained resource path")
    base = store.path(manifest["product_id"])
    path = base / relative
    integrity = manifest["files"].get(relative)
    if (
        not isinstance(integrity, dict)
        or not path.resolve().is_relative_to(base)
        or any(item.is_symlink() for item in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size > 25_000_000
    ):
        raise RetrievalError("Retained resource is missing from the verified file inventory")
    body = path.read_bytes()
    if (
        integrity.get("bytes") != len(body)
        or integrity.get("sha256") != hashlib.sha256(body).hexdigest()
    ):
        raise RetrievalError("Retained resource differs from its verified file inventory")
    return body


def seed_from_snapshots(store, *, provider=None):
    """Recover optional response cache from independently verified snapshot evidence.

    Latest valid captures win. Unrelated damaged captures are isolated and reported
    in the return value; their bytes never enter the response cache. Catalogue pages
    absent from a portable bundle remain unavailable.
    """
    manifests, scan_errors = store._scan_manifests()
    root = store.root / "connector-cache"
    root.mkdir(exist_ok=True)
    (root / "objects").mkdir(exist_ok=True)
    report = {"seeded": 0, "skipped_snapshots": list(scan_errors)}
    for manifest in sorted(
        manifests, key=lambda m: (m["created_at"], m["product_id"]), reverse=True
    ):
        if provider is not None and manifest.get("provider") != provider:
            continue
        verified = None
        try:
            for record in manifest["resources"]:
                if record["method"] not in {"GET", "POST"}:
                    continue
                identity = {
                    "method": record["method"],
                    "url": record["url"],
                    "request_body": record["request_body"],
                    "cache_scope": record.get("cache_scope"),
                }
                key = hashlib.sha256(_canonical(identity)).hexdigest()
                path = root / f"{key}.json"
                if path.exists():
                    continue
                if verified is None:
                    verified = store.verify(manifest["product_id"])
                if record not in verified["resources"]:
                    raise RetrievalError("Retained resource differs from its verified manifest")
                body = _retained_body(store, verified, record)
                document = {
                    "request": identity,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "url": record["url"],
                    "content_type": record["content_type"],
                    "headers": record["response_headers"],
                }
                document["capture_id"] = hashlib.sha256(_canonical(document)).hexdigest()
                with (root / f"{key}.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if not path.exists():
                        _atomic(root / "objects" / f"{document['body_sha256']}.bin", body)
                        _atomic(path, _canonical(document))
                        report["seeded"] += 1
        except (RetrievalError, OSError, KeyError, TypeError, ValueError) as exc:
            report["skipped_snapshots"].append(
                {"product_id": manifest["product_id"], "error": str(exc)}
            )
    return report
