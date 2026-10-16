"""Build and transfer a discovery catalogue using invented provider responses.

Every title, category and publication below is independently invented teaching
material, shaped like the five supported metadata protocols. No observations
are present. No provider is contacted. This script works outside the checkout.
Run with ``--output-dir NEW_DIRECTORY`` after installing SSDU.
"""

from __future__ import annotations

import argparse
import json
import socket
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from ssdu.http import HttpClient
from ssdu.metadata_contract import CONTRACT_VERSION
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_mapping import MAPPING_VERSION
from ssdu.metadata_store import MetadataStore
from ssdu.metadata_sync import MetadataSync
from ssdu.rate_limit import RateLimiter

DATASETS = {
    "nomis": "NM_999_2",
    "statswales": "00000000-0000-4000-8000-000000000001",
    "mhclg": "ssdu-invented-housing",
    "nisra": "SYNTH_METADATA",
    "scotland": "ckan:00000000-0000-4000-8000-000000000005",
}
TITLES = {
    "nomis": "Invented community workshop occupations",
    "statswales": "Invented community travel modes",
    "mhclg": "Invented community housing publication",
    "nisra": "Invented community workshop attendance",
    "scotland": "Invented community events",
}
QB = "http://purl.org/linked-data/cube#"
DCT = "http://purl.org/dc/terms/"
PERIOD = "http://purl.org/linked-data/sdmx/2009/dimension#refPeriod"
SCOT_BASE = "http://statistics.gov.scot/data/"


def nomis_definition():
    return {
        "structure": {
            "keyfamilies": {
                "keyfamily": [
                    {
                        "id": DATASETS["nomis"],
                        "name": {"value": TITLES["nomis"]},
                        "description": {
                            "lang": "en",
                            "value": "<p>Invented workshop roles for a classroom catalogue.</p>",
                        },
                        "components": {
                            "dimension": [{"conceptref": "OCCUPATION", "codelist": "CL_OCC"}],
                            "primarymeasure": {"conceptref": "OBS_VALUE"},
                            # A declared time axis supplies no observed time extent.
                            "timedimension": {"conceptref": "TIME"},
                        },
                        "annotations": {
                            "annotation": [{"annotationtitle": "Units", "annotationtext": "People"}]
                        },
                    }
                ]
            }
        }
    }


def nisra_metadata():
    return {
        "version": "2.0",
        "class": "dataset",
        "label": TITLES["nisra"],
        "note": [
            "<p>Invented attendance definitions for classroom exercises.</p>",
            "Listed periods are available categories; observations are absent.",
        ],
        "id": ["STATISTIC", "TLIST(A1)"],
        "size": [1, 2],
        "dimension": {
            "STATISTIC": {
                "label": "Statistic",
                "category": {
                    "index": ["COUNT"],
                    "label": {"COUNT": "Invented attendees"},
                    "unit": {"COUNT": {"label": "People"}},
                },
            },
            "TLIST(A1)": {
                "label": "Year",
                "category": {"index": ["2023", "2024"], "label": {"2023": "2023", "2024": "2024"}},
            },
        },
        "updated": "2026-01-01T00:00:00Z",
        "role": {"metric": ["STATISTIC"], "time": ["TLIST(A1)"]},
        "extension": {
            "matrix": DATASETS["nisra"],
            "copyright": {
                "name": "Invented teaching owner",
                "href": "https://example.invalid/ssdu/invented-cube-terms",
            },
        },
    }


def term(value, kind="literal"):
    return {"type": kind, "value": value}


def bindings(variables, rows):
    return {"head": {"vars": variables}, "results": {"bindings": rows}}


def scotland_metadata():
    rows = [
        {"kind": term("dataset"), "property": term(DCT + key, "uri"), "value": term(value)}
        for key, value in {
            "title": TITLES["scotland"],
            "description": "<p>Invented event definitions for a metadata lesson.</p>",
            "modified": "2026-01-01T00:00:00Z",
            "license": "https://example.invalid/ssdu/invented-fixtures",
            "accessRights": "Invented terms require permission before redistribution.",
        }.items()
    ]
    rows.append(
        {
            "kind": term("dataset"),
            "property": term(QB + "structure", "uri"),
            "value": term(SCOT_BASE + "structure/" + DATASETS["scotland"], "uri"),
        }
    )
    for role, prop, label in (
        ("dimension", PERIOD, "Year"),
        ("measure", "https://example.invalid/ssdu/events", "Invented events"),
    ):
        rows.append(
            {
                "kind": term(QB + role),
                "property": term(prop, "uri"),
                "value": term(prop, "uri"),
                "label": term(label),
            }
        )
    return bindings(["kind", "property", "value", "label", "attachment", "required"], rows)


