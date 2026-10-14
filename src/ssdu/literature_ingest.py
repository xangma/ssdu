"""Bounded, cached public literature acquisition; reads and transfer remain offline."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlparse

from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.store import canonical

HOSTS = frozenset({"raw.githubusercontent.com", "api.openalex.org"})
RICH_CONTEXT_REVISION = "4d5347d8d1ac2693901966d6dd6905ba14133f89"
MAX_BYTES = 12_000_000
MAX_PAPERS = 100
SHA = re.compile(r"[0-9a-f]{64}")


def _atomic(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".capture-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _immutable(path: Path, raw: bytes):
    if path.exists():
        if path.read_bytes() != raw:
            raise RetrievalError(f"Corrupt retained literature response: {path.name}")
        return
    _atomic(path, raw)


@dataclass(frozen=True)
class Capture:
    body: bytes
    metadata: dict
    cache_hit: bool

    def resources(self, name: str) -> dict[str, bytes]:
        return {name: self.body, f"{name}.capture.json": canonical(self.metadata) + b"\n"}


class LiteratureClient:
    """One shared rate ledger, one host slot and immutable cache entries per response.

    The cache is checkpointed after every successful GET. Interrupted enrichment
    resumes from those verified entries; explicit refresh preserves past captures.
    No credentials or unrestricted source URLs are accepted.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        client: HttpClient | None = None,
        allowed_hosts=HOSTS,
        cache_namespace="literature",
        max_requests=None,
        max_total_bytes=None,
    ):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", cache_namespace):
            raise ValueError("Invalid cache namespace")
        for budget in (max_requests, max_total_bytes):
            if budget is not None and (type(budget) is not int or budget < 1):
                raise ValueError("Capture budgets must be positive integers")
        self.allowed_hosts = frozenset(allowed_hosts)
        self.max_requests = max_requests
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self.root = Path(root).resolve() / cache_namespace / "http"
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.attempts = 0
        self.cache_hits = 0

    def get(self, url: str, *, params=None, offline=False, refresh=False) -> Capture:
        if offline and refresh:
            raise ValueError("Offline acquisition and refresh cannot be combined")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise RetrievalError("Literature URL is outside the bounded public-source allowlist")
        parameters = {} if params is None else params
        if not isinstance(parameters, dict) or any(
            not isinstance(k, str) or not isinstance(v, (str, int)) for k, v in parameters.items()
        ):
            raise ValueError("Literature query parameters must be string/integer pairs")
        if parsed.query:
            # httpx replaces a URL's query when passed params={}, so retain it
            # explicitly in both transport and cache identity. Old query-bearing
            # captures with dropped parameters are not silently reused.
            url_parameters = parse_qsl(parsed.query, keep_blank_values=True)
            if len({key for key, _ in url_parameters}) != len(url_parameters):
                raise ValueError("Duplicate URL query parameters are unsupported")
            parameters = {**dict(url_parameters), **parameters}
        sensitive = {"api_key", "key", "token", "access_token", "authorization"}
        if sensitive & {k.lower() for k in {*parameters, *parse_qs(parsed.query)}}:
            raise ValueError("This bounded public metadata client does not accept credentials")
        request = {"url": url, "params": parameters}
        request_id = hashlib.sha256(canonical(request)).hexdigest()
        request_dir = self.root / "requests" / request_id
        request_dir.mkdir(parents=True, exist_ok=True)
        with (request_dir / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = request_dir / "current.json"
            if current.exists() and not refresh:
                result = self._cached(current, request)
                self.cache_hits += 1
                return result
            if offline:
                raise RetrievalError(f"Offline literature cache miss: {url}")
            self.before_request(parsed.hostname)
            remaining_requests = (
                3 if self.max_requests is None else self.max_requests - self.attempts
            )
            remaining_bytes = (
                MAX_BYTES
                if self.max_total_bytes is None
                else self.max_total_bytes - self.total_bytes
            )
            if remaining_requests <= 0 or remaining_bytes <= 0:
                raise RetrievalError("Metadata acquisition request/download budget exhausted")
            if self.client is None:
                with HttpClient(
                    allowed_hosts=self.allowed_hosts,
                    max_bytes=MAX_BYTES,
                    max_requests=min(3, remaining_requests),
                    max_total_bytes=min(MAX_BYTES, remaining_bytes),
                ) as client:
                    before = client.attempts
                    before_bytes = client.total_bytes
                    try:
                        response = client.get(url, params=parameters)
                    finally:
                        self.attempts += client.attempts - before
                        self.total_bytes += client.total_bytes - before_bytes
            else:
                before = self.client.attempts
                before_bytes = self.client.total_bytes
                old_requests, old_bytes = self.client.max_requests, self.client.max_total_bytes
                self.client.max_requests = min(old_requests, before + remaining_requests)
                self.client.max_total_bytes = min(old_bytes, before_bytes + remaining_bytes)
                try:
                    response = self.client.get(url, params=parameters)
                finally:
                    self.attempts += self.client.attempts - before
                    self.total_bytes += self.client.total_bytes - before_bytes
                    self.client.max_requests, self.client.max_total_bytes = old_requests, old_bytes
            body = response.body
            if len(body) > MAX_BYTES:
                raise RetrievalError("Literature response exceeds size budget")
            digest = hashlib.sha256(body).hexdigest()
            identity = {
                "schema_version": 1,
                "request": request,
                "url": response.url,
                "body_sha256": digest,
                "bytes": len(body),
                "headers": response.headers,
            }
            capture_id = hashlib.sha256(canonical(identity)).hexdigest()
            captures = self.root / "captures"
            captures.mkdir(exist_ok=True)
            _immutable(captures / f"{digest}.body", body)
            metadata_file = captures / f"{capture_id}.json"
            if metadata_file.exists():
                metadata = self._read_metadata(metadata_file, capture_id)
            else:
                metadata = {
                    **identity,
                    "capture_id": capture_id,
                    "captured_at": datetime.now(UTC).isoformat(),
                }
                _immutable(metadata_file, canonical(metadata) + b"\n")
            _atomic(current, canonical({"capture_id": capture_id}) + b"\n")
            return Capture(body, metadata, False)

    def before_request(self, host):
        """Hook for a bounded metadata harvester's observed quota policy."""

    def _read_metadata(self, path, capture_id):
        try:
            if path.stat().st_size > 64_000:
                raise ValueError("Capture metadata exceeds size budget")
            metadata = json.loads(path.read_bytes())
            if not isinstance(metadata, dict):
                raise TypeError("Capture metadata must be an object")
            identity = {k: v for k, v in metadata.items() if k not in {"capture_id", "captured_at"}}
            if (
                metadata.get("capture_id") != capture_id
                or hashlib.sha256(canonical(identity)).hexdigest() != capture_id
                or metadata.get("schema_version") != 1
            ):
                raise ValueError("Capture identity mismatch")
            return metadata
        except (ValueError, TypeError, OSError) as exc:
            raise RetrievalError("Invalid literature capture metadata") from exc

    def _cached(self, pointer, request):
        try:
            if pointer.stat().st_size > 1_000:
                raise ValueError("Pointer exceeds size budget")
            capture_id = json.loads(pointer.read_bytes())["capture_id"]
            if not isinstance(capture_id, str) or not SHA.fullmatch(capture_id):
                raise ValueError("Invalid capture identifier")
            metadata = self._read_metadata(
                self.root / "captures" / f"{capture_id}.json", capture_id
            )
            digest = metadata["body_sha256"]
            if (
                not isinstance(digest, str)
                or not SHA.fullmatch(digest)
                or metadata["request"] != request
            ):
                raise ValueError("Capture request mismatch")
            path = self.root / "captures" / f"{digest}.body"
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("Response exceeds size budget")
            body = path.read_bytes()
            if len(body) != metadata["bytes"] or hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("Response hash mismatch")
            return Capture(body, metadata, True)
        except (ValueError, TypeError, OSError, KeyError) as exc:
            raise RetrievalError(
                "Invalid retained literature response; explicit recovery required"
            ) from exc


def _merge(left: dict, right: dict) -> dict:
    result = {**left}
    for collection in ("sources", "papers", "datasets", "relationships", "providers"):
        merged = {row["id"]: row for row in left.get(collection, [])}
        for row in right.get(collection, []):
            if row["id"] in merged and merged[row["id"]] != row:
                raise ValueError(f"Conflicting literature record: {row['id']}")
            merged[row["id"]] = row
        result[collection] = [merged[key] for key in sorted(merged)]
    return result


def capture_literature(
    root: str | Path,
    plan: dict,
    *,
    seed_document: dict | None = None,
    seed_resources: dict[str, bytes] | None = None,
    offline=False,
    refresh=False,
    client: LiteratureClient | None = None,
) -> dict:
    """Capture a bounded plan, publish its raw closure and return a reusable snapshot."""
    from ssdu.literature_sources import enrich_openalex, normalize_doi, parse_rich_context
    from ssdu.literature_store import LiteratureStore

    if offline and refresh:
        raise ValueError("Offline acquisition and refresh cannot be combined")
    if (
        not isinstance(plan, dict)
        or type(plan.get("schema_version")) is not int
        or plan.get("schema_version") != 1
    ):
        raise ValueError("Unsupported literature plan")
    spec = plan.get("rich_context", {})
    enrichment = plan.get("openalex", {})
    if not isinstance(spec, dict) or not isinstance(enrichment, dict):
        raise TypeError("Literature source plans must be JSON objects")
    revision = spec.get("revision", RICH_CONTEXT_REVISION)
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Rich Context requires an immutable 40-character commit revision")
    count = spec.get("max_papers", 20)
    enrichment_limit = enrichment.get("max_papers", 0)
    if (
        any(type(n) is not int or not 0 <= n <= MAX_PAPERS for n in (count, enrichment_limit))
        or count == 0
    ):
        raise ValueError(
            "Literature paper/enrichment budgets must be bounded integers (1–100 papers)"
        )
    identifiers = spec.get("publication_ids")
    if identifiers is not None and (
        not isinstance(identifiers, list)
        or len(identifiers) > count
        or any(not isinstance(key, str) or not key.strip() for key in identifiers)
    ):
        raise ValueError("Publication identifiers must be a list within the paper budget")
    if seed_document is not None:
        from ssdu.literature_store import validate_document

        validate_document(seed_document, (seed_resources or {}).keys())
    transport = client or LiteratureClient(root)
    source_url = (
        f"https://raw.githubusercontent.com/Coleridge-Data-For-Impact/rclc/{revision}/corpus.jsonld"
    )
    source = transport.get(source_url, offline=offline, refresh=refresh)
    resources = source.resources("rich-context.jsonld")
    licence_url = (
        f"https://raw.githubusercontent.com/Coleridge-Data-For-Impact/rclc/{revision}/LICENSE"
    )
    licence = transport.get(licence_url, offline=offline, refresh=refresh)
    if b"CC0" not in licence.body or b"Universal" not in licence.body:
        raise RetrievalError("Unexpected Rich Context repository licence; inspect before use")
    resources.update(licence.resources("rich-context-LICENSE.txt"))
    resources["capture-plan.json"] = canonical(plan) + b"\n"
    document = parse_rich_context(
        source.body,
        source_url=source_url,
        revision=revision,
        publication_ids=spec.get("publication_ids"),
        max_papers=count,
        resource_path="rich-context.jsonld",
    )
    corpus_source = next(s for s in document["sources"] if s["id"] == f"rich-context:{revision}")
    corpus_source["licence"] = "CC0-1.0 (retained repository LICENSE)"
    corpus_source["resource_paths"].append("rich-context-LICENSE.txt")
    document["ingestion"] = {
        "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
        "plan": plan,
        "independent_human_review": False,
    }
    if seed_document is not None:
        document = _merge(document, seed_document)
    for name, raw in (seed_resources or {}).items():
        if name in resources:
            raise ValueError("Seed resource collides with acquired resource")
        resources[name] = raw
    dois = sorted({normalize_doi(p["doi"]) for p in document["papers"] if p.get("doi")})
    if len(dois) > enrichment_limit:
        dois = dois[:enrichment_limit]
    statuses = []
    for doi in dois:
        url = f"https://api.openalex.org/works/https://doi.org/{doi}"
        # Missing works produce a verified empty results list, rather than a 404.
        params = {
            "filter": f"doi:{doi}",
            "per_page": 2,
            "select": "id,doi,title,publication_date,abstract_inverted_index",
        }
        capture = transport.get(
            "https://api.openalex.org/works", params=params, offline=offline, refresh=refresh
        )
        payload = json.loads(capture.body)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RetrievalError("Unexpected OpenAlex lookup response")
        works = payload["results"]
        if len(works) > 1:
            raise RetrievalError(f"Ambiguous OpenAlex DOI lookup: {doi}")
        if works and normalize_doi(works[0].get("doi")) != doi:
            raise RetrievalError("OpenAlex lookup returned a different DOI")
        name = f"openalex/{hashlib.sha256(doi.encode()).hexdigest()}.json"
        resources.update(capture.resources(name))
        document = enrich_openalex(
            document,
            works,
            source_url=capture.metadata["url"],
            resource_path=name,
        )
        statuses.append(
            {
                "doi": doi,
                "status": "matched" if works else "not_found",
                "resource": name,
                "record_url": url,
            }
        )
    store = LiteratureStore(root)
    snapshot_id = store.publish(document, resources)
    result = {
        "snapshot_id": snapshot_id,
        "counts": store.verify(snapshot_id)["counts"],
        "openalex": statuses,
        "provider_attempts": transport.attempts,
        "cache_hits": transport.cache_hits,
        "offline": offline,
        "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
    }
    _atomic(Path(root).resolve() / "literature" / "last-capture.json", canonical(result) + b"\n")
    return result
