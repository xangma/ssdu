"""Invented Scottish linked cubes exercise the common connector workflow.

No provider metadata or observations are captured. Protocol references:
https://guides.statistics.gov.scot/article/22-querying-data-with-sparql
https://guides.statistics.gov.scot/article/41-api-access-for-file-datasets-not-linked-data
"""

import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from ssdu import Connector, Request, Store
from ssdu.adapters import scotland
from ssdu.bundles import export_bundle, import_bundle
from ssdu.collections import Collections
from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient
from ssdu.models import RetrievalError
from ssdu.rate_limit import RateLimiter

FIXTURES = Path(__file__).parent / "fixtures" / "scotland" / "series"
DATASET = "ssdu-synthetic-unrelated-events"
CODE = "https://example.invalid/ssdu/geography/001"


def fixtures():
    """Reuse independently invented RDF terms under an unrelated native slug."""
    return tuple(
        json.loads((FIXTURES / filename).read_text().replace("ssdu-synthetic-series", DATASET))
        for filename in ("metadata.json", "observations.json")
    )


def term(value, kind="literal"):
    return {"type": kind, "value": value}


def document(variables, rows):
    return {"head": {"vars": variables}, "results": {"bindings": rows}}


def responder(calls, metadata=None, observations=None):
    metadata_fixture, observation_fixture = fixtures()
    metadata = metadata or metadata_fixture
    observations = observations or observation_fixture

    def handler(request):
        assert request.url.host == "statistics.gov.scot"
        assert request.url.path == "/sparql.json"
        query = request.url.params["query"]
        calls.append(query)
        if "AS ?kind" in query:
            return httpx.Response(200, json=metadata)
        if query.startswith("SELECT ?dataset"):
            assert "GROUP BY ?dataset ORDER BY ?dataset" in query
            size, offset = map(int, re.search(r"LIMIT (\d+) OFFSET (\d+)", query).groups())
            rows = [
                {"dataset": term(scotland.BASE + identity, "uri"), "title": term(identity)}
                for identity in [DATASET, "ssdu-synthetic-other-events", "ssdu-synthetic-z-events"]
            ]
            return httpx.Response(
                200, json=document(["dataset", "title"], rows[offset : offset + size])
            )
        if query.startswith("SELECT ?value"):
            assert "GROUP BY ?value ORDER BY ?value" in query
            assert "?property" not in query and "measure" not in query
            size, offset = map(int, re.search(r"LIMIT (\d+) OFFSET (\d+)", query).groups())
            rows = [
                {"value": term(CODE, "uri"), "label": term("Invented district 001")},
                {"value": term(CODE[:-1] + "2", "uri")},
                {"value": term(CODE[:-1] + "3", "uri"), "label": term("Invented district 003")},
            ]
            return httpx.Response(
                200, json=document(["value", "label"], rows[offset : offset + size])
            )
        assert query.startswith("SELECT DISTINCT ?obs")
        return httpx.Response(200, json=observations)

    return handler


def http(tmp_path, handler):
    return HttpClient(
        transport=httpx.MockTransport(handler),
        rate_limiter=RateLimiter(tmp_path / "fixture-rate.sqlite3", interval_seconds=0),
    )


def test_catalogue_pages_have_one_native_identity_and_bounded_lookahead(tmp_path):
    calls = []
    with http(tmp_path, responder(calls)) as client:
        first = scotland.list_datasets('events "quoted"', client, limit=2)
        final = scotland.list_datasets("", client, limit=2, offset=first["next_offset"])
    assert [item["id"] for item in first["items"]] == [DATASET, "ssdu-synthetic-other-events"]
    assert first["total"] is None and first["next_offset"] == 2
    assert final["items"][0]["id"] == "ssdu-synthetic-z-events"
    assert final["next_offset"] is None
    assert '\\"quoted\\"' in calls[0]
    assert scotland.QB + "dimension" in calls[0] and scotland.QB + "measure" in calls[0]
    assert all("GROUP BY ?dataset" in query for query in calls)


def test_describe_is_metadata_only_and_dimensions_keep_native_uri_ids(tmp_path):
    calls = []
    with http(tmp_path, responder(calls)) as client:
        descriptor = scotland.describe_dataset(DATASET, client)
    assert len(calls) == 1 and "AS ?kind" in calls[0]
    assert descriptor["provider"] == "scotland" and descriptor["dataset_id"] == DATASET
    assert {dim["id"] for dim in descriptor["dimensions"]} == {
        scotland.AREA,
        scotland.PERIOD,
    }
    assert descriptor["capabilities"]["dimension_values_scope"] == "observed_dataset_members"
    assert descriptor["release"]["historical_revision_addressable"] is False
    assert descriptor["release"]["modified"] == "2024-01-02T00:00:00Z"
    assert len(descriptor["release"]["metadata_sha256"]) == 64
    assert descriptor["references"][0]["url"].startswith("https://guides.statistics.gov.scot/")


