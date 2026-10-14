"""Cached UK data-source literature candidates, with literal evidence and no use claims.

OpenAlex search can match text unavailable in the returned title or abstract. Such
hits remain unlinked candidates. Literal matches establish a mention, never actual
dataset use, a specific release, or geographic suitability.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from ssdu.literature_sources import LiteratureSourceError, normalize_doi, reconstruct_abstract
from ssdu.literature_store import LiteratureStore
from ssdu.models import RetrievalError
from ssdu.store import canonical

OPENALEX = "https://api.openalex.org/works"
SELECT = "id,doi,title,publication_date,abstract_inverted_index"
MAX_QUERIES = 30
MAX_UNIQUE_PAPERS = 10_000
MAX_REQUESTS = 300
MAX_TOTAL_BYTES = 200_000_000
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
WORK_ID = re.compile(r"https://openalex\.org/W[0-9]+")


def default_plan() -> dict:
    """Return an explicit, bounded pilot plan; its searches are not a census of UK use."""
    queries = [
        {
            "id": "nomis",
            "text": '"Nomis"',
            "aliases": ["Nomis"],
            "family_id": "uk-family:nomis",
            "family_title": "Nomis census and labour market statistics",
            "provider": "nomis",
            "source_url": "https://www.nomisweb.co.uk/",
        },
        {
            "id": "statswales",
            "text": '"StatsWales"',
            "aliases": ["StatsWales", "Stats Wales"],
            "family_id": "uk-family:statswales",
            "family_title": "StatsWales statistical data",
            "provider": "welsh_government",
            "source_url": "https://statswales.gov.wales/",
        },
        {
            "id": "english_imd",
            "text": '"English Index of Multiple Deprivation"',
            "aliases": ["English Index of Multiple Deprivation"],
            "family_id": "uk-family:english-deprivation",
            "family_title": "English indices of deprivation",
            "provider": "mhclg",
            "source_url": "https://www.gov.uk/government/collections/english-indices-of-deprivation",
        },
        {
            "id": "english_iod",
            "text": '"English Indices of Deprivation"',
            "aliases": ["English Indices of Deprivation"],
            "family_id": "uk-family:english-deprivation",
            "family_title": "English indices of deprivation",
            "provider": "mhclg",
            "source_url": "https://www.gov.uk/government/collections/english-indices-of-deprivation",
        },
        {
            "id": "welsh_wimd",
            "text": '"Welsh Index of Multiple Deprivation"',
            "aliases": ["Welsh Index of Multiple Deprivation"],
            "family_id": "uk-family:welsh-deprivation",
            "family_title": "Welsh Index of Multiple Deprivation",
            "provider": "welsh_government",
            "source_url": "https://www.gov.wales/welsh-index-multiple-deprivation",
        },
        {
            "id": "wimd",
            "text": '"WIMD"',
            "aliases": ["WIMD"],
            "family_id": "uk-family:welsh-deprivation",
            "family_title": "Welsh Index of Multiple Deprivation",
            "provider": "welsh_government",
            "source_url": "https://www.gov.wales/welsh-index-multiple-deprivation",
        },
    ]
    return {
        "schema_version": 1,
        "queries": queries,
        "page_size": 100,
        "max_pages_per_query": 3,
        "max_unique_papers": 2000,
        "max_requests": 30,
        "max_total_bytes": 64_000_000,
    }


def _bounded_int(plan, name, default, maximum):
    value = plan.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _validate_plan(plan):
    if (
        not isinstance(plan, dict)
        or type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 1
    ):
        raise ValueError("Unsupported UK literature harvest plan")
    queries = plan.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
        raise ValueError(f"queries must contain between 1 and {MAX_QUERIES} source searches")
    query_ids, families = set(), {}
    for query in queries:
        if not isinstance(query, dict):
            raise TypeError("Each query must be an object")
        identifier = query.get("id")
        if not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier):
            raise ValueError("Query id must be a safe short identifier")
        if identifier in query_ids:
            raise ValueError("Duplicate query id")
        query_ids.add(identifier)
        for key in ("text", "family_id", "family_title", "provider", "source_url"):
            value = query.get(key)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 2000
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(f"Query {key} must be bounded nonempty text")
        aliases = query.get("aliases")
        if (
            not isinstance(aliases, list)
            or not 1 <= len(aliases) <= 20
            or any(
                not isinstance(alias, str)
                or not alias.strip()
                or alias != alias.strip()
                or len(alias) > 200
                or any(ord(character) < 32 for character in alias)
                for alias in aliases
            )
            or len({alias.casefold() for alias in aliases}) != len(aliases)
        ):
            raise ValueError("aliases must be unique bounded literal strings")
        try:
            url = urlsplit(query["source_url"])
            valid_url = (
                url.scheme in {"https", "http"}
                and url.hostname
                and url.username is None
                and url.password is None
                and url.port is None
                and not any(character.isspace() for character in query["source_url"])
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("Family source_url must be a public HTTP(S) reference")
        family = {key: query[key] for key in ("family_title", "provider", "source_url")}
        previous = families.setdefault(query["family_id"], family)
        if previous != family:
            raise ValueError("Queries disagree on their shared dataset family")
    bounds = {
        "page_size": _bounded_int(plan, "page_size", 100, 100),
        "max_pages_per_query": _bounded_int(plan, "max_pages_per_query", 3, 20),
        "max_unique_papers": _bounded_int(plan, "max_unique_papers", 2000, MAX_UNIQUE_PAPERS),
        "max_requests": _bounded_int(plan, "max_requests", 30, MAX_REQUESTS),
        "max_total_bytes": _bounded_int(plan, "max_total_bytes", 64_000_000, MAX_TOTAL_BYTES),
    }
    return copy.deepcopy(queries), families, bounds


def _ref(source):
    return {key: source[key] for key in ("id", "title", "url", "type", "published_at")}


def _refs(ids, sources):
    return [_ref(sources[identifier]) for identifier in sorted(set(ids))]


def _page(capture, page_size):
    try:
        page = json.loads(capture.body, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RetrievalError("Invalid OpenAlex search page JSON") from exc
    if not isinstance(page, dict) or not isinstance(page.get("results"), list):
        raise RetrievalError("OpenAlex search page requires results and meta")
    meta = page.get("meta")
    if (
        not isinstance(meta, dict)
        or type(meta.get("count")) is not int
        or meta["count"] < 0
        or "next_cursor" not in meta
        or len(page["results"]) > page_size
    ):
        raise RetrievalError("Invalid OpenAlex search page shape")
    cursor = meta["next_cursor"]
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 16_384):
        raise RetrievalError("Invalid OpenAlex search cursor")
    return page["results"], meta["count"], cursor


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _work(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise TypeError("Work must be an object with an OpenAlex work ID")
    if not WORK_ID.fullmatch(raw["id"]):
        raise ValueError("Invalid OpenAlex work ID")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 20_000:
        raise ValueError("Work title is missing, invalid or excessive")
    issues = []
    try:
        doi = normalize_doi(raw.get("doi"))
    except LiteratureSourceError as exc:
        doi = None
        issues.append({"field": "doi", "reason": str(exc)[:1000]})
    published_at = raw.get("publication_date")
    if published_at is not None:
        try:
            if (
                not isinstance(published_at, str)
                or date.fromisoformat(published_at).isoformat() != published_at
            ):
                raise ValueError
        except ValueError:
            published_at = None
            issues.append(
                {"field": "published_at", "reason": "Invalid publication_date; retained as null"}
            )
    try:
        abstract = reconstruct_abstract(raw.get("abstract_inverted_index"))
    except LiteratureSourceError as exc:
        abstract = None
        issues.append({"field": "abstract", "reason": str(exc)})
    return {
        "id": "openalex:" + raw["id"].rsplit("/", 1)[1],
        "openalex_id": raw["id"],
        "title": title,
        "doi": doi,
        "published_at": published_at,
        "abstract": abstract,
        "issues": issues,
    }


class _Identities:
    """Union duplicate work IDs and exact normalized DOIs without fuzzy title matching."""

    def __init__(self, maximum):
        self.maximum = maximum
        self.parents = {}
        self.dois = {}
        self.count = 0
        self.observations = []

    def root(self, identifier):
        parent = self.parents[identifier]
        if parent != identifier:
            self.parents[identifier] = self.root(parent)
        return self.parents[identifier]

    def add(self, work):
        identifier, doi = work["id"], work["doi"]
        known_doi = self.dois.get(doi) if doi else None
        if identifier not in self.parents:
            if known_doi is None:
                if self.count >= self.maximum:
                    return False
                self.parents[identifier] = identifier
                self.count += 1
            else:
                self.parents[identifier] = self.root(known_doi)
        if known_doi is not None:
            roots = sorted({self.root(identifier), self.root(known_doi)})
            if len(roots) == 2:
                self.parents[roots[1]] = roots[0]
                self.count -= 1
        if doi:
            self.dois.setdefault(doi, identifier)
        self.observations.append(work)
        return True


def _matches(work, query):
    matches = []
    for field in ("title", "abstract"):
        text = work[field]
        if text is None:
            continue
        for alias in query["aliases"]:
            left = r"(?<!\w)" if alias[0].isalnum() or alias[0] == "_" else ""
            right = r"(?!\w)" if alias[-1].isalnum() or alias[-1] == "_" else ""
            pattern = re.compile(left + re.escape(alias) + right, re.IGNORECASE)
            for match in pattern.finditer(text):
                start, end = match.span()
                snippet_start, snippet_end = max(0, start - 70), min(len(text), end + 70)
                matches.append(
                    {
                        "query_id": query["id"],
                        "record_id": work["openalex_id"],
                        "source_id": work["source_id"],
                        "location": work["location"]
                        + "/"
                        + ("abstract_inverted_index" if field == "abstract" else "title"),
                        "field": field,
                        "alias": alias,
                        "matched_text": text[start:end],
                        "span": {"start": start, "end": end, "unit": "Unicode code points"},
                        "snippet": text[snippet_start:snippet_end],
                        "snippet_span": {"start": snippet_start, "end": snippet_end},
                        "abstract_transform": "OpenAlex inverted index reconstructed in positional order"
                        if field == "abstract"
                        else None,
                    }
                )
                if len(matches) >= 12:
                    return matches
    return matches


def _build_records(identities, queries, sources):
    by_query = {query["id"]: query for query in queries}
    groups = {}
    for work in identities.observations:
        groups.setdefault(identities.root(work["id"]), []).append(work)
    papers, relationships = [], []
    query_papers = {query["id"]: set() for query in queries}
    query_links = {query["id"]: set() for query in queries}
    for _, observations in sorted(groups.items()):
        observations.sort(key=lambda row: (row["id"], row["source_id"], row["location"]))
        identifier = min(row["id"] for row in observations)
        values, provenance = {}, {}
        for field in ("title", "doi", "abstract", "published_at"):
            selected = next((row for row in observations if row[field] is not None), None)
            values[field] = selected[field] if selected is not None else None
            if selected is not None:
                provenance[field] = {
                    "source_id": selected["source_id"],
                    "record_id": selected["openalex_id"],
                    "location": selected["location"],
                    "references": _refs([selected["source_id"]], sources),
                }
        doi = values["doi"]
        publication_source = {
            "id": "reference:" + identifier,
            "title": values["title"],
            "url": "https://doi.org/" + doi
            if doi
            else min(row["openalex_id"] for row in observations),
            "type": "publication_reference",
            "published_at": values["published_at"],
        }
        sources[publication_source["id"]] = publication_source
        source_ids = {publication_source["id"], *(row["source_id"] for row in observations)}
        hits, issues, conflicts, family_matches = [], [], [], {}
        seen_hits = set()
        for work in observations:
            query = by_query[work["query_id"]]
            query_papers[query["id"]].add(identifier)
            hit_key = (query["id"], work["source_id"], work["location"])
            if hit_key not in seen_hits:
                seen_hits.add(hit_key)
                hits.append(
                    {
                        "query_id": query["id"],
                        "query_text": query["text"],
                        "source_id": work["source_id"],
                        "record_id": work["openalex_id"],
                        "location": work["location"],
                        "references": _refs([work["source_id"]], sources),
                    }
                )
            attribution = {
                "source_id": work["source_id"],
                "record_id": work["openalex_id"],
                "location": work["location"],
                "references": _refs([work["source_id"]], sources),
            }
            issues.extend({**issue, **attribution} for issue in work["issues"])
            for field, value in values.items():
                if work[field] is not None and work[field] != value:
                    conflicts.append({"field": field, "observed_value": work[field], **attribution})
            matches = _matches(work, query)
            if matches:
                for match in matches:
                    match["references"] = _refs([match["source_id"]], sources)
                family_matches.setdefault(query["family_id"], []).extend(matches)
                query_links[query["id"]].add(identifier)
        papers.append(
            {
                "id": identifier,
                **values,
                "url": publication_source["url"],
                "openalex_ids": sorted({row["openalex_id"] for row in observations}),
                "doi_aliases": sorted({row["doi"] for row in observations if row["doi"]}),
                "source_ids": sorted(source_ids),
                "references": _refs(source_ids, sources),
                "candidate_status": "literal_mention" if family_matches else "search_hit_unlinked",
                "query_hits": hits,
                "field_provenance": provenance,
                "metadata_issues": issues,
                "metadata_conflicts": conflicts,
                "uk_dataset_use_verified": False,
                "abstract_rights": "Upstream article rights may apply",
            }
        )
        for family_id, matches in sorted(family_matches.items()):
            primary = matches[0]
            evidence_source = sources[primary["source_id"]]
            related_sources = {publication_source["id"], *(match["source_id"] for match in matches)}
            relationships.append(
                {
                    "id": "mention:"
                    + hashlib.sha256(canonical([identifier, family_id])).hexdigest()[:24],
                    "paper_id": identifier,
                    "dataset_id": family_id,
                    "relationship_type": "inferred_mention",
                    "asserted_by": "SSDU literal metadata matcher",
                    "dataset_version": None,
                    "evidence": {
                        "source_id": primary["source_id"],
                        "location": primary["location"],
                        "url": evidence_source["url"],
                        "summary": f"Literal alias {primary['alias']!r} occurs in retained {primary['field']} metadata; dataset use and release identity are unverified.",
                        "literal_matches": matches,
                        "method": "case-insensitive literal match with word boundaries",
                    },
                    "review": {
                        "status": "pending",
                        "reviewer": None,
                        "method": "generated",
                        "independently_human_validated": False,
                    },
                    "references": _refs(related_sources, sources),
                }
            )
    return papers, relationships, query_papers, query_links


def _result(
    document, snapshot_id, plan_sha, *, attempts=0, cache_hits=0, offline=False, replayed=False
):
    return {
        "snapshot_id": snapshot_id,
        "counts": {
            key: len(document[key]) for key in ("papers", "datasets", "relationships", "sources")
        },
        "coverage": document["coverage"],
        "quarantine": document["quarantine"],
        "http_attempts": attempts,
        "cache_hits": cache_hits,
        "offline": bool(offline),
        "replayed_snapshot": replayed,
        "plan_sha256": plan_sha,
    }


def _completed(root, plan, plan_sha):
    path = Path(root).resolve() / "harvest" / "uk-literature" / f"{plan_sha}.json"
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 8192:
            raise ValueError("Completion pointer exceeds size budget")
        pointer = json.loads(path.read_bytes(), object_pairs_hook=_unique_fields)
        if (
            not isinstance(pointer, dict)
            or set(pointer) != {"schema_version", "plan_sha256", "snapshot_id"}
            or type(pointer.get("schema_version")) is not int
            or pointer["schema_version"] != 1
            or pointer.get("plan_sha256") != plan_sha
            or not isinstance(pointer.get("snapshot_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", pointer["snapshot_id"])
        ):
            raise ValueError("Completion pointer identity mismatch")
        document = LiteratureStore(root).read(pointer["snapshot_id"])
        ingestion = document.get("ingestion", {})
        if (
            ingestion.get("kind") != "targeted_uk_literature_candidates"
            or ingestion.get("plan_sha256") != plan_sha
            or canonical(ingestion.get("plan")) != canonical(plan)
            or not isinstance(document.get("coverage"), dict)
            or not isinstance(document.get("quarantine"), list)
        ):
            raise ValueError("Completed snapshot does not belong to this harvest plan")
        return _result(document, pointer["snapshot_id"], plan_sha, offline=True, replayed=True)
    except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
        raise RetrievalError("Invalid UK literature completion pointer or snapshot") from exc


def _quota_observation(capture, source):
    headers = capture.metadata.get("headers", {})
    if not isinstance(headers, dict):
        return None, False
    retained = {key: value for key, value in headers.items() if key.startswith("x-ratelimit-")}
    if not retained:
        return None, False
    exhausted = False
    try:
        remaining = float(retained.get("x-ratelimit-remaining", "nan"))
        exhausted = math.isfinite(remaining) and remaining <= 0 and not capture.cache_hit
    except (ValueError, TypeError):
        pass
    return {
        "source_id": source["id"],
        "headers": retained,
        "references": [_ref(source)],
    }, exhausted


def harvest_uk_literature(
    root: str | Path,
    plan: dict,
    *,
    client=None,
    offline=False,
    refresh=False,
) -> dict:
    """Publish a bounded candidate snapshot from cached, resumable cursor pages.

    Provider errors abort publication while successful page checkpoints survive.
    The page budget also applies offline, so cached replay has the same scope.
    Actual HTTP attempts (including retries) have a separate shared client budget.
    """
    if offline and refresh:
        raise ValueError("Offline acquisition and refresh cannot be combined")
    queries, families, bounds = _validate_plan(plan)
    plan_raw = canonical(plan) + b"\n"
    plan_sha = hashlib.sha256(plan_raw).hexdigest()
    if offline:
        completed = _completed(root, plan, plan_sha)
        if completed is not None:
            return completed
    if client is None:
        from ssdu.harvest import MetadataClient

        client = MetadataClient(
            root,
            max_requests=bounds["max_requests"],
            max_total_bytes=bounds["max_total_bytes"],
        )
    initial_attempts, initial_hits = client.attempts, client.cache_hits
    plan_source = {
        "id": "uk-literature-plan:" + plan_sha,
        "title": "SSDU targeted UK literature search plan",
        "url": "urn:sha256:" + plan_sha,
        "type": "operator_query_plan",
        "published_at": None,
        "resource_paths": ["uk-literature-plan.json"],
    }
    sources = {plan_source["id"]: plan_source}
    resources = {"uk-literature-plan.json": plan_raw}
    datasets = []
    for family_id, family in sorted(families.items()):
        source = {
            "id": "provider-reference:"
            + hashlib.sha256(family["source_url"].encode()).hexdigest()[:24],
            "title": family["family_title"] + " provider reference",
            "url": family["source_url"],
            "type": "provider_reference",
            "published_at": None,
            "acquisition": "Reference supplied by the search plan; not fetched by this harvest",
        }
        # Two operator labels for one URL should not make contradictory source records.
        previous = sources.setdefault(source["id"], source)
        source = previous
        ids = [plan_source["id"], source["id"]]
        datasets.append(
            {
                "id": family_id,
                "title": family["family_title"],
                "provider": family["provider"],
                "description": "Broad data family supplied by the search plan; candidate mentions do not identify a specific table or release.",
                "native_id": None,
                "doi": None,
                "version": None,
                "coverage": {},
                "identification_precision": "data_family",
                "retrieval_supported": False,
                "source_ids": ids,
                "references": _refs(ids, sources),
            }
        )
    identities = _Identities(bounds["max_unique_papers"])
    quarantine, coverage = [], []
    logical_requests, retained_bytes = 0, len(plan_raw)
    quota_exhausted = False
    for query in queries:
        row = {
            "query_id": query["id"],
            "query_text": query["text"],
            "family_id": query["family_id"],
            "pages": 0,
            "reported_counts": [],
            "returned_records": 0,
            "rejected_records": 0,
            "paper_budget_rejections": 0,
            "exhausted": False,
            "budget_truncated": False,
            "stopping_reason": None,
            "quota_observations": [],
            "references": [_ref(plan_source)],
        }
        page_sources = []
        cursor, seen_cursors = "*", set()
        for number in range(1, bounds["max_pages_per_query"] + 1):
            if logical_requests >= bounds["max_requests"]:
                row.update(budget_truncated=True, stopping_reason="request_budget")
                break
            if identities.count >= bounds["max_unique_papers"]:
                row.update(budget_truncated=True, stopping_reason="unique_paper_budget")
                break
            seen_cursors.add(cursor)
            parameters = {
                "search": query["text"],
                "per_page": bounds["page_size"],
                "cursor": cursor,
                "select": SELECT,
            }
            try:
                capture = client.get(
                    OPENALEX,
                    params=parameters,
                    offline=offline or quota_exhausted,
                    refresh=refresh and not quota_exhausted,
                )
            except RetrievalError as exc:
                if quota_exhausted and "Offline literature cache miss" in str(exc):
                    row.update(budget_truncated=True, stopping_reason="quota_exhausted")
                    break
                raise
            logical_requests += 1
            if client.attempts - initial_attempts > bounds["max_requests"]:
                raise RetrievalError("UK literature HTTP attempt budget exceeded")
            page_name = f"openalex-search/{query['id']}/page-{number:04d}.json"
            page_resources = capture.resources(page_name)
            retained_bytes += sum(len(raw) for raw in page_resources.values())
            if retained_bytes > bounds["max_total_bytes"]:
                raise RetrievalError("UK literature retained byte budget exceeded")
            resources.update(page_resources)
            source_id = (
                "openalex-search:"
                + hashlib.sha256(canonical([query["id"], parameters])).hexdigest()[:24]
            )
            source = {
                "id": source_id,
                "title": f"OpenAlex search {query['id']}: page {number}",
                "url": capture.metadata["url"],
                "type": "bibliographic_search_metadata",
                "published_at": None,
                "captured_at": capture.metadata.get("captured_at"),
                "resource_paths": sorted(page_resources),
                "query": parameters,
                "metadata_licence": "CC0",
                "licence_url": "https://help.openalex.org/api/",
                "abstract_rights": "Upstream article rights may apply",
            }
            sources[source_id] = source
            page_sources.append(source_id)
            observation, exhausted = _quota_observation(capture, source)
            if observation is not None:
                row["quota_observations"].append(observation)
            quota_exhausted = quota_exhausted or exhausted
            records, count, next_cursor = _page(capture, bounds["page_size"])
            row["pages"] += 1
            row["reported_counts"].append(count)
            row["returned_records"] += len(records)
            for index, raw in enumerate(records):
                location = f"/results/{index}"
                try:
                    work = _work(raw)
                    work.update(query_id=query["id"], source_id=source_id, location=location)
                    if not identities.add(work):
                        row["paper_budget_rejections"] += 1
                        reason = "unique_paper_budget"
                    else:
                        continue
                except (ValueError, TypeError) as exc:
                    reason = str(exc)
                    row["rejected_records"] += 1
                quarantine.append(
                    {
                        "id": "rejected:"
                        + hashlib.sha256(canonical([source_id, location])).hexdigest()[:24],
                        "query_id": query["id"],
                        "source_id": source_id,
                        "location": location,
                        "reason": reason,
                        "source_record_id": raw.get("id")
                        if isinstance(raw, dict)
                        and isinstance(raw.get("id"), str)
                        and len(raw["id"]) <= 2000
                        else None,
                        "references": [_ref(source)],
                    }
                )
            if next_cursor in seen_cursors:
                raise RetrievalError("OpenAlex cursor loop detected; snapshot publication aborted")
            if next_cursor is None or not records:
                row.update(exhausted=True, stopping_reason="exhausted")
                if next_cursor is not None:
                    row["empty_page_with_cursor"] = True
                break
            cursor = next_cursor
        else:
            row.update(budget_truncated=True, stopping_reason="page_budget")
        row["references"] = _refs([plan_source["id"], *page_sources], sources)
        coverage.append(row)
    papers, relationships, query_papers, query_links = _build_records(identities, queries, sources)
    paper_by_id = {paper["id"]: paper for paper in papers}
    for row in coverage:
        identifier = row["query_id"]
        ids = query_papers[identifier]
        row.update(
            unique_papers=len(ids),
            literal_mention_papers=len(query_links[identifier]),
            unlinked_candidates=len(ids - query_links[identifier]),
            papers_with_abstract=sum(paper_by_id[key]["abstract"] is not None for key in ids),
            duplicate_record_hits=row["returned_records"]
            - row["rejected_records"]
            - row["paper_budget_rejections"]
            - len(ids),
        )
    report = {
        "schema_version": 1,
        "queries": coverage,
        "papers": len(papers),
        "datasets": len(datasets),
        "relationships": len(relationships),
        "abstracts": sum(paper["abstract"] is not None for paper in papers),
        "unlinked_candidates": sum(
            paper["candidate_status"] == "search_hit_unlinked" for paper in papers
        ),
        "quarantined_records": len(quarantine),
        "records_with_metadata_issues": sum(bool(paper["metadata_issues"]) for paper in papers),
        "logical_pages": logical_requests,
        "independently_human_validated": False,
        "interpretation": "Search candidates and literal mentions; UK dataset use, release identity, and suitability remain unverified.",
        "references": [_ref(plan_source)],
    }
    resources["coverage.json"] = canonical(report) + b"\n"
    resources["quarantine.json"] = canonical({"schema_version": 1, "records": quarantine}) + b"\n"
    if sum(len(raw) for raw in resources.values()) > bounds["max_total_bytes"]:
        raise RetrievalError("UK literature retained byte budget exceeded")
    document = {
        "schema_version": 1,
        "sources": list(sources.values()),
        "papers": papers,
        "datasets": datasets,
        "relationships": relationships,
        "coverage": report,
        "quarantine": quarantine,
        "ingestion": {
            "kind": "targeted_uk_literature_candidates",
            "plan_sha256": plan_sha,
            "plan": copy.deepcopy(plan),
            "bounds": bounds,
            "literal_match_limit_per_observation": 12,
            "independently_human_validated": False,
            "restart": "Successful cursor-page responses are verified cache checkpoints; replay uses the same page and paper budgets.",
        },
    }
    snapshot_id = LiteratureStore(root).publish(document, resources)
    from ssdu.literature_ingest import _atomic

    _atomic(
        Path(root).resolve() / "harvest" / "uk-literature" / f"{plan_sha}.json",
        canonical({"schema_version": 1, "plan_sha256": plan_sha, "snapshot_id": snapshot_id})
        + b"\n",
    )
    return _result(
        document,
        snapshot_id,
        plan_sha,
        attempts=client.attempts - initial_attempts,
        cache_hits=client.cache_hits - initial_hits,
        offline=offline,
    )