def fixture_response(request):
    """Only metadata endpoints are permitted; unexpected requests fail closed."""
    assert request.method == "GET", "Metadata synchronization must not request observations"
    host, path = request.url.host, request.url.path
    if host == "www.nomisweb.co.uk":
        if path in (
            "/api/v01/dataset/def.sdmx.json",
            f"/api/v01/dataset/{DATASETS['nomis']}/def.sdmx.json",
        ):
            return httpx.Response(200, json=nomis_definition())
        if path == f"/api/v01/dataset/{DATASETS['nomis']}/time.def.sdmx.json":
            return httpx.Response(
                200,
                json={
                    "structure": {
                        "codelists": {
                            "codelist": {
                                "id": "CL_TIME",
                                "code": [
                                    {"value": year, "description": {"value": year}}
                                    for year in ("2023", "2024")
                                ],
                            }
                        }
                    }
                },
            )
        if path == f"/api/v01/dataset/{DATASETS['nomis']}/occupation.def.sdmx.json":
            return httpx.Response(
                200,
                json={
                    "structure": {
                        "codelists": {
                            "codelist": {
                                "id": "CL_OCC",
                                "code": [
                                    {
                                        "value": "001",
                                        "description": {"value": "Invented facilitators"},
                                    },
                                    {"value": "002", "description": {"value": "Invented makers"}},
                                ],
                            }
                        }
                    }
                },
            )
    if host == "api.stats.gov.wales":
        if path in ("/v2", "/v2/search"):
            return httpx.Response(
                200,
                json={
                    "data": [{"id": DATASETS["statswales"], "title": TITLES["statswales"]}],
                    "total": 1,
                },
            )
        if path == f"/v2/{DATASETS['statswales']}":
            return httpx.Response(
                200,
                json={
                    "id": DATASETS["statswales"],
                    "published_revision": {
                        "id": "invented-revision-1",
                        "metadata": [
                            {
                                "language": "en-GB",
                                "title": TITLES["statswales"],
                                "summary": "<p>Invented travel modes for a metadata lesson.</p>",
                            }
                        ],
                        "coverage_start_date": "2023-01-01",
                        "coverage_end_date": "2024-12-31",
                    },
                },
            )
        if path == f"/v2/{DATASETS['statswales']}/filters":
            return httpx.Response(
                200,
                json=[
                    {
                        "factTableColumn": "ModeKey",
                        "columnName": "Travel mode",
                        "values": [
                            {"reference": "01", "description": "Invented walking journeys"},
                            {"reference": "02", "description": "Invented cycling journeys"},
                        ],
                    }
                ],
            )
    if host == "www.gov.uk":
        publication = f"/government/statistics/{DATASETS['mhclg']}"
        if path == "/api/search.json":
            return httpx.Response(
                200, json={"results": [{"title": TITLES["mhclg"], "link": publication}], "total": 1}
            )
        if path == "/api/content" + publication:
            return httpx.Response(
                200,
                json={
                    "base_path": publication,
                    "title": TITLES["mhclg"],
                    "content_id": DATASETS["mhclg"],
                    "public_updated_at": "2026-01-01T00:00:00Z",
                    "description": "Independently invented community teaching publication.",
                    "links": {
                        "organisations": [
                            {
                                "base_path": "/government/organisations/ministry-of-housing-communities-local-government"
                            }
                        ]
                    },
                    "details": {
                        "licence": "https://example.invalid/ssdu/invented-publication-terms",
                        "national_applicability": {
                            "invented-region": {
                                "applicable": True,
                                "label": "Invented Teaching Region",
                            }
                        },
                        "attachments": [
                            {
                                "id": "invented-csv",
                                "title": "Invented community housing CSV",
                                "url": "https://assets.publishing.service.gov.uk/media/ssdu-invented/housing.csv",
                                "content_type": "text/csv",
                            }
                        ],
                    },
                },
            )
    if host == "ws-data.nisra.gov.uk":
        base = "/public/api.restful/PxStat.Data.Cube_API."
        if path == base + "ReadCollection":
            return httpx.Response(
                200,
                json={
                    "version": "2.0",
                    "class": "collection",
                    "link": {
                        "item": [
                            {"label": TITLES["nisra"], "extension": {"matrix": DATASETS["nisra"]}}
                        ]
                    },
                },
            )
        if path == base + f"ReadMetadata/{DATASETS['nisra']}/en":
            return httpx.Response(200, json=nisra_metadata())
    if host == "api.data.gov.scot":
        dataset_id = DATASETS["scotland"].removeprefix("ckan:")
        native = {
            "id": dataset_id,
            "name": "ssdu-invented-community",
            "title": TITLES["scotland"],
            "description": "<p>Invented event definitions for a metadata lesson.</p>",
            "metadata_modified": "2026-01-01T00:00:00Z",
            "license_url": "https://example.invalid/ssdu/invented-fixtures",
            "resources": [],
        }
        if path.endswith("/package_list"):
            return httpx.Response(200, json={"success": True, "result": [native["name"]]})
        if path.endswith("/package_search"):
            return httpx.Response(
                200, json={"success": True, "result": {"count": 1, "results": [native]}}
            )
        if path.endswith("/package_show") and request.url.params["id"] == dataset_id:
            return httpx.Response(200, json={"success": True, "result": native})
    if host == "statistics.gov.scot" and path == "/sparql.json":
        query = request.url.params["query"]
        if "GROUP BY ?dataset" in query:
            return httpx.Response(
                200,
                json=bindings(
                    ["dataset", "title"],
                    [
                        {
                            "dataset": term(SCOT_BASE + DATASETS["scotland"], "uri"),
                            "title": term(TITLES["scotland"]),
                        }
                    ],
                ),
            )
        if "AS ?kind" in query and DATASETS["scotland"] in query:
            return httpx.Response(200, json=scotland_metadata())
        if "GROUP BY ?value" in query and DATASETS["scotland"] in query:
            return httpx.Response(
                200,
                json=bindings(
                    ["value", "label"],
                    [
                        {
                            "value": term("http://reference.data.gov.uk/id/year/" + year, "uri"),
                            "label": term(year),
                        }
                        for year in ("2023", "2024")
                    ],
                ),
            )
    raise AssertionError(
        f"Unexpected metadata or observation request: {request.method} {request.url}"
    )


