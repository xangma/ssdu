"""Independently invented GOV.UK protocol metadata and observations.

No provider metadata or observation values are captured here. Unrelated housing,
local-finance and deprivation-shaped examples exercise one shared file workflow.
"""

import copy
import hashlib
import json

import httpx
import pandas as pd
import pytest

from ssdu.adapters import mhclg
from ssdu.dataset_contract import Dataset
from ssdu.distributions import parse_csv
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError

ORG = "/government/organisations/ministry-of-housing-communities-local-government"
ASSETS = "https://assets.publishing.service.gov.uk/media/invented/"
CSV = b"area_code,homes,flag\nA01,0012,\nA02,[c],suppressed\n"


def publication(slug="synthetic-housing", *, attachments=None, revision="v1", organisation=ORG):
    return {
        "base_path": "/government/statistics/" + slug,
        "title": "Invented " + slug,
        "content_id": "invented-" + slug,
        "document_type": "official_statistics",
        "first_published_at": "2026-01-01T00:00:00Z",
        "public_updated_at": "2026-02-01T00:00:00Z" if revision == "v2" else "2026-01-01T00:00:00Z",
        "links": {"organisations": [{"base_path": organisation}]},
        "details": {
            "attachments": attachments
            if attachments is not None
            else [
                {
                    "id": "table-" + revision,
                    "title": "Invented table",
                    "url": ASSETS + revision + ".csv",
                }
            ]
        },
    }


def client_for(document, files=None, *, calls=None):
    files = {ASSETS + "v1.csv": CSV, **(files or {})}

    def respond(request):
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.startswith("/api/content/"):
            return httpx.Response(200, json=document)
        if request.url.path == "/api/search.json":
            return httpx.Response(
                200,
                json={
                    "results": [{"title": document["title"], "link": document["base_path"]}],
                    "total": 1,
                },
            )
        body = files.get(str(request.url))
        assert body is not None, f"Unexpected request: {request.url}"
        return httpx.Response(200, content=body, headers={"etag": '"invented-etag"'})

    return HttpClient(transport=httpx.MockTransport(respond), allowed_hosts=mhclg.HOSTS)


@pytest.mark.parametrize(
    "slug,body",
    [
        ("synthetic-housing", CSV),
        ("synthetic-local-finance", b"authority,budget\nB01,321.50\nB02,0.00\n"),
        ("synthetic-area-indicators", b"small_area,score,rank\nC01,3.125,1\nC02,2.875,2\n"),
    ],
)
def test_general_csv_publications_preserve_source_cells(slug, body):
    request = Request("mhclg", slug)
    with client_for(publication(slug), {ASSETS + "v1.csv": body}) as client:
        acquisition = mhclg.fetch(request, client)
        assert len(client.resources) == 3
        assert (
            next(
                resource for resource in client.resources if resource.url == ASSETS + "v1.csv"
            ).body
            == body
        )
    dataset = acquisition.dataset
    assert dataset.contract["profile"] == "records"
    assert dataset.contract["contract_version"] == "1.1.0"
    assert dataset.contract["transform"] == {"id": "ssdu.mhclg.records", "version": "1.0.0"}
    assert dataset.to_arrow("source").equals(dataset.to_arrow("records"))
    assert dataset.contract["dimensions"] == dataset.contract["variables"] == []
    assert acquisition.release["resource_sha256"] == hashlib.sha256(body).hexdigest()
    assert acquisition.release["etag"] == '"invented-etag"'
    assert all(str(dtype) == "string" for dtype in acquisition.frame.dtypes)


def test_plain_csv_does_not_guess_numeric_missingness_or_geography():
    with client_for(publication()) as client:
        acquisition = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    assert acquisition.frame.to_dict("list") == {
        "area_code": ["A01", "A02"],
        "homes": ["0012", "[c]"],
        "flag": ["", "suppressed"],
    }
    assert all(
        column.get("unit") is None
        for column in acquisition.dataset.contract["tables"][0]["columns"]
    )


