import hashlib
import json
import socket

import httpx
import numpy as np
import pytest

from ssdu.discovery_benchmark import BenchmarkBundle
from ssdu.discovery_enrichment import (
    EnrichmentError,
    _requests,
    build_enrichment_plan,
    capture_enrichment,
    derive_enrichment,
    validate_enrichment_plan,
)
from ssdu.harvest import METADATA_HOSTS, MetadataClient
from ssdu.http import HttpClient
from ssdu.literature_store import LiteratureStore
from ssdu.rate_limit import RateLimiter

UUID = "00000001-1234-4321-9876-123456789012"
REFERENCE = {"id": "base", "title": "Provider catalogue", "url": "https://example.org/catalogue"}


def _target(provider, native):
    return {
        "dataset_id": f"{provider}:{native}",
        "provider": provider,
        "native_id": native,
        "title": "Provider data",
        "base_metadata_sha256": "a" * 64,
        "base_coverage": {"release": "2019", "geography_vintages": ["2011"]},
        "question_ids": ["dev-1"],
        "reasons": ["lexical_top10_candidate"],
        "references": [REFERENCE],
        "requests": _requests(provider, native),
    }


@pytest.fixture
def plan():
    targets = [_target("nomis", "NM_1_1"), _target("statswales", UUID)]
    return {
        "schema_version": 1,
        "kind": "discovery_metadata_enrichment",
        "selection_split": "dev",
        "benchmark": {
            key: "b" * 64
            for key in ("manifest_sha256", "corpus_sha256", "questions_sha256", "embeddings_sha256")
        },
        "dataset_limit": 2,
        "max_requests": 5,
        "max_total_bytes": 1_000_000,
        "planned_requests": 5,
        "targets": targets,
        "unresolved": [],
    }


def _nomis(native="NM_1_1"):
    return {
        "structure": {
            "keyfamilies": {
                "keyfamily": {
                    "id": native,
                    "name": {"value": "Claimant count"},
                    "description": {"value": "Provider labour market statistics"},
                    "components": {
                        "dimension": {"conceptref": "GEOGRAPHY", "codelist": "CL_GEO"},
                        "timedimension": {"conceptref": "TIME", "codelist": "CL_TIME"},
                    },
                    "version": "2",
                    "annotations": {
                        "annotation": [
                            {"annotationtitle": "LastUpdated", "annotationtext": "2026-09-15"}
                        ]
                    },
                }
            }
        }
    }


def _codes(values):
    return {"structure": {"codelists": {"codelist": [{"code": values}]}}}


def _time():
    return _codes(
        [
            {"value": "2005-03", "description": {"value": "Apr 2004-Mar 2005"}},
            {"value": "2026-03", "description": {"value": "Apr 2025-Mar 2026"}},
        ]
    )


def _geography():
    return _codes(
        [
            {
                "value": 1,
                "description": {"value": "England"},
                "annotations": {
                    "annotation": [{"annotationtitle": "TypeName", "annotationtext": "countries"}]
                },
            },
            {
                "value": 2,
                "description": {"value": "Wales"},
                "annotations": {
                    "annotation": [{"annotationtitle": "TypeName", "annotationtext": "countries"}]
                },
            },
        ]
    )


def _published(native=UUID):
    return {
        "id": native,
        "published_revision": {
            "id": "revision-1",
            "revision_index": 3,
            "publish_at": "2026-01-15",
            "coverage_start_date": "1997-01-01",
            "coverage_end_date": "2025-12-31",
            "metadata": [{"language": "en-GB", "summary": "Observed provider description"}],
        },
    }


def _filters():
    return [
        {
            "columnName": "Area",
            "factTableColumn": "AreaID",
            "values": [
                {
                    "reference": "Wales",
                    "label": "Wales",
                    "children": [{"reference": "Cardiff", "label": "Cardiff"}],
                }
            ],
        },
        {
            "columnName": "Year",
            "factTableColumn": "YearID",
            "values": [{"reference": "2025", "label": "2025"}],
        },
    ]


