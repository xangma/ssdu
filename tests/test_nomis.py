import csv
import io

import httpx
import pytest

from ssdu.adapters.nomis import default_request, fetch, search
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError


def definition(revision="2026-09-15"):
    return {
        "structure": {
            "keyfamilies": {
                "keyfamily": [
                    {
                        "id": "NM_162_1",
                        "name": {"value": "Claimant count"},
                        "components": {"dimension": [{"conceptref": "GEOGRAPHY"}]},
                        "annotations": {
                            "annotation": [
                                {"annotationtitle": "LastRevised", "annotationtext": revision}
                            ]
                        },
                    }
                ]
            }
        }
    }


def page(rows, total=3):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        ["DATE", "GEOGRAPHY_CODE", "OBS_VALUE", "OBS_STATUS", "RECORD_COUNT", "RECORD_OFFSET"]
    )
    for offset, value, status in rows:
        writer.writerow(["2026-08", "K02000001", value, status, total, offset])
    return stream.getvalue()


def test_complete_pagination_preserves_missing_values_and_codes():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=definition())
        offset = int(request.url.params["recordoffset"])
        text = page([(0, "001", "A"), (1, "..", "S")]) if offset == 0 else page([(2, "0", "A")])
        return httpx.Response(200, text=text)

    with HttpClient(transport=httpx.MockTransport(handler)) as client:
        request = Request(**{**default_request().to_dict(), "page_size": 2})
        result = fetch(request, client)
    assert len(result.frame) == 3
    assert result.frame["GEOGRAPHY_CODE"].tolist() == ["K02000001"] * 3
    assert result.frame["OBS_VALUE"].tolist() == ["001", "..", "0"]
    assert result.frame["OBS_STATUS"].tolist() == ["A", "S", "A"]
    assert result.frame["OBS_VALUE_NUMERIC"].isna().tolist() == [False, True, False]
    assert result.metadata["provider_total_rows"] == 3
    assert len(calls) == 4


def test_premature_end_is_not_complete():
    def handler(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=definition())
        return httpx.Response(
            200,
            text=page([(0, "12", "A")]) if request.url.params["recordoffset"] == "0" else page([]),
        )

    with (
        HttpClient(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RetrievalError, match="before completion"),
    ):
        fetch(default_request(), client)


def test_revision_change_aborts():
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path.endswith(".json"):
            count += 1
            return httpx.Response(200, json=definition(str(count)))
        return httpx.Response(200, text=page([(0, "12", "A")], total=1))

    with (
        HttpClient(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RetrievalError, match="revision changed"),
    ):
        fetch(default_request(), client)


def test_unsupported_filter_rejected():
    with (
        HttpClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=definition()))
        ) as c,
        pytest.raises(ValueError, match="Unsupported Nomis"),
    ):
        fetch(Request("nomis", "NM_162_1", {"geography": "1", "date": "latest", "typo": 1}), c)


def test_search_no_matches_is_empty():
    with HttpClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"structure": {"header": {"id": "none"}, "keyfamilies": None}}
            )
        )
    ) as client:
        assert search("unfindable", client) == []