def typed_metadata():
    return {
        "@context": "http://www.w3.org/ns/csvw",
        "url": "typed.csv",
        "tableSchema": {
            "columns": [
                {"name": "area_code", "titles": "Area", "datatype": "string", "required": True},
                {"name": "homes", "titles": "Homes", "datatype": "integer", "null": ["", "[c]"]},
                {"name": "budget", "titles": "Budget", "datatype": "decimal"},
                {"name": "active", "titles": "Active", "datatype": "boolean"},
            ],
            "primaryKey": "area_code",
        },
    }


def test_csvw_explicit_types_keys_nulls_and_source_lexemes():
    document = publication(
        attachments=[{"id": "metadata", "url": ASSETS + "typed.csv-metadata.json"}]
    )
    body = b"Area,Homes,Budget,Active\nA01,0012,321.500,true\nA02,[c],1.250,false\n"
    with client_for(
        document,
        {
            ASSETS + "typed.csv-metadata.json": json.dumps(typed_metadata()).encode(),
            ASSETS + "typed.csv": body,
        },
    ) as client:
        acquisition = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
        assert len(client.resources) == 4
    dataset = acquisition.dataset
    assert dataset.contract["primary_key"] == ["area_code"]
    assert dataset.to_arrow("records")["homes"].to_pylist() == [12, None]
    assert dataset.to_arrow("records")["active"].to_pylist() == [True, False]
    assert dataset.to_arrow("source")["Homes"].to_pylist() == ["0012", "[c]"]
    assert str(dataset.to_arrow("records")["budget"][0].as_py()) == "321.500"
    assert acquisition.metadata["csvw"] == typed_metadata()


def test_revision_attachment_changes_and_offline_contract_replay(tmp_path):
    captures = []
    for edition, body in [("v1", CSV), ("v2", b"area_code,homes,flag\nA01,0019,revised\n")]:
        with client_for(publication(revision=edition), {ASSETS + edition + ".csv": body}) as client:
            captures.append(mhclg.fetch(Request("mhclg", "synthetic-housing"), client))
    old, new = captures
    assert old.release["revision"] != new.release["revision"]
    assert old.release["resource_url"] != new.release["resource_url"]
    assert old.release["distribution_id"] != new.release["distribution_id"]
    old.dataset.write(tmp_path / "retained-old")
    # Dataset.read is an offline operation; no adapter or client is involved.
    restored = Dataset.read(tmp_path / "retained-old")
    assert restored.to_arrow("records").equals(old.dataset.to_arrow("records"))
    assert restored.contract["source"]["release"] == old.release
    assert restored.contract["source"]["release"] != new.release


def test_catalogue_pagination_parameters_and_no_attachment_download():
    calls = []

    def respond(request):
        calls.append(request)
        offset = int(request.url.params["start"])
        assert request.url.params.get_list("filter_organisations") == list(mhclg.ORGANISATIONS)
        assert request.url.params["q"] == "housing"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": f"Invented {offset}",
                        "link": f"/government/statistics/synthetic-housing-{offset}",
                    }
                ],
                "total": 2,
            },
        )

    with HttpClient(transport=httpx.MockTransport(respond), allowed_hosts=mhclg.HOSTS) as client:
        first = mhclg.list_datasets("housing", client, limit=1)
        second = mhclg.list_datasets("housing", client, limit=1, offset=first["next_offset"])
    assert first["items"][0]["id"] == "synthetic-housing-0"
    assert second["next_offset"] is None
    assert len(calls) == 2


def test_describe_resolves_current_metadata_without_download():
    with client_for(publication()) as client:
        described = mhclg.describe_dataset("synthetic-housing", client)
        assert len(client.resources) == 1
    assert described["distributions"][0]["id"] == "table-v1"
    assert described["capabilities"]["immutable_upstream_release"] is False
    assert described["dimensions"] == []