def _handler(request):
    path = request.url.path
    if path.endswith("time.def.sdmx.json"):
        return httpx.Response(200, json=_time())
    if path.endswith("geography.def.sdmx.json"):
        return httpx.Response(200, json=_geography())
    if path.endswith("def.sdmx.json"):
        return httpx.Response(200, json=_nomis())
    if path.endswith("/filters"):
        return httpx.Response(200, json=_filters())
    assert path == "/v2/" + UUID
    assert request.url.params["lang"] == "en-gb"
    return httpx.Response(200, json=_published())


def _client(root, handler):
    http = HttpClient(
        transport=httpx.MockTransport(handler),
        allowed_hosts=METADATA_HOSTS,
        rate_limiter=RateLimiter(root / "rate.sqlite", interval_seconds=0),
        retries=0,
    )
    return http, MetadataClient(root, client=http)


def _run(root, plan, handler=_handler, **kwargs):
    http, client = _client(root, handler)
    with http:
        return capture_enrichment(root, plan, client=client, **kwargs)


def test_capture_metadata_only_and_reparse_every_field_from_raw(tmp_path, plan):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return _handler(request)

    captured = _run(tmp_path, plan, handler)
    assert captured["complete"] and captured["attempts"] == len(calls) == 5
    assert not any(".data" in url or "/query" in url for url in calls)
    derived = derive_enrichment(tmp_path, captured["snapshot_id"])
    nomis, wales = derived["updates"]
    assert nomis["fields"]["coverage"] == {
        "period_start": 2004,
        "period_end": 2026,
        "countries": ["England", "Wales"],
    }
    assert wales["fields"]["coverage"] == {"period_start": 1997, "period_end": 2025}
    assert wales["fields"]["variables"] == ["Area", "Year"]
    assert "countries" not in wales["fields"]["coverage"]
    assert nomis["provider_revision"]["annotations"]["LastUpdated"] == "2026-09-15"
    assert nomis["provider_revision"]["historical_revision_addressable"] is False
    assert nomis["fields"]["dimensions"][0]["scope_partial"] is True
    for update in derived["updates"]:
        assert update["references"] and update["base_metadata_sha256"] == "a" * 64
        assert (
            not {"places", "release", "geography_vintages", "geography_levels"}
            & update["fields"]["coverage"].keys()
        )
        assert "concepts" not in update["fields"]
        for field, evidence in update["field_provenance"].items():
            assert evidence, field
            for proof in evidence:
                resource = (
                    LiteratureStore(tmp_path).path(captured["snapshot_id"])
                    / "resources"
                    / proof["resource_path"]
                )
                assert hashlib.sha256(resource.read_bytes()).hexdigest() == proof["sha256"]


def test_resume_and_offline_replay_identical_identity(tmp_path, plan, monkeypatch):
    calls, fail = [], True

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/filters") and fail:
            return httpx.Response(503)
        return _handler(request)

    first = _run(tmp_path, plan, handler)
    assert not first["complete"] and first["counts"]["failed"] == 1
    assert derive_enrichment(tmp_path, first["snapshot_id"])["outcomes"] == first["outcomes"]
    fail = False
    completed = _run(tmp_path, plan, handler)
    assert completed["attempts"] == 1 and completed["cache_hits"] == 4
    assert len(calls) == 6
    monkeypatch.setattr(
        socket, "socket", lambda *a, **kw: pytest.fail("Offline replay contacted a socket")
    )
    replay = _run(
        tmp_path, plan, lambda _: pytest.fail("Offline acquisition reached transport"), offline=True
    )
    assert replay["snapshot_id"] == completed["snapshot_id"]
    assert replay["attempts"] == 0 and replay["cache_hits"] == 5
    assert (
        derive_enrichment(tmp_path, replay["snapshot_id"])["updates"]
        == derive_enrichment(tmp_path, completed["snapshot_id"])["updates"]
    )


def test_portable_derive_needs_neither_http_cache_nor_original_store(tmp_path, plan, monkeypatch):
    original = tmp_path / "original"
    result = _run(original, plan)
    store = LiteratureStore(original)
    archive = tmp_path / "metadata.zip"
    store.export(result["snapshot_id"], archive)
    fresh = LiteratureStore(tmp_path / "fresh")
    fresh.import_bundle(archive)
    monkeypatch.setattr(
        MetadataClient, "get", lambda *a, **kw: pytest.fail("Derivation used acquisition client")
    )
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Derivation resolved a host")
    )
    actual = derive_enrichment(fresh.root, result["snapshot_id"])
    assert actual == derive_enrichment(original, result["snapshot_id"])
    assert not (fresh.root / "harvest").exists()