@contextmanager
def no_network(events):
    """Block accidental live connections, including DNS and raw sockets."""

    def denied(*args, **kwargs):
        events.append("network")
        raise AssertionError("This teaching example permits no network access")

    with ExitStack() as stack:
        for name in ("getaddrinfo", "create_connection"):
            stack.enter_context(patch.object(socket, name, denied))
        for name in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, denied))
        yield


class TeachingInterruption(BaseException):
    """Model stopping a process after some provider metadata has been retained."""


def mapped_records(document):
    """Compare mapped values and their native evidence independently of job IDs."""
    return {
        row["provider"]: {
            **{
                field: row.get(field)
                for field in ("description", "measures", "coverage", "licence", "access")
            },
            "mapping_version": row["mapping_version"],
            "mapping": row["source_metadata"]["mapping"],
        }
        for row in document["records"]
    }


def field_availability(document):
    """Summarize assertions; category/axis coverage never implies observed extents."""
    result = {}
    for row in document["records"]:
        coverage = row.get("coverage") or {}
        evidence = row["source_metadata"]["mapping"]["evidence"]
        result[row["provider"]] = {
            "description": bool(row.get("description")),
            "measure_count": len(row.get("measures", [])),
            "spatial_scope": (coverage.get("spatial") or {}).get("scope"),
            "temporal_scope": (coverage.get("temporal") or {}).get("scope"),
            "licence_asserted": any(item["field"] == "licence" for item in evidence),
            "metadata_url": bool((row.get("access") or {}).get("metadata_url")),
            "data_access": (row.get("access") or {}).get("data_access"),
        }
    return result


