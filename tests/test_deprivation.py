"""Invented observations shaped like IoD File 7, including missing-value tokens.

Protocol headings and geographic IDs are retained only to exercise source-schema
and selector handling. All observation cells and LSOA labels are independently
invented; these rows are not official statistics.
"""

import httpx
import pandas as pd
import pytest

from ssdu.adapters import deprivation
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError


@pytest.fixture
def iod_csv():
    # All measurements are invented. The second selected score is a suppression
    # token so an unavailable measure cannot silently become zero or disappear.
    return (
        b"LSOA code (2021),LSOA name (2021),Local Authority District code (2024),"
        b"Local Authority District name (2024),Index of Multiple Deprivation (IMD) Score,"
        b"Index of Multiple Deprivation (IMD) Rank (where 1 is most deprived),"
        b"Index of Multiple Deprivation (IMD) Decile (where 1 is most deprived 10% of LSOAs),"
        b"Health Deprivation and Disability Score,Total population: mid 2022\n"
        b"E01000001,Synthetic area A,E09000001,City of London,9.625,31234,9,-2.125,1444\n"
        b"E01017013,Synthetic area B,E06000044,Portsmouth,12.375,16432,5,0.625,2333\n"
        b"E01027305,Synthetic area C,E06000062,West Northamptonshire,"
        b"25.875,28314,2,-0.375,2111\n"
        b"E01017014,Synthetic area D,E06000044,Portsmouth,[c],16433,5,,2334\n"
    )


def client_for(body):
    return HttpClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=body, headers={"content-type": "text/csv"})
        )
    )


def test_complete_lad_subset_preserves_codes_measures_flags_and_raw(iod_csv):
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
    )
    with client_for(iod_csv) as client:
        product = deprivation.fetch(request, client)
        assert client.resources[0].body == iod_csv
    frame = product.frame
    assert frame["lsoa_code"].tolist() == ["E01017013", "E01017014"]
    assert frame["local_authority_code"].tolist() == ["E06000044", "E06000044"]
    assert str(frame["lsoa_code"].dtype) == "string"
    assert frame.loc[0, "imd_score"] == pytest.approx(12.375)
    assert frame.loc[0, "imd_rank"] == 16432
    assert frame.loc[0, "health_deprivation_and_disability_score"] == pytest.approx(0.625)
    assert frame.loc[0, "total_population_mid_2022"] == 2333
    assert pd.isna(frame.loc[1, "imd_score"])
    assert frame.loc[1, "imd_score_flag"] == "[c]"
    assert pd.isna(frame.loc[1, "health_deprivation_and_disability_score"])
    assert product.metadata["raw_rows"] == 4
    assert product.metadata["product_rows"] == 2
    assert product.metadata["complete_selection"] is True
    assert product.release["lad_geography"] == "2024"
    assert product.release["population_reference_period"] == "mid-2022"
    assert product.licence.endswith("/version/3/")


def test_default_request_fetches_all_retained_england_rows(iod_csv):
    request = deprivation.default_request()
    assert request == deprivation.national_request()
    assert request.params == {"scope": "england"}
    with client_for(iod_csv) as client:
        product = deprivation.fetch(request, client)
        assert len(client.resources) == 1
    assert len(product.frame) == 4
    assert set(product.frame["local_authority_code"]) == {
        "E09000001",
        "E06000044",
        "E06000062",
    }
    assert product.metadata["product_scope"] == "Complete England File 7 release"
    assert product.metadata["product_rows"] == product.metadata["raw_rows"] == 4


def test_curated_search_uses_national_release_metadata():
    with client_for(b"") as client:
        assert deprivation.search("England IMD", client)[0]["id"] == deprivation.DATASET_ID
        assert deprivation.search("Portsmouth", client) == []
        assert client.resources == []


def test_corrected_lad_allocation_is_selectable(iod_csv):
    request = Request(
        "deprivation",
        "english-indices-of-deprivation-2025",
        {"local_authority_codes": ["E06000062"]},
    )
    with client_for(iod_csv) as client:
        product = deprivation.fetch(request, client)
    assert product.frame["lsoa_code"].tolist() == ["E01027305"]
    assert product.frame["local_authority_name"].tolist() == ["West Northamptonshire"]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"local_authority_codes": "E06000044"},
        {"local_authority_codes": []},
        {"local_authority_codes": [6000044]},
        {"local_authority_codes": ["Portsmouth"]},
        {"local_authority_codes": ["E01017013"]},
        {"local_authority_codes": ["E06000044", "E06000044"]},
        {"local_authority_codes": ["E06000044"], "year": "2019"},
    ],
)
def test_invalid_selector_fails_before_network(params):
    with client_for(b"") as client:
        with pytest.raises(RetrievalError):
            deprivation.fetch(Request("deprivation", deprivation.DATASET_ID, params), client)
        assert client.resources == []


def test_row_limit_rejects_whole_selection_instead_of_truncating(iod_csv):
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}, max_rows=1
    )
    with client_for(iod_csv) as client:
        with pytest.raises(RetrievalError, match="Complete LAD selection has 2 rows"):
            deprivation.fetch(request, client)
        assert client.resources[0].body == iod_csv


def test_unmatched_lad_fails_without_empty_or_partial_product(iod_csv):
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044", "E06099999"]}
    )
    with client_for(iod_csv) as client, pytest.raises(RetrievalError, match="E06099999"):
        deprivation.fetch(request, client)


def test_html_or_schema_drift_fails_closed():
    with (
        client_for(b"<html>Service unavailable</html>") as client,
        pytest.raises(RetrievalError, match="schema"),
    ):
        deprivation.fetch(deprivation.default_request(), client)