def test_identity_mismatch_skips_dependent_endpoint_without_filling_fields(tmp_path, plan):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == "www.nomisweb.co.uk":
            assert request.url.path.endswith("/def.sdmx.json")
            return httpx.Response(200, json=_nomis("NM_99_99"))
        return _handler(request)

    result = _run(tmp_path, plan, handler)
    assert result["counts"]["datasets"] == 1 and result["counts"]["failed"] == 3
    assert len(calls) == 3
    derived = derive_enrichment(tmp_path, result["snapshot_id"])
    assert derived["updates"][0]["provider"] == "statswales"
    assert "identity disagrees" in result["outcomes"][0]["error"]


def test_budget_clamps_injected_client_and_checkpoint_survives_interruption(tmp_path, plan):
    # The plan permits five endpoints but retries consume this same network
    # attempt budget. An injected client's larger budget cannot bypass it.
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    http = HttpClient(
        transport=httpx.MockTransport(handler),
        allowed_hosts=METADATA_HOSTS,
        rate_limiter=RateLimiter(tmp_path / "rate.sqlite", interval_seconds=0),
        retries=2,
    )
    with http:
        client = MetadataClient(tmp_path, client=http, max_requests=100)
        result = capture_enrichment(tmp_path, plan, client=client)
    assert result["attempts"] == len(calls) == 5
    assert client.max_requests == 100
    assert not result["complete"]
    # KeyboardInterrupt leaves successful HTTP checkpoints, no final snapshot.
    root = tmp_path / "interrupted"

    def interrupt(request):
        if request.url.path.endswith("time.def.sdmx.json"):
            raise KeyboardInterrupt
        return _handler(request)

    with pytest.raises(KeyboardInterrupt):
        _run(root, plan, interrupt)
    assert LiteratureStore(root).list() == []
    resumed = _run(root, plan)
    assert resumed["cache_hits"] == 1 and resumed["attempts"] == 4


def test_offline_misses_are_explicit_partial_snapshot_and_refresh_invalid(tmp_path, plan):
    result = _run(
        tmp_path, plan, lambda _: pytest.fail("Offline miss made a request"), offline=True
    )
    assert not result["complete"] and result["attempts"] == 0
    assert result["counts"]["datasets"] == 0 and result["counts"]["failed"] == 5
    assert "Offline literature cache miss" in result["outcomes"][0]["error"]
    assert derive_enrichment(tmp_path, result["snapshot_id"])["updates"] == []
    with pytest.raises(EnrichmentError, match="mutually exclusive"):
        capture_enrichment(tmp_path, plan, offline=True, refresh=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["targets"][0]["requests"][0].update(
            url="https://www.nomisweb.co.uk/api/v01/dataset/NM_1_1.data.csv"
        ),
        lambda p: p["targets"][1]["requests"][0].update(
            params={"lang": "en-gb", "api_key": "secret"}
        ),
        lambda p: p["targets"][1].update(native_id="00000002-1234-4321-9876-123456789012"),
        lambda p: p.update(selection_split="test"),
        lambda p: p.update(max_requests=4),
        lambda p: p.update(max_total_bytes=0),
    ],
)
def test_plan_rejects_arbitrary_urls_identity_changes_and_budgets(plan, mutate):
    mutate(plan)
    with pytest.raises(EnrichmentError):
        validate_enrichment_plan(plan)


def test_ambiguous_time_and_geography_do_not_create_false_coverage(tmp_path, plan):
    def handler(request):
        if request.url.path.endswith("time.def.sdmx.json"):
            return httpx.Response(
                200, json=_codes([{"value": "TOTAL", "description": {"value": "All years"}}])
            )
        if request.url.path.endswith("geography.def.sdmx.json"):
            return httpx.Response(
                200, json=_codes([{"value": "E06000044", "description": {"value": "Portsmouth"}}])
            )
        return _handler(request)

    result = _run(tmp_path, plan, handler)
    update = derive_enrichment(tmp_path, result["snapshot_id"])["updates"][0]
    assert "coverage" not in update["fields"]
    assert len(update["limitations"]) == 2