def test_statistical_data_set_ids_round_trip_transport_identifier():
    from pydantic import TypeAdapter

    from ssdu.contracts import Identifier

    path = "/government/statistical-data-sets/synthetic-local-finance"
    document = publication("synthetic-local-finance")
    document["base_path"] = path
    assert mhclg._identifier(path) == "statdata:synthetic-local-finance"
    canonical = TypeAdapter(Identifier).validate_python(mhclg._identifier(path))
    with client_for(document) as client:
        listed = mhclg.list_datasets("finance", client)
        described = mhclg.describe_dataset(canonical, client)
        fetched = mhclg.fetch(Request("mhclg", canonical), client)
    assert listed["items"][0]["id"] == canonical
    assert described["dataset_id"] == canonical
    assert fetched.dataset.contract["source"]["dataset_id"] == canonical
    assert described["source_url"] == mhclg.BASE + path


@pytest.mark.parametrize(
    "organisation",
    [
        ORG,
        "/government/organisations/department-for-levelling-up-housing-and-communities",
        "/government/organisations/department-for-communities-and-local-government",
    ],
)
def test_current_and_predecessor_publishers(organisation):
    with client_for(publication(organisation=organisation)) as client:
        assert mhclg.describe_dataset("synthetic-housing", client)["provider"] == "mhclg"


def test_wrong_publisher_rejected_before_attachment():
    with client_for(
        publication(organisation="/government/organisations/unrelated-ministry")
    ) as client:
        with pytest.raises(RetrievalError, match="not published by MHCLG"):
            mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
        assert len(client.resources) == 1


def test_primary_publisher_takes_precedence_over_tagged_organisation():
    document = publication()
    document["links"]["primary_publishing_organisation"] = [
        {"base_path": "/government/organisations/unrelated-ministry"}
    ]
    with (
        client_for(document) as client,
        pytest.raises(RetrievalError, match="not published by MHCLG"),
    ):
        mhclg.describe_dataset("synthetic-housing", client)


def test_ambiguous_distributions_require_explicit_selection():
    document = publication(
        attachments=[
            {"id": "homes", "url": ASSETS + "v1.csv"},
            {"id": "finances", "url": ASSETS + "finances.csv"},
        ]
    )
    with client_for(document) as client:
        with pytest.raises(RetrievalError, match="ambiguous"):
            mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
        assert len(client.resources) == 1
    with client_for(document) as client:
        selected = mhclg.fetch(
            Request("mhclg", "synthetic-housing", {"distribution": "homes"}), client
        )
    assert selected.release["distribution_id"] == "homes"


@pytest.mark.parametrize(
    "url",
    [
        "https://untrusted.example/table.csv",
        "http://assets.publishing.service.gov.uk/table.csv",
        "https://user:pass@assets.publishing.service.gov.uk/table.csv",
        "https://assets.publishing.service.gov.uk:444/table.csv",
        ASSETS + "report.pdf",
    ],
)
def test_untrusted_or_unsupported_attachment_is_not_fetched(url):
    with client_for(publication(attachments=[{"id": "bad", "url": url}])) as client:
        with pytest.raises(RetrievalError, match="unsupported format|untrusted URL"):
            mhclg.fetch(Request("mhclg", "synthetic-housing", {"distribution": "bad"}), client)
        assert len(client.resources) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"distribution": 1},
        {"selectors": []},
        {"selectors": {"area_code": "A01"}},
        {"selectors": {"area_code": [1]}},
        {"selectors": {"area_code": ["A01", "A01"]}},
        {"expected_revision": "unversioned"},
        {"year": 2026},
    ],
)
def test_invalid_request_rejected_before_network(params):
    with client_for(publication()) as client:
        with pytest.raises(RetrievalError):
            mhclg.fetch(Request("mhclg", "synthetic-housing", params), client)
        assert client.resources == []


