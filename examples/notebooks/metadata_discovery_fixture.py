"""Invented metadata for the adjacent lesson; no provider or observation requests.

Download beside metadata_discovery.ipynb. All responses are independently invented
teaching material. An unexpected endpoint fails rather than reaching the network.
"""

from pathlib import Path

import httpx

from ssdu.http import HttpClient
from ssdu.metadata_sync import MetadataSync
from ssdu.rate_limit import RateLimiter

PUBLICATION = "/government/statistics/ssdu-invented-community-buildings"
SCOTTISH_ID = "00000000-0000-4000-8000-000000000019"


def fixture_response(request):
    """Supply one invented catalogue entry per service, with metadata only."""
    assert request.method == "GET"
    if request.url.host == "www.gov.uk":
        if request.url.path == "/api/search.json":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "results": [{"title": "Invented community buildings", "link": PUBLICATION}],
                },
            )
        if request.url.path == "/api/content" + PUBLICATION:
            return httpx.Response(
                200,
                json={
                    "base_path": PUBLICATION,
                    "content_id": "ssdu-invented-community-buildings",
                    "title": "Invented community buildings",
                    "description": "Counts of fictional community buildings, for a teaching lesson.",
                    "public_updated_at": "2026-01-01T00:00:00Z",
                    "links": {
                        "organisations": [
                            {
                                "base_path": "/government/organisations/ministry-of-housing-communities-local-government"
                            }
                        ]
                    },
                    "details": {
                        "licence": "https://example.invalid/invented-teaching-terms",
                        "national_applicability": {
                            "invented-region": {"applicable": True, "label": "Invented region"}
                        },
                        "attachments": [
                            {
                                "id": "invented-buildings-csv",
                                "title": "Invented community buildings CSV",
                                "url": "https://assets.publishing.service.gov.uk/media/ssdu-invented/buildings.csv",
                                "content_type": "text/csv",
                            }
                        ],
                    },
                },
            )
    if request.url.host == "api.data.gov.scot":
        dataset = {
            "id": SCOTTISH_ID,
            "name": "ssdu-invented-community-events",
            "title": "Invented community events",
            "description": "Descriptions of fictional community events, for a teaching lesson.",
            "metadata_modified": "2026-01-02T00:00:00Z",
            "resources": [],
        }
        if request.url.path.endswith("/package_search"):
            return httpx.Response(
                200, json={"success": True, "result": {"count": 1, "results": [dataset]}}
            )
        if request.url.path.endswith("/package_show"):
            assert request.url.params["id"] == SCOTTISH_ID
            return httpx.Response(200, json={"success": True, "result": dataset})
    raise AssertionError(f"Unexpected request in invented metadata lesson: {request.url}")


def create_teaching_catalogue(root):
    """Retain and normalize two invented descriptions using the real sync workflow."""
    root = Path(root)
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return fixture_response(request)

    def client_factory(**kwargs):
        return HttpClient(
            transport=httpx.MockTransport(respond),
            # Zero delay is for these in-memory responses, never real providers.
            rate_limiter=RateLimiter(root / "fixture-rate.sqlite3", interval_seconds=0),
            **kwargs,
        )

    specification = {
        "providers": {
            provider: {"detail": "dataset", "page_size": 10, "max_pages": 1, "max_datasets": 10}
            for provider in ("mhclg", "scotland")
        }
    }
    sync = MetadataSync(root, client_factory=client_factory)
    plan = sync.plan_sync(specification)
    job = sync.submit_plan(plan["plan_id"], "invented-metadata-discovery-lesson")
    result = sync.run_job(job["job_id"])
    assert result["status"] == "succeeded", result
    return {"snapshot_id": result["result"]["snapshot_id"], "mock_requests": requests}
