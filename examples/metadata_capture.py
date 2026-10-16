"""Capture five full teaching catalogues and transfer them for offline discovery.

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
from ssdu.metadata_capture import MetadataCapture
from ssdu.metadata_contract import CONTRACT_VERSION
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_mapping import MAPPING_VERSION
from ssdu.metadata_store import MetadataStore
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
            "organization": {
                "id": "invented-community-office",
                "title": "Invented community office",
            },
            "theme": ["Community teaching"],
            "language": ["en"],
            "geographical_coverage": ["Invented teaching region"],
            "reference_period": [{"start": "2023-01-01", "end": "2024-12-31"}],
            "metadata": {"json": "https://example.invalid/ssdu/invented-metadata.json"},
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


SECOND_DATASETS = {
    "nomis": "NM_999_3",
    "statswales": "00000000-0000-4000-8000-000000000002",
    "mhclg": "ssdu-invented-rural",
    "nisra": "SYNTH_RURAL",
    "scotland": "ckan:00000000-0000-4000-8000-000000000006",
}


def second_record(value, provider):
    """Make a second invented record, never derived from provider content."""
    if isinstance(value, list):
        return [second_record(item, provider) for item in value]
    if isinstance(value, dict):
        return {key: second_record(item, provider) for key, item in value.items()}
    if isinstance(value, str):
        if provider == "scotland":
            value = value.replace("ssdu-invented-community", "ssdu-invented-rural-community")
        value = value.replace(
            DATASETS[provider].removeprefix("ckan:"),
            SECOND_DATASETS[provider].removeprefix("ckan:"),
        )
        return value.replace(TITLES[provider], TITLES[provider] + " in invented rural communities")
    return value


def full_fixture_response(request):
    """Serve two invented catalogue entries per service, honoring native paging."""
    host, path = request.url.host, request.url.path
    params = request.url.params
    if host == "www.nomisweb.co.uk" and path == "/api/v01/dataset/def.sdmx.json":
        native = nomis_definition()
        row = native["structure"]["keyfamilies"]["keyfamily"][0]
        native["structure"]["keyfamilies"]["keyfamily"].append(second_record(row, "nomis"))
        return httpx.Response(200, json=native)
    if host == "api.stats.gov.wales" and path in ("/v2", "/v2/search"):
        rows = [{"id": DATASETS["statswales"], "title": TITLES["statswales"]}]
        rows.append(second_record(rows[0], "statswales"))
        size, page = int(params["page_size"]), int(params["page_number"])
        return httpx.Response(
            200, json={"data": rows[(page - 1) * size : page * size], "total": len(rows)}
        )
    if host == "www.gov.uk" and path == "/api/search.json":
        rows = [{"title": TITLES["mhclg"], "link": "/government/statistics/" + DATASETS["mhclg"]}]
        rows.append(second_record(rows[0], "mhclg"))
        start, size = int(params["start"]), int(params["count"])
        return httpx.Response(200, json={"results": rows[start : start + size], "total": len(rows)})
    if host == "ws-data.nisra.gov.uk" and path.endswith("ReadCollection"):
        native = fixture_response(request).json()
        rows = native["link"]["item"]
        rows.append(second_record(rows[0], "nisra"))
        return httpx.Response(200, json=native)
    if host == "api.data.gov.scot" and path.endswith("/package_list"):
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": ["ssdu-invented-community", "ssdu-invented-rural-community"],
            },
        )
    if host == "api.data.gov.scot" and path.endswith("/package_search"):
        native = fixture_response(request).json()
        rows = native["result"]["results"]
        rows.append(second_record(rows[0], "scotland"))
        start, size = int(params["start"]), int(params["rows"])
        native["result"] = {"count": len(rows), "results": rows[start : start + size]}
        return httpx.Response(200, json=native)

    for provider, dataset_id in SECOND_DATASETS.items():
        native_id = dataset_id.removeprefix("ckan:")
        if native_id not in str(request.url):
            continue
        url = str(request.url).replace(native_id, DATASETS[provider].removeprefix("ckan:"))
        native = second_record(
            fixture_response(httpx.Request(request.method, url)).json(), provider
        )
        if provider == "mhclg":
            # Catalogue coverage includes an associated publication whose primary
            # publisher and file format fall outside the download adapter policy.
            native["links"]["primary_publishing_organisation"] = [
                {
                    "base_path": "/government/organisations/department-for-culture-media-sport",
                    "title": "Invented associated publishing department",
                }
            ]
            attachment = native["details"]["attachments"][0]
            attachment["url"] = attachment["url"].replace(".csv", ".xlsx")
            attachment["content_type"] = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        if provider == "scotland":
            native["result"]["resources"] = [
                {
                    "id": "invented-workbook",
                    "url": "https://example.invalid/ssdu/invented-events.xlsx",
                    "format": "XLSX",
                    "name": "Invented event workbook",
                }
            ]
        return httpx.Response(200, json=native)
    return fixture_response(request)


def normalized_values(matches):
    """Compare values, native evidence and source clocks independently of capture IDs."""
    fields = (
        "description",
        "publishers",
        "subjects",
        "languages",
        "related",
        "dimensions",
        "measures",
        "coverage",
        "licence",
        "access",
        "distributions",
        "field_status",
        "mapping_version",
        "last_checked_at",
        "provider_modified_at",
    )
    return {
        row["dataset_id"]: {
            **{field: row.get(field) for field in fields},
            "mapping": row["source_metadata"]["mapping"],
            "native": row["source_metadata"]["descriptor"],
        }
        for row in matches["items"]
    }


def run(output_dir):
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Choose an absent or empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    calls, network_events = [], []

    def transport(request):
        calls.append(str(request.url))
        return full_fixture_response(request)

    def client_factory(**kwargs):
        # Zero spacing only applies to in-memory teaching responses.
        return HttpClient(
            transport=httpx.MockTransport(transport),
            rate_limiter=RateLimiter(output / "fixture-rate.sqlite3", interval_seconds=0),
            **kwargs,
        )

    source = output / "source"
    spec = {
        "providers": {
            provider: {"detail": "dataset", "page_size": 1, "max_pages": 4, "max_datasets": 10}
            for provider in DATASETS
        }
    }
    with no_network(network_events):
        capture = MetadataCapture(source, client_factory=client_factory)
        plan = capture.plan_capture(spec, batch_size=1)
        assert calls == [], "Planning must not contact providers"
        job = capture.submit_plan(plan["plan_id"], "invented-full-five-provider-catalogue")
        paused = capture.run_job(job["job_id"], max_batches=1)
        assert paused["status"] == "queued", paused
        assert calls, "The first inventory batch must retain metadata"
        retained_calls = list(calls)
        capture = MetadataCapture(source, client_factory=client_factory)
        result = capture.run_job(job["job_id"])
        assert result["status"] == "succeeded", result
        capture_id = result["result"]["capture_id"]
        report = capture.report(capture_id)
        before_count = len(calls)
        assert not set(retained_calls).intersection(calls[len(retained_calls) :])
        source_store = MetadataStore(source)
        snapshots = source_store.list(limit=100)["rows"]
        for snapshot in snapshots:
            source_store.verify(snapshot["snapshot_id"])
        index_clock = datetime.now(UTC)
        index = MetadataIndex(source, clock=lambda: index_clock)
        matches = index.search("community", limit=100)
        assert matches["total"] == 10, matches
        before = {(row["provider"], row["native_id"], row["title"]) for row in matches["items"]}
        normalized = normalized_values(matches)
        providers = report["report"]["providers"]
        assert all(
            row["catalogue_records"] == row["described_records"] == 2 for row in providers.values()
        )
        assert all(
            row["catalogue_complete"] and row["descriptions_complete"] for row in providers.values()
        )
        assert all(row["all_provider_holdings_percent"] is None for row in providers.values())
        bundle = output / "invented-full-catalogue.ssdu-metadata.zip"
        capture.export_capture(capture_id, bundle)
        source.rename(output / "retained-original-store")
        fresh = output / "fresh"
        with patch.object(
            HttpClient, "request", side_effect=AssertionError("Offline HTTP attempt")
        ):
            fresh_capture = MetadataCapture(fresh, client_factory=client_factory)
            imported = fresh_capture.import_capture(bundle)
            assert imported["capture_id"] == capture_id, imported
            assert fresh_capture.report(capture_id) == report
            fresh_store = MetadataStore(fresh)
            for snapshot in fresh_store.list(limit=100)["rows"]:
                fresh_store.verify(snapshot["snapshot_id"])
            fresh_index = MetadataIndex(fresh, clock=lambda: index_clock)
            current = fresh_index.search("community", limit=100)
            assert {
                (row["provider"], row["native_id"], row["title"]) for row in current["items"]
            } == before
            assert normalized_values(current) == normalized
            offline_plan = fresh_capture.plan_capture(spec, offline=True, batch_size=1)
            offline_job = fresh_capture.submit_plan(
                offline_plan["plan_id"], "invented-full-offline-replay"
            )
            replay = fresh_capture.run_job(offline_job["job_id"])
            assert replay["status"] == "succeeded", replay
            after = fresh_index.search("community", limit=100)
            assert {
                (row["provider"], row["native_id"], row["title"]) for row in after["items"]
            } == before
            assert normalized_values(after) == normalized
            fresh_index.rebuild()
            assert normalized_values(fresh_index.search("community", limit=100)) == normalized
        assert len(calls) == before_count

    receipt = {
        "status": "passed",
        "fixture_kind": "independently_invented_teaching_metadata",
        "providers": sorted(DATASETS),
        "records": len(before),
        "capture_id": capture_id,
        "offline_capture_id": replay["result"]["capture_id"],
        "contract_version": CONTRACT_VERSION,
        "mapping_version": MAPPING_VERSION,
        "job_id": job["job_id"],
        "interruption_resumed": True,
        "retained_prefix_not_refetched": True,
        "fixture_http_requests": len(calls),
        "provider_network_requests": len(network_events),
        "observation_requests": 0,
        "search_results": matches["total"],
        "report": report,
        "fresh_store_replayed_offline": True,
        "normalized_fields_and_evidence_equal": True,
        "index_rebuilt_offline": True,
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