def test_exact_code_local_selection_and_limit():
    with client_for(publication()) as client:
        selected = mhclg.fetch(
            Request(
                "mhclg",
                "synthetic-housing",
                {
                    "selectors": {"area_code": ["A02"]},
                },
                max_rows=1,
            ),
            client,
        )
    assert selected.frame["homes"].tolist() == ["[c]"]
    assert selected.metadata["raw_rows"] == 2
    with client_for(publication()) as client, pytest.raises(RetrievalError, match="no truncation"):
        mhclg.fetch(Request("mhclg", "synthetic-housing", max_rows=1), client)


@pytest.mark.parametrize(
    "selectors,match",
    [
        ({"unknown": ["A01"]}, "unknown column"),
        ({"area_code": ["A99"]}, "absent"),
        ({"area_code": ["A01"], "homes": ["[c]"]}, "empty table"),
    ],
)
def test_invalid_complete_selections_fail(selectors, match):
    with client_for(publication()) as client, pytest.raises(RetrievalError, match=match):
        mhclg.fetch(Request("mhclg", "synthetic-housing", {"selectors": selectors}), client)


def test_expected_revision_and_bytes_are_checked():
    with client_for(publication()) as client:
        with pytest.raises(RetrievalError, match="expected_revision"):
            mhclg.fetch(
                Request("mhclg", "synthetic-housing", {"expected_revision": "0" * 64}), client
            )
        assert len(client.resources) == 1
    with (
        client_for(publication()) as client,
        pytest.raises(RetrievalError, match="expected_sha256"),
    ):
        mhclg.fetch(Request("mhclg", "synthetic-housing", {"expected_sha256": "0" * 64}), client)
    with client_for(publication()) as client:
        valid = mhclg.fetch(
            Request(
                "mhclg", "synthetic-housing", {"expected_sha256": hashlib.sha256(CSV).hexdigest()}
            ),
            client,
        )
    assert valid.release["resource_sha256"] == hashlib.sha256(CSV).hexdigest()


def test_dimension_enumeration_is_explicitly_unsupported():
    with client_for(publication()) as client:
        with pytest.raises(RetrievalError, match="does not enumerate"):
            mhclg.list_dimension_values("synthetic-housing", "area_code", client)
        assert client.resources == []


@pytest.mark.parametrize(
    "body", [b"a,a\n1,2\n", b"a,b\n1\n", b"a\n", b"a\n\xff\n", b'a,b\n"unclosed,2\n']
)
def test_malformed_csv_fails_without_partial_product(body):
    with (
        client_for(publication(), {ASSETS + "v1.csv": body}) as client,
        pytest.raises(RetrievalError),
    ):
        mhclg.fetch(Request("mhclg", "synthetic-housing"), client)


@pytest.mark.parametrize(
    "change,match",
    [
        (
            lambda schema: schema["columns"][0].update({"name": "unknown", "titles": "Wrong"}),
            "disagrees",
        ),
        (
            lambda schema: schema["columns"][1].update({"datatype": "unrecognised"}),
            "Unsupported CSVW datatype",
        ),
        (
            lambda schema: schema["columns"][1].update(
                {"datatype": {"base": "integer", "minimum": 0}}
            ),
            "constraints",
        ),
        (
            lambda schema: schema["columns"][1].update({"separator": ";"}),
            "unsupported interpretation",
        ),
        (lambda schema: schema.update({"primaryKey": "missing"}), "unknown"),
    ],
)
def test_csvw_unsupported_or_ambiguous_interpretation_fails(change, match):
    schema = copy.deepcopy(typed_metadata()["tableSchema"])
    change(schema)
    with pytest.raises(RetrievalError, match=match):
        parse_csv(b"Area,Homes,Budget,Active\nA01,12,321.5,true\n", schema=schema)


def test_csvw_required_and_duplicate_primary_key_fail():
    schema = typed_metadata()["tableSchema"]
    with pytest.raises(RetrievalError, match="Required"):
        parse_csv(b"Area,Homes,Budget,Active\n,12,321.5,true\n", schema=schema)
    with pytest.raises(RetrievalError, match="duplicate"):
        parse_csv(
            b"Area,Homes,Budget,Active\nA01,12,321.5,true\nA01,14,1.25,false\n", schema=schema
        )