def test_member_pages_are_dataset_scoped_and_preserve_unknown_labels(tmp_path):
    calls = []
    with http(tmp_path, responder(calls)) as client:
        first = scotland.list_dimension_values(DATASET, scotland.AREA, client, limit=1)
        second = scotland.list_dimension_values(
            DATASET, scotland.AREA, client, limit=1, offset=first["next_offset"]
        )
        final = scotland.list_dimension_values(
            DATASET, scotland.AREA, client, limit=1, offset=second["next_offset"]
        )
    assert first["items"] == [{"value": CODE, "label": "Invented district 001"}]
    assert second["items"] == [{"value": CODE[:-1] + "2", "label": None}]
    assert final["next_offset"] is None
    assert first["scope"] == "provider" and first["value_scope"] == "observed_dataset_members"
    assert first["completeness"] == "not_an_external_vocabulary"
    assert all(not query.startswith("SELECT DISTINCT ?obs") for query in calls)


def test_selection_checks_metadata_and_native_uri_syntax_without_observations(tmp_path):
    calls = []
    with http(tmp_path, responder(calls)) as client:
        descriptor = scotland.describe_dataset(DATASET, client)
    request = Request("scotland", DATASET, {"dimensions": {scotland.AREA: [CODE]}})
    scotland.validate_selection(request, descriptor)
    for params, match in [
        ({"dimensions": {"https://example.invalid/unknown": [CODE]}}, "Unknown Scotland dimension"),
        ({"dimensions": {scotland.AREA: ['https://example.invalid/>"] UNION { ?s ?p ?o']}}, "URI"),
        ({"expected_modified": "different"}, "requested release"),
    ]:
        with pytest.raises(RetrievalError, match=match):
            scotland.validate_selection(Request("scotland", DATASET, params), descriptor)
    with pytest.raises(RetrievalError, match="different dataset"):
        scotland.validate_selection(request, {**descriptor, "dataset_id": "different"})
    assert len(calls) == 1


@pytest.mark.parametrize("limit,offset", [(0, 0), (True, 0), (1, -1), (1, True), (1001, 0)])
def test_bad_page_bounds_do_not_issue_requests(tmp_path, limit, offset):
    with http(tmp_path, lambda _: pytest.fail("Unexpected HTTP")) as client:
        with pytest.raises(ValueError):
            scotland.list_datasets("", client, limit=limit, offset=offset)
        with pytest.raises(ValueError):
            scotland.list_dimension_values(
                DATASET, scotland.AREA, client, limit=limit, offset=offset
            )


def test_unsupported_file_description_and_unknown_dimension_fail_before_values(tmp_path):
    metadata, observations = fixtures()
    calls = []
    with (
        http(tmp_path, responder(calls)) as client,
        pytest.raises(RetrievalError, match="Unknown Scotland dimension"),
    ):
        scotland.list_dimension_values(DATASET, "https://example.invalid/unknown", client)
    assert len(calls) == 1
    metadata["results"]["bindings"] = [
        row
        for row in metadata["results"]["bindings"]
        if row["kind"]["value"] == "dataset"
        and row["property"]["value"] != scotland.QB + "structure"
    ]
    with (
        http(tmp_path, responder([], metadata, observations)) as client,
        pytest.raises(RetrievalError, match="file datasets are unsupported"),
    ):
        scotland.describe_dataset(DATASET, client)


@pytest.mark.parametrize("kind", ["catalogue", "codes"])
@pytest.mark.parametrize("defect", ["duplicate", "overflow", "unsafe_uri"])
def test_malformed_or_unbounded_pages_fail_closed(tmp_path, kind, defect):
    metadata, _ = fixtures()
    row = (
        {"dataset": term(scotland.BASE + DATASET, "uri"), "title": term("Invented events")}
        if kind == "catalogue"
        else {"value": term(CODE, "uri"), "label": term("Invented district")}
    )
    rows = [copy.deepcopy(row)] * (3 if defect == "overflow" else 2)
    if defect == "unsafe_uri":
        rows = [copy.deepcopy(row)]
        rows[0]["dataset" if kind == "catalogue" else "value"]["value"] = "javascript:unsafe"

    def handler(request):
        if "AS ?kind" in request.url.params["query"]:
            return httpx.Response(200, json=metadata)
        return httpx.Response(200, json=document(list(row), rows))

    with http(tmp_path, handler) as client, pytest.raises(RetrievalError):
        if kind == "catalogue":
            scotland.list_datasets("", client, limit=1)
        else:
            scotland.list_dimension_values(DATASET, scotland.AREA, client, limit=1)


