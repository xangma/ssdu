"""Invented Nomis responses for the provider-selection lesson; no network is used.

Protocol shape: https://www.nomisweb.co.uk/api/v01/help
These invented IDs, areas and values are not provider metadata or statistics.
"""

import csv
import io

import httpx

from ssdu.http import HttpClient
from ssdu.rate_limit import RateLimiter

DATASET_ID = "NM_999_1"
CODES = {
    "geography": [("001", "Example North"), ("002", "Example South")],
    "time": [("2024", "2024")],
    "measures": [("001", "Invented dwelling count")],
}
DEFINITION = {
    "structure": {
        "keyfamilies": {
            "keyfamily": [
                {
                    "id": DATASET_ID,
                    "name": {"value": "Invented housing counts"},
                    "components": {"dimension": [{"conceptref": name.upper()} for name in CODES]},
                    "annotations": {
                        "annotation": [
                            {"annotationtitle": "Units", "annotationtext": "dwellings"},
                            {"annotationtitle": "LastRevised", "annotationtext": "teaching-1"},
                        ]
                    },
                }
            ]
        }
    }
}


def respond(request):
    """Fail closed if teaching code asks for any unrecognised request."""
    if request.method != "GET" or request.url.host != "www.nomisweb.co.uk":
        raise AssertionError(f"Unexpected teaching request: {request.method} {request.url}")
    base = "/api/v01/dataset"
    if request.url.path in {f"{base}/def.sdmx.json", f"{base}/{DATASET_ID}/def.sdmx.json"}:
        return httpx.Response(200, json=DEFINITION)
    for dimension, values in CODES.items():
        if request.url.path == f"{base}/{DATASET_ID}/{dimension}.def.sdmx.json":
            codes = [{"value": value, "description": {"value": label}} for value, label in values]
            return httpx.Response(
                200, json={"structure": {"codelists": {"codelist": {"code": codes}}}}
            )
    if request.url.path == f"{base}/{DATASET_ID}.data.csv":
        params = request.url.params
        assert params["geography"] == "001"
        assert params["date"] == "2024"
        assert params["measures"] == "001"
        assert params["setallwherenotspecified"] == "false"
        assert params["recordlimit"] == "2"
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "DATE",
                "GEOGRAPHY_CODE",
                "GEOGRAPHY_NAME",
                "MEASURES_CODE",
                "MEASURES_NAME",
                "OBS_VALUE",
                "UNIT_NAME",
                "OBS_STATUS",
                "RECORD_COUNT",
                "RECORD_OFFSET",
            ]
        )
        if int(params["recordoffset"]) == 0:
            writer.writerow(
                [
                    "2024",
                    "001",
                    "Example North",
                    "001",
                    "Invented dwelling count",
                    12,
                    "dwellings",
                    "A",
                    1,
                    0,
                ]
            )
        return httpx.Response(200, text=output.getvalue(), headers={"content-type": "text/csv"})
    raise AssertionError(f"Unexpected teaching request: {request.method} {request.url}")


def teaching_client(workspace):
    """Use the normal HTTP accounting with an in-memory, synthetic transport.

    The private zero-delay limiter is for this mock only; live defaults are untouched.
    """
    return HttpClient(
        transport=httpx.MockTransport(respond),
        retries=0,
        rate_limiter=RateLimiter(workspace / "teaching-rate.sqlite3", interval_seconds=0),
    )
