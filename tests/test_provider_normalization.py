"""Pure transform conformance: source identities and semantics survive conversion."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pytest

from ssdu.adapters import deprivation as iod
from ssdu.dataset_contract import Dataset
from ssdu.models import Acquisition, Request, RetrievalError
from ssdu.normalization import TRANSFORM_VERSION, normalize


def acquisition(provider, frame, metadata, *, release=None):
    return Acquisition(
        provider=provider,
        dataset_id="NM_1_1" if provider == "nomis" else "test-data",
        title="Source-bound synthetic transform fixture",
        frame=frame,
        metadata=metadata,
        release=release or {"revision": "fixture-1"},
        licence="https://example.invalid/licence",
        citation="Synthetic fixture; not official statistics.",
        source_url="https://example.invalid/source",
    )


def nomis_acquisition():
    frame = pd.DataFrame(
        {
            "DATE": ["2025-01"] * 5,
            "DATE_CODE": ["2025-01"] * 5,
            "DATE_NAME": ["January 2025"] * 5,
            "GEOGRAPHY_CODE": ["E00000001"] * 5,
            "GEOGRAPHY_NAME": ["Synthetic area"] * 5,
            "GEOGRAPHY_TYPE": ["Fixture geography"] * 5,
            "SEX_CODE": ["001", "001", "002", "002", "002"],
            "SEX_NAME": ["Group A", "Group A", "Group B", "Group B", "Group B"],
            "MEASURE_CODE": ["01"] * 5,
            "MEASURE_NAME": ["Synthetic observation"] * 5,
            "MEASURES": ["20100", "20100", "20100", "20130", "20130"],
            "MEASURES_NAME": ["Value", "Value", "Value", "Percentage", "Percentage"],
            "OBS_VALUE": ["001", "..", "0", "12.5", "1,23"],
            "OBS_STATUS": ["A", "S", "A", "A", "S"],
            "OBS_STATUS_NAME": ["Normal", "Suppressed", "Normal", "Normal", "Suppressed"],
            "OBS_CONF": ["F"] * 5,
            "OBS_VALUE_NUMERIC": [1, None, 0, 12.5, None],
        }
    )
    metadata = {
        "definition": {
            "structure": {
                "keyfamilies": {
                    "keyfamily": [
                        {
                            "components": {
                                "dimension": [
                                    {"conceptref": name}
                                    for name in ("GEOGRAPHY", "SEX", "MEASURE", "MEASURES")
                                ]
                            },
                            "annotations": {
                                "annotation": [
                                    {"annotationtitle": "Units", "annotationtext": "Persons"}
                                ]
                            },
                        }
                    ]
                }
            }
        },
        "provider_total_rows": len(frame),
    }
    return acquisition("nomis", frame, metadata)


def wales_acquisition():
    frame = pd.DataFrame(
        {
            "Area": ["Synthetic area", "Repeated", "Unknown"],
            "Year": ["2025", "2025", "2025"],
            "Data values": ["   001", "*", "0"],
            "Notes": [None, "suppressed", "provisional"],
            "Area_sort": [1, 2, 3],
        }
    )
    metadata = {
        "dimensions": [
            {
                "columnName": "Area",
                "factTableColumn": "AreaID",
                "values": [
                    {"reference": "001", "description": "Synthetic area"},
                    {"reference": "002", "description": "Repeated"},
                    {"reference": "003", "description": "Repeated"},
                ],
            },
            {
                "columnName": "Year",
                "factTableColumn": "YearID",
                "values": [{"reference": "2025", "description": "2025"}],
            },
        ],
        "provider_row_count": 3,
    }
    return acquisition("statswales", frame, metadata)


def diagnostics(dataset, code):
    return [entry for entry in dataset.descriptor.quality.diagnostics if entry.code == code]


def test_nomis_measure_representations_codes_units_flags_and_source_are_preserved():
    acquired = nomis_acquisition()
    before = acquired.frame.copy(deep=True)
    request = Request("nomis", "NM_1_1", {"geography": "001", "date": "2025-01"})
    dataset = normalize(acquired, request)
    observed = dataset.to_arrow().to_pylist()
    assert dataset.descriptor.dataset_id == "nomis:NM_1_1"
    assert dataset.descriptor.source.dataset_id == "NM_1_1"
    assert dataset.descriptor.source.effective_request == request.to_dict()
    assert dataset.descriptor.transform.version == TRANSFORM_VERSION
    assert [row["value"] for row in observed] == [1.0, None, 0.0, 12.5, None]
    assert [row["value_raw"] for row in observed] == ["001", "..", "0", "12.5", "1,23"]
    assert [row["status"] for row in observed] == ["A", "S", "A", "A", "S"]
    assert [row["sex"] for row in observed] == ["001", "001", "002", "002", "002"]
    assert observed[0]["unit"] == "Persons"
    assert observed[3]["unit"] is None  # Dataset-level Persons cannot define a percentage.
    assert observed[0]["measure"] != observed[3]["measure"]
    assert observed[0]["geography"] == "E00000001"
    assert observed[0]["period"] == "2025-01"
    assert dataset.descriptor.dimensions[1].vintage is None
    assert dataset.to_arrow("source").column("OBS_CONF").to_pylist() == ["F"] * 5
    assert diagnostics(dataset, "non_numeric_value")[0].count == 2
    assert diagnostics(dataset, "unknown_unit")[0].count == 2
    assert dataset.descriptor.quality.completeness == "complete"
    pd.testing.assert_frame_equal(acquired.frame, before)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.5", 1234.5),
        (" -1.25e2 ", -125.0),
        (".5", 0.5),
        ("1,23", None),
        ("NaN", None),
        ("inf", None),
        ("1e999", None),
        ("12%", None),
        ("[c]", None),
        ("", None),
        (None, None),
    ],
)
def test_numeric_parsing_is_conservative_and_keeps_the_exact_source_token(raw, expected):
    acquired = nomis_acquisition()
    acquired.frame = acquired.frame.iloc[:1].copy()
    acquired.frame.loc[0, "OBS_VALUE"] = raw
    row = normalize(acquired).to_arrow().to_pylist()[0]
    assert row["value"] == expected
    assert row["value_raw"] == raw


def test_missing_nomis_dimension_is_unknown_with_diagnostic():
    acquired = nomis_acquisition()
    acquired.frame.loc[1, "SEX_CODE"] = ""
    dataset = normalize(acquired)
    assert dataset.to_arrow().column("sex").to_pylist()[1] is None
    assert diagnostics(dataset, "unknown_dimension")[0].column == "sex"
    assert dataset.to_arrow("source").column("SEX_CODE").to_pylist()[1] == ""


def test_wales_unknown_and_ambiguous_codes_are_not_invented():
    acquired = wales_acquisition()
    dataset = normalize(acquired)
    observed = dataset.to_arrow().to_pylist()
    assert [row["area"] for row in observed] == ["001", None, None]
    assert [row["value"] for row in observed] == [1, None, 0]
    assert [row["value_raw"] for row in observed] == ["   001", "*", "0"]
    assert [row["notes"] for row in observed] == [None, "suppressed", "provisional"]
    assert [row["unit"] for row in observed] == [None] * 3
    assert {dimension.id: dimension.kind for dimension in dataset.descriptor.dimensions} == {
        "area": "geography",
        "year": "time",
    }
    assert diagnostics(dataset, "unknown_dimension")[0].count == 2
    assert dataset.to_arrow("source").column("Area_sort").to_pylist() == [1, 2, 3]


def test_wales_explicit_measure_dimension_creates_distinct_source_variables():
    acquired = wales_acquisition()
    acquired.frame["Indicator"] = ["Number", "Percentage", "Number"]
    acquired.metadata["dimensions"].append(
        {
            "columnName": "Indicator",
            "factTableColumn": "MeasureID",
            "values": [
                {"reference": "N", "description": "Number"},
                {"reference": "P", "description": "Percentage"},
            ],
        }
    )
    dataset = normalize(acquired)
    assert dataset.to_arrow().column("measure").to_pylist() == ["N", "P", "N"]
    assert {variable.id: variable.label for variable in dataset.descriptor.variables} == {
        "N": "Number",
        "P": "Percentage",
    }
    assert "indicator" not in dataset.to_arrow().column_names
    assert dataset.to_arrow("source").column("Indicator").to_pylist() == [
        "Number",
        "Percentage",
        "Number",
    ]


def test_wales_ambiguous_measure_identity_remains_an_explicit_unknown_dimension():
    acquired = wales_acquisition()
    acquired.frame["Indicator"] = ["Number"] * 3
    acquired.metadata["dimensions"].append(
        {
            "columnName": "Indicator",
            "factTableColumn": "MeasureID",
            "values": [
                {"reference": "N1", "description": "Number"},
                {"reference": "N2", "description": "Number"},
            ],
        }
    )
    dataset = normalize(acquired)
    assert dataset.to_arrow().column("indicator").to_pylist() == [None] * 3
    assert diagnostics(dataset, "unknown_measure")[0].count == 3


def test_wales_recovered_codes_cannot_override_source_metadata():
    acquired = wales_acquisition()
    acquired.frame["Area__code"] = ["wrong", None, None]
    with pytest.raises(RetrievalError, match="disagree"):
        normalize(acquired)


def test_wales_verified_empty_selection_has_typed_empty_observations():
    acquired = wales_acquisition()
    acquired.frame = pd.DataFrame()
    acquired.metadata["provider_row_count"] = 0
    dataset = normalize(acquired)
    assert dataset.to_arrow().num_rows == 0
    assert dataset.to_arrow().schema.field("value").type == pa.float64()
    assert dataset.descriptor.quality.completeness == "complete"
    assert dataset.descriptor.quality.source_rows == 0
    assert [table.name for table in dataset.descriptor.tables] == ["observations"]


def deprivation_acquisition():
    path = Path(__file__).parents[1] / "examples/fixtures/dataset-contract/deprivation.csv"
    body = path.read_bytes()
    product = iod.from_resource(
        iod.national_request(), SimpleNamespace(body=body, url=iod.SOURCE_URL), national=True
    )
    return product, pd.read_csv(path, dtype="string", keep_default_na=False)


def test_deprivation_all_measures_vintages_and_raw_tokens_survive():
    acquired, raw = deprivation_acquisition()
    dataset = normalize(acquired, iod.national_request(), raw)
    observed = dataset.to_arrow().to_pylist()
    assert len(observed) == 15
    assert len(dataset.descriptor.variables) == 5
    assert {variable.id for variable in dataset.descriptor.variables} == {
        value
        for key, value in acquired.metadata["column_map"].items()
        if key not in iod.IDENTIFIERS
    }
    assert {d.id: (d.level, d.vintage) for d in dataset.descriptor.dimensions} == {
        "lsoa_code": ("LSOA", "2021"),
        "local_authority_code": ("LAD", "2024"),
    }
    assert dataset.descriptor.coverage.temporal == {"status": "unknown", "dimensions": []}
    assert all(d.kind != "time" for d in dataset.descriptor.dimensions)
    raw_values = [row["value_raw"] for row in observed if row["measure"] == "imd_score"]
    assert raw_values == raw[next(iter(iod.IMD))].tolist()
    flagged = next(row for row in observed if row["value_raw"] == "[c]")
    assert flagged["value"] is None and flagged["notes"] == "[c]"
    assert not diagnostics(dataset, "raw_value_reconstructed")


def test_historical_deprivation_without_raw_text_reports_reconstruction():
    acquired, _ = deprivation_acquisition()
    dataset = normalize(acquired)
    assert diagnostics(dataset, "raw_value_reconstructed")[0].count == 15
    assert "[c]" in dataset.to_arrow().column("value_raw").to_pylist()


def test_deprivation_retained_raw_uses_identity_not_row_position():
    acquired, raw = deprivation_acquisition()
    expected = normalize(acquired, raw_frame=raw).to_arrow()
    actual = normalize(acquired, raw_frame=raw.iloc[::-1]).to_arrow()
    assert actual.equals(expected)
    raw.loc[1, iod.LSOA_CODE] = raw.loc[0, iod.LSOA_CODE]
    with pytest.raises(RetrievalError, match="ambiguous LSOA"):
        normalize(acquired, raw_frame=raw)


def test_deprivation_retained_raw_values_must_match_the_source_table():
    acquired, raw = deprivation_acquisition()
    raw.loc[0, next(iter(iod.IMD))] = "99999"
    with pytest.raises(RetrievalError, match="raw values disagree"):
        normalize(acquired, raw_frame=raw)


def test_deprivation_retained_raw_geography_must_match_the_source_table():
    acquired, raw = deprivation_acquisition()
    raw.loc[0, iod.LAD_CODE] = "E09000002"
    with pytest.raises(RetrievalError, match="raw identifiers disagree"):
        normalize(acquired, raw_frame=raw)


@pytest.mark.parametrize("provider", ["nomis", "statswales", "deprivation"])
def test_unsupported_source_schema_fails_closed(provider):
    acquired = acquisition(provider, pd.DataFrame({"unknown": ["1"]}), {})
    with pytest.raises(RetrievalError, match="schema|metadata"):
        normalize(acquired)


@pytest.mark.parametrize("duplicate", [True, False])
def test_colliding_dimension_columns_fail_closed(duplicate):
    acquired = wales_acquisition()
    dim = copy.deepcopy(acquired.metadata["dimensions"][0])
    dim["columnName"] = "Area" if duplicate else "Area!"
    acquired.frame[dim["columnName"]] = acquired.frame["Area"]
    acquired.metadata["dimensions"].append(dim)
    with pytest.raises(RetrievalError, match="ambiguous normalized"):
        normalize(acquired)


def test_transform_and_contract_serialization_are_exactly_reproducible(tmp_path):
    acquired, raw = deprivation_acquisition()
    one, two = normalize(acquired, raw_frame=raw), normalize(acquired, raw_frame=raw)
    assert one.contract == two.contract
    one.write(tmp_path / "one")
    two.write(tmp_path / "two")
    first = {
        path.relative_to(tmp_path / "one"): path.read_bytes()
        for path in (tmp_path / "one").rglob("*")
        if path.is_file()
    }
    second = {
        path.relative_to(tmp_path / "two"): path.read_bytes()
        for path in (tmp_path / "two").rglob("*")
        if path.is_file()
    }
    assert first == second
    restored = Dataset.read(tmp_path / "one")
    assert restored.to_arrow().equals(one.to_arrow())
    assert restored.to_arrow("source").equals(one.to_arrow("source"))


def test_wrong_request_and_unimplemented_provider_fail_closed():
    with pytest.raises(RetrievalError, match="different source"):
        normalize(nomis_acquisition(), Request("nomis", "NM_2_1"))
    with pytest.raises(RetrievalError, match="No dataset normalization"):
        normalize(acquisition("unknown", pd.DataFrame(), {}))


def test_contract_is_machine_readable_without_nan_literals():
    contract = normalize(nomis_acquisition()).contract
    assert json.loads(json.dumps(contract, allow_nan=False)) == contract
    assert normalize(nomis_acquisition()).to_arrow().schema.field("value").type == pa.float64()