@pytest.mark.parametrize("change", ["revision", "interpretation"])
def test_changed_metadata_scopes_observations_and_members_but_not_stability_reads(tmp_path, change):
    metadata, observations = fixtures()
    state = {"metadata": metadata, "observations": observations, "label": "Original label"}
    calls, cache = [], {}

    def handler(request):
        query = request.url.params["query"]
        if "AS ?kind" in query:
            return httpx.Response(200, json=state["metadata"])
        if query.startswith("SELECT ?value"):
            return httpx.Response(
                200,
                json=document(
                    ["value", "label"],
                    [{"value": term(CODE, "uri"), "label": term(state["label"])}],
                ),
            )
        return httpx.Response(200, json=state["observations"])

    class ScopedCache:
        active = None

        def __init__(self, client):
            self.client = client

        @contextmanager
        def scope(self, token):
            previous, self.active = self.active, token
            try:
                yield self
            finally:
                self.active = previous

        def get(self, url, params=None):
            calls.append((params["query"], self.active))
            key = (self.active, url, json.dumps(params, sort_keys=True))
            if self.active is None:
                return self.client.get(url, params=params)
            if key not in cache:
                cache[key] = self.client.get(url, params=params)
            return cache[key]

    request = Request("scotland", DATASET, {"dimensions": {scotland.AREA: [CODE]}})
    with http(tmp_path, handler) as transport:
        client = ScopedCache(transport)
        first = scotland.fetch(request, client)
        first_codes = scotland.list_dimension_values(DATASET, scotland.AREA, client)
        state["metadata"] = copy.deepcopy(metadata)
        rows = state["metadata"]["results"]["bindings"]
        if change == "revision":
            next(row for row in rows if row["property"]["value"] == scotland.DCT + "modified")[
                "value"
            ]["value"] = "2025-01-01T00:00:00Z"
        else:
            next(row for row in rows if row["property"]["value"] == scotland.AREA)["label"][
                "value"
            ] = "Changed geography interpretation"
        state["label"] = "Changed member label"
        state["observations"] = copy.deepcopy(observations)
        next(
            row
            for row in state["observations"]["results"]["bindings"]
            if row["property"]["value"] == "https://example.invalid/ssdu/measure/count"
        )["value"]["value"] = "0009"
        second = scotland.fetch(request, client)
        second_codes = scotland.list_dimension_values(DATASET, scotland.AREA, client)
    assert first.dataset.to_arrow().to_pydict()["value_raw"] == ["0007", "0"]
    assert second.dataset.to_arrow().to_pydict()["value_raw"] == ["0009", "0"]
    assert first_codes["items"][0]["label"] != second_codes["items"][0]["label"]
    scopes = {scope for query, scope in calls if "AS ?kind" not in query}
    assert None not in scopes and len(scopes) == 2
    assert all(scope is None for query, scope in calls if "AS ?kind" in query)


def test_common_workflow_cache_and_fresh_store_snapshot_replay_are_offline(tmp_path):
    calls = []
    source = Store(tmp_path / "source")
    request = Request("scotland", DATASET, {"dimensions": {scotland.AREA: [CODE]}})
    with http(tmp_path, responder(calls)) as transport:
        connector = Connector("scotland", source, client=transport)
        catalogue = connector.list_datasets(limit=2)
        descriptor = connector.describe_dataset(DATASET)
        codes = connector.list_dimension_values(DATASET, scotland.AREA, limit=2)
        before = len(calls)
        assert connector.list_datasets(limit=2) == catalogue
        assert connector.describe_dataset(DATASET) == descriptor
        assert connector.list_dimension_values(DATASET, scotland.AREA, limit=2) == codes
        assert len(calls) == before
        assert connector.plan_fetch(request)["selector_validation"] == "metadata"
        manifest = connector.fetch(request)
        expected = source.open_dataset(manifest["product_id"]).to_arrow()
        before = len(calls)
        with patch.object(HttpClient, "request", side_effect=AssertionError("Offline HTTP")):
            assert connector.list_datasets(limit=2, offline=True) == catalogue
            assert connector.describe_dataset(DATASET, offline=True) == descriptor
            assert (
                connector.list_dimension_values(DATASET, scotland.AREA, limit=2, offline=True)
                == codes
            )
            assert connector.fetch_dataset(request, offline=True).to_arrow().equals(expected)
        assert len(calls) == before

    lock = Collections(source).resolve(
        {
            "name": "invented-scottish-cube",
            "entries": {"events": {"snapshot_id": manifest["product_id"]}},
        },
        offline=True,
    )
    bundle = tmp_path / "synthetic.ssdu.zip"
    export_bundle(source, lock, bundle)
    source.root.rename(tmp_path / "original-moved")
    fresh = Store(tmp_path / "fresh")
    with patch.object(HttpClient, "request", side_effect=AssertionError("Offline HTTP")):
        import_bundle(fresh, bundle)
        Collections(fresh).replay(lock.lock_id)
        restored = Connector("scotland", fresh).fetch_dataset(request, offline=True)
        assert restored.to_arrow().equals(expected)
        assert Connector("scotland", fresh).describe_dataset(DATASET, offline=True) == descriptor


def test_cached_member_page_same_metadata_never_acquires_rate_slot(tmp_path):
    calls = []
    with http(tmp_path, responder(calls)) as transport:
        with CachedClient(tmp_path / "store", client=transport) as client:
            expected = scotland.list_dimension_values(DATASET, scotland.AREA, client)
        before = len(calls)
        with (
            patch.object(RateLimiter, "slot", side_effect=AssertionError("Cache hit used quota")),
            CachedClient(tmp_path / "store", client=transport) as client,
        ):
            assert scotland.list_dimension_values(DATASET, scotland.AREA, client) == expected
        assert len(calls) == before