def run(output_dir):
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Choose an absent or empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    calls, network_events = [], []
    stopped = False

    def transport(request):
        nonlocal stopped
        calls.append(str(request.url))
        if not stopped and request.url.path.endswith("/filters"):
            stopped = True
            raise TeachingInterruption("Intentional interruption in the teaching example")
        return fixture_response(request)

    def client_factory(**kwargs):
        # Zero spacing is confined to invented responses, never provider traffic.
        return HttpClient(
            transport=httpx.MockTransport(transport),
            rate_limiter=RateLimiter(output / "fixture-rate.sqlite3", interval_seconds=0),
            **kwargs,
        )

    source = output / "source"
    spec = {
        "providers": {
            provider: {
                "detail": "codes",
                "page_size": 20,
                "max_pages": 2,
                "max_datasets": 10,
                "max_code_pages": 2,
            }
            for provider in DATASETS
        }
    }
    with no_network(network_events):
        sync = MetadataSync(source, client_factory=client_factory)
        plan = sync.plan_sync(spec)
        assert calls == [], "Planning must not contact a provider"
        job_id = sync.submit_plan(
            plan["plan_id"], idempotency_key="invented-five-provider-catalogue"
        )["job_id"]
        try:
            sync.run_job(job_id)
        except TeachingInterruption:
            pass
        else:
            raise AssertionError("Expected the deliberate interruption")
        interrupted_calls = list(calls)
        assert len(interrupted_calls) > 1
        assert sync.get_job(job_id)["status"] == "queued"

        # Reopen the worker, as after restarting a process; resume the same job.
        sync = MetadataSync(source, client_factory=client_factory)
        result = sync.run_job(job_id)
        assert result["status"] == "succeeded", result
        snapshot_id = result["result"]["snapshot_id"]
        # Only the interrupted request may repeat, not the retained HTTP prefix.
        assert not set(interrupted_calls[:-1]).intersection(calls[len(interrupted_calls) :])

        store = MetadataStore(source)
        document = store.read(snapshot_id)
        assert document["contract_version"] == CONTRACT_VERSION
        manifest = store.verify(snapshot_id)
        assert manifest["counts"] == {"providers": 5, "records": 5}
        assert {row["provider"] for row in document["records"]} == set(DATASETS)
        assert all(row["references"] and row["raw_resources"] for row in document["records"])
        for row in document["records"]:
            mapping = row["source_metadata"]["mapping"]
            assert mapping["version"] == row["mapping_version"] == MAPPING_VERSION
            assert mapping["evidence"]
            assert all(
                item["resource_paths"]
                and set(item["resource_paths"]).issubset(row["raw_resources"])
                for item in mapping["evidence"]
            )
            assert row["access"]["data_access"] == "not_asserted"

        # Compare both views at the same instant; freshness ages change over time.
        index_clock = datetime.now(UTC)
        index = MetadataIndex(source, clock=lambda: index_clock)
        index.import_snapshot(snapshot_id)
        matches = index.search("community", limit=20)
        assert matches["total"] == 5
        status = index.status()
        assert status["dataset_count"] == 5
        assert len(status["providers"]) == 5

        bundle = output / "invented-catalogue.ssdu-metadata.zip"
        store.export(snapshot_id, bundle)
        request_count = len(calls)
        source.rename(output / "retained-original-store")
        fresh = output / "fresh"

        # The bundle is the only source supplied to this store; block all HTTP.
        with patch.object(
            HttpClient, "request", side_effect=AssertionError("Offline HTTP attempt")
        ):
            fresh_store = MetadataStore(fresh)
            assert fresh_store.import_bundle(bundle) == snapshot_id
            assert fresh_store.verify(snapshot_id) == manifest
            assert fresh_store.read(snapshot_id) == document
            fresh_index = MetadataIndex(fresh, clock=lambda: index_clock)
            fresh_index.import_snapshot(snapshot_id)
            assert fresh_index.search("community", limit=20)["items"] == matches["items"]

            offline_sync = MetadataSync(fresh, client_factory=client_factory)
            offline_plan = offline_sync.plan_sync(spec, offline=True)
            offline_job = offline_sync.submit_plan(
                offline_plan["plan_id"], idempotency_key="invented-offline-replay"
            )
            replay = offline_sync.run_job(offline_job["job_id"])
            assert replay["status"] == "succeeded", replay
            replay_snapshot_id = replay["result"]["snapshot_id"]
            fresh_store.verify(replay_snapshot_id)
            replay_document = fresh_store.read(replay_snapshot_id)
            assert {
                (row["provider"], row["native_id"], row["title"])
                for row in replay_document["records"]
            } == {(row["provider"], row["native_id"], row["title"]) for row in document["records"]}
            assert mapped_records(replay_document) == mapped_records(document)
        assert len(calls) == request_count

    receipt = {
        "status": "passed",
        "fixture_kind": "independently_invented_teaching_metadata",
        "providers": sorted(DATASETS),
        "records": len(document["records"]),
        "snapshot_id": snapshot_id,
        "offline_snapshot_id": replay_snapshot_id,
        "contract_version": document["contract_version"],
        "mapping_version": MAPPING_VERSION,
        "job_id": job_id,
        "interruption_resumed": True,
        "retained_prefix_not_refetched": True,
        "fixture_http_requests": len(calls),
        "provider_network_requests": len(network_events),
        "observation_requests": 0,
        "search_results": matches["total"],
        "provider_reports": document["providers"],
        "normalized_field_availability": field_availability(document),
        "freshness": status["providers"],
        "fresh_store_replayed_offline": True,
        "enriched_fields_and_evidence_replayed_offline": True,
        "original_store_path_unavailable": not source.exists(),
    }
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir), indent=2))


if __name__ == "__main__":
    main()
