"""Synthetic full-inventory reconciliation and retained-cache replay checks."""

import socket

import httpx
import pytest

from ssdu.adapters import statswales
from ssdu.connector_cache import CachedClient
from ssdu.http import HttpClient


def row(number):
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "title": f"Invented catalogue table {number}",
        "first_published_at": "2040-01-01T09:00:00Z",
    }


def test_single_response_reconciles_ids_lost_at_tied_page_boundaries(tmp_path, monkeypatch):
    rows = [row(number) for number in range(1, 7)]
    calls = []

    def handler(request):
        assert request.method == "GET" and request.url.path == "/v2"
        params = dict(request.url.params)
        calls.append(params)
        size, page = int(params["page_size"]), int(params["page_number"])
        # A mutable ordering at an equal publication time can repeat an ID across
        # ordinary pages while keeping page lengths and the declared count valid.
        items = (
            rows
            if size == 1000
            else ([rows[0], rows[1], rows[2]] if page == 1 else [rows[2], rows[4], rows[5]])
        )
        return httpx.Response(200, json={"data": items, "count": 6})

    with (
        HttpClient(transport=httpx.MockTransport(handler), retries=0) as transport,
        CachedClient(tmp_path, client=transport) as client,
    ):
        first = statswales.list_datasets("", client, limit=3)
        second = statswales.list_datasets("", client, limit=3, offset=3)
        assert len({item["id"] for item in first["items"] + second["items"]}) == 5
        inventory = statswales.catalogue_inventory("", client)
    assert inventory["inventory_complete"] is True
    assert inventory["observed_distinct"] == inventory["total"] == 6
    assert inventory["next_offset"] is None
    assert inventory["duplicate_count"] == 0 and inventory["errors"] == []
    assert len(calls) == 3

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline inventory attempted network access")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(HttpClient, "get", forbidden)
    with CachedClient(tmp_path, offline=True) as client:
        assert statswales.catalogue_inventory("", client) == inventory
    assert len(calls) == 3


def test_inventory_larger_than_bound_is_explicitly_incomplete():
    def handler(request):
        assert dict(request.url.params)["page_size"] == "3"
        return httpx.Response(200, json={"data": [row(1), row(2), row(3)], "count": 7})

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        result = statswales.catalogue_inventory("", client, max_records=3)
    assert result["total"] == 7 and result["observed_distinct"] == 3
    assert result["next_offset"] == 3
    assert result["inventory_complete"] is False
    assert result["reconciliation"]["atomic_provider_snapshot"] is False


@pytest.mark.parametrize(
    ("rows", "total", "code"),
    [([row(1), row(1)], 2, "duplicate_ids"), ([row(1)], 2, "declared_count_mismatch")],
)
def test_duplicate_or_inconsistent_inventory_cannot_claim_complete(rows, total, code):
    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": rows, "count": total})
        ),
        retries=0,
    ) as client:
        result = statswales.catalogue_inventory("", client, max_records=3)
    assert result["inventory_complete"] is False
    assert code in {item["code"] for item in result["errors"]}
    assert len({item["id"] for item in result["items"]}) == len(result["items"])


def test_unknown_total_requires_a_short_single_response_to_claim_exhaustion():
    with HttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [row(1), row(2)]})
        ),
        retries=0,
    ) as client:
        full = statswales.catalogue_inventory("", client, max_records=2)
        short = statswales.catalogue_inventory("", client, max_records=3)
    assert full["inventory_complete"] is False and full["next_offset"] == 2
    assert short["inventory_complete"] is True and short["next_offset"] is None
    assert short["total"] is None


def test_inventory_query_uses_search_endpoint_without_observation_requests():
    def handler(request):
        assert request.url.path == "/v2/search"
        assert dict(request.url.params) == {
            "lang": "en-gb",
            "page_size": "3",
            "page_number": "1",
            "keywords": "invented",
            "mode": "basic",
        }
        return httpx.Response(200, json={"data": [row(1)], "count": 1})

    with HttpClient(transport=httpx.MockTransport(handler), retries=0) as client:
        assert statswales.catalogue_inventory(" invented ", client, max_records=3)[
            "inventory_complete"
        ]


@pytest.mark.parametrize("bound", [0, 1001, True, "1000"])
def test_inventory_bounds_fail_before_http(bound):
    with pytest.raises(ValueError, match="1 to 1000"):
        statswales.catalogue_inventory("", None, max_records=bound)