def test_csvw_dialect_and_relative_schema_resource():
    document = publication(
        attachments=[{"id": "metadata", "url": ASSETS + "typed.csv-metadata.json"}]
    )
    metadata = {
        "tables": [{"url": "typed.csv", "tableSchema": "schema.json"}],
        "dialect": {"delimiter": ";"},
    }
    with client_for(
        document,
        {
            ASSETS + "typed.csv-metadata.json": json.dumps(metadata).encode(),
            ASSETS + "schema.json": json.dumps(typed_metadata()["tableSchema"]).encode(),
            ASSETS + "typed.csv": b"Area;Homes;Budget;Active\nA01;0012;321.500;true\n",
        },
    ) as client:
        acquisition = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    assert acquisition.frame["homes"].tolist() == [12]


def test_csvw_selectors_use_declared_column_names_but_original_codes():
    document = publication(
        attachments=[{"id": "metadata", "url": ASSETS + "typed.csv-metadata.json"}]
    )
    with client_for(
        document,
        {
            ASSETS + "typed.csv-metadata.json": json.dumps(typed_metadata()).encode(),
            ASSETS
            + "typed.csv": b"Area,Homes,Budget,Active\nA01,0012,321.500,true\nA02,14,1.25,false\n",
        },
    ) as client:
        acquisition = mhclg.fetch(
            Request("mhclg", "synthetic-housing", {"selectors": {"homes": ["0012"]}}), client
        )
    assert acquisition.frame["homes"].tolist() == [12]
    assert acquisition.dataset.to_arrow("source")["Homes"].to_pylist() == ["0012"]


def test_normalization_requires_original_cells():
    from ssdu.normalization_mhclg import normalize

    with client_for(publication()) as client:
        acquisition = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    with pytest.raises(RetrievalError, match="retained original"):
        normalize(acquisition)
    assert isinstance(acquisition.frame.iloc[0, 0], str)
    assert not pd.isna(acquisition.frame.iloc[0, 2])


def test_store_reuses_whole_resource_for_new_selections_and_offline_replay(tmp_path):
    from ssdu.store import Store

    store, calls = Store(tmp_path), []
    with client_for(publication(), calls=calls) as client:
        first = store.fetch(
            Request("mhclg", "synthetic-housing", {"selectors": {"area_code": ["A01"]}}),
            client=client,
        )
        second = store.fetch(
            Request("mhclg", "synthetic-housing", {"selectors": {"area_code": ["A02"]}}),
            client=client,
        )
    assert calls.count(ASSETS + "v1.csv") == 1
    assert store.replay(first["product_id"])["homes"].tolist() == ["0012"]
    assert store.replay(second["product_id"])["homes"].tolist() == ["[c]"]
    complete = store.fetch_dataset(Request("mhclg", "synthetic-housing"), offline=True)
    assert complete.to_arrow("records")["homes"].to_pylist() == ["0012", "[c]"]
    migrated = store.migrate_dataset(first["product_id"])
    assert migrated.to_arrow("source")["homes"].to_pylist() == ["0012"]