@pytest.mark.parametrize("mixed", [False, True])
def test_stats_country_coverage_requires_all_geographic_roots_known_country_codes(
    tmp_path, plan, mixed
):
    def handler(request):
        if request.url.path.endswith("/filters"):
            dimensions = _filters()
            dimensions[0]["columnName"] = "Area name"
            dimensions[0]["values"][0]["reference"] = "W92000004"
            dimensions[0]["values"][0]["description"] = "Wales"
            if mixed:
                dimensions[0]["values"].append(
                    {"reference": "unknown", "description": "Other regions"}
                )
            return httpx.Response(200, json=dimensions)
        return _handler(request)

    result = _run(tmp_path, plan, handler)
    update = derive_enrichment(tmp_path, result["snapshot_id"])["updates"][1]
    if mixed:
        assert "countries" not in update["fields"]["coverage"]
    else:
        assert update["fields"]["coverage"]["countries"] == ["Wales"]
        assert update["field_provenance"]["coverage.countries"][0]["json_pointer"] == "/0/values"
    assert "places" not in update["fields"]["coverage"]


def test_normalized_updates_are_verified_against_raw_not_merely_read(tmp_path, plan):
    result = _run(tmp_path, plan)
    store = LiteratureStore(tmp_path)
    document = store.read(result["snapshot_id"])
    raw_root = store.path(result["snapshot_id"]) / "resources"
    resources = {
        p.relative_to(raw_root).as_posix(): p.read_bytes()
        for p in raw_root.rglob("*")
        if p.is_file()
    }
    document["enrichment"]["updates"][0]["fields"]["coverage"]["period_start"] = 1900
    resources["enrichment-derived.json"] = json.dumps(
        document["enrichment"], sort_keys=True
    ).encode()
    mutated = store.publish(document, resources)
    with pytest.raises(EnrichmentError, match="Raw metadata replay"):
        derive_enrichment(tmp_path, mutated)


def test_selection_only_uses_development_and_does_not_guess_historical_native(
    tmp_path, monkeypatch
):
    for name in ("manifest.json", "corpus.json", "questions.json", "embeddings.npz"):
        (tmp_path / name).write_bytes(name.encode())
    documents = [
        {**_target("nomis", "NM_1_1"), "id": "nomis:NM_1_1", "coverage": {}},
        {**_target("statswales", UUID), "id": "statswales:" + UUID, "coverage": {}},
        {
            "id": "historic",
            "provider": "nomis",
            "native_id": "C2021TS021",
            "title": "Historic table",
            "references": [REFERENCE],
        },
    ]
    questions = [
        {"id": "dev-1", "split": "dev", "relevance": {"historic": 3}},
        {"id": "test-1", "split": "test", "relevance": {}},
    ]
    bundle = BenchmarkBundle(
        tmp_path,
        {},
        documents,
        questions,
        [],
        np.zeros((3, 2)),
        np.zeros((2, 2)),
        (1,) * 7,
        60,
        (5, 10),
    )
    observed = []

    def ranked(bundle, question, method):
        observed.append((question["id"], method))
        assert question["split"] == "dev"
        return [
            {"id": "nomis:NM_1_1", "checks": [{"status": "unknown"}]},
            {"id": "statswales:" + UUID, "checks": []},
        ]

    monkeypatch.setattr("ssdu.discovery_enrichment.rank", ranked)
    actual = build_enrichment_plan(
        bundle, {"test_question": "cannot inject"}, dataset_limit=2, max_requests=5
    )
    assert len(observed) == 3 and actual["planned_requests"] == 5
    assert [target["provider"] for target in actual["targets"]] == ["nomis", "statswales"]
    assert actual["unresolved"][0]["dataset_id"] == "historic"
    assert actual["unresolved"][0]["question_ids"] == ["dev-1"]
    assert all(target["question_ids"] == ["dev-1"] for target in actual["targets"])
    assert actual["benchmark"]["manifest_sha256"] == hashlib.sha256(b"manifest.json").hexdigest()
    assert documents[0]["base_coverage"]["release"] == "2019"