def test_cached_resource_is_scoped_to_publication_revision_even_when_url_reused(tmp_path):
    from ssdu.connector_cache import CachedClient
    from ssdu.store import Store

    store, calls = Store(tmp_path), []
    with client_for(publication(), calls=calls) as client:
        old = store.fetch(
            Request("mhclg", "synthetic-housing", {"selectors": {"area_code": ["A01"]}}),
            client=client,
        )
    changed = publication(revision="v2", attachments=[{"id": "table-v2", "url": ASSETS + "v1.csv"}])
    new_csv = b"area_code,homes,flag\nA01,0018,revised\nA02,0022,revised\n"
    with client_for(changed, {ASSETS + "v1.csv": new_csv}, calls=calls) as client:
        # Refresh only discovery metadata, leaving the old URL's file cached.
        with CachedClient(
            tmp_path, client=client, refresh=True, allowed_hosts=mhclg.HOSTS
        ) as metadata_client:
            mhclg.describe_dataset("synthetic-housing", metadata_client)
        new = store.fetch(
            Request("mhclg", "synthetic-housing", {"selectors": {"area_code": ["A02"]}}),
            client=client,
        )
    assert calls.count(ASSETS + "v1.csv") == 2
    assert store.replay(new["product_id"])["homes"].tolist() == ["0022"]
    assert old["release"]["resource_sha256"] != new["release"]["resource_sha256"]
    assert store.open_dataset(old["product_id"]).to_arrow("source")["homes"].to_pylist() == ["0012"]


def test_publication_change_during_file_download_is_rejected():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "www.gov.uk":
            return httpx.Response(200, json=publication(revision="v1" if len(calls) == 1 else "v2"))
        return httpx.Response(200, content=CSV)

    with (
        HttpClient(transport=httpx.MockTransport(respond), allowed_hosts=mhclg.HOSTS) as client,
        pytest.raises(RetrievalError, match="changed during retrieval"),
    ):
        mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    assert len(calls) == 3


def test_file_rate_limit_uses_shared_transport_retry_policy():
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.host == "www.gov.uk":
            return httpx.Response(200, json=publication())
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, content=CSV)

    with HttpClient(transport=httpx.MockTransport(respond), allowed_hosts=mhclg.HOSTS) as client:
        result = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
        assert client.attempts == 4  # two metadata checks and two bounded file attempts
    assert attempts == 2
    assert result.frame["homes"].tolist() == ["0012", "[c]"]


def test_multiple_csvw_tables_require_explicit_choice():
    document = publication(
        attachments=[{"id": "metadata", "url": ASSETS + "typed.csv-metadata.json"}]
    )
    metadata = {
        "tables": [
            {"url": "typed.csv", "tableSchema": typed_metadata()["tableSchema"]},
            {"url": "other.csv", "tableSchema": typed_metadata()["tableSchema"]},
        ]
    }
    files = {
        ASSETS + "typed.csv-metadata.json": json.dumps(metadata).encode(),
        ASSETS + "typed.csv": b"Area,Homes,Budget,Active\nA01,0012,321.500,true\n",
    }
    with client_for(document, files) as client:
        with pytest.raises(RetrievalError, match="table selection is missing or ambiguous"):
            mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
        assert len(client.resources) == 2
    with client_for(document, files) as client:
        chosen = mhclg.fetch(Request("mhclg", "synthetic-housing", {"table": "typed.csv"}), client)
    assert chosen.frame["homes"].tolist() == [12]


def test_publication_and_csvw_licences_are_only_reported_when_explicit():
    document = publication()
    with client_for(document) as client:
        unknown = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    assert unknown.licence == mhclg.UNKNOWN_LICENCE
    document["details"]["licence"] = "https://example.org/synthetic-licence"
    with client_for(document) as client:
        explicit = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    assert explicit.licence == "https://example.org/synthetic-licence"
    document["details"]["attachments"] = [
        {"id": "metadata", "url": ASSETS + "typed.csv-metadata.json"}
    ]
    metadata = typed_metadata()
    metadata["dc:license"] = {"@id": "https://example.org/synthetic-table-licence"}
    with client_for(
        document,
        {
            ASSETS + "typed.csv-metadata.json": json.dumps(metadata).encode(),
            ASSETS + "typed.csv": b"Area,Homes,Budget,Active\nA01,0012,321.500,true\n",
        },
    ) as client:
        declared = mhclg.fetch(Request("mhclg", "synthetic-housing"), client)
    assert (
        declared.licence
        == declared.metadata["licence"]
        == "https://example.org/synthetic-table-licence"
    )
