import json

import pytest
from pydantic import ValidationError

from ssdu.conformance import get_conformance_families
from ssdu.providers import provider_ids
from ssdu.source_coverage import (
    COVERAGE_SCHEMA_VERSION,
    DATASET_CONTRACT_VERSION,
    OBSERVATIONS_CONTRACT_VERSION,
    RECORDS_CONTRACT_VERSION,
    SOURCE_COVERAGE,
    CoverageMatrix,
    SourceCoverage,
    get_source_coverage,
    provider_coverage_markdown,
    source_coverage_markdown,
)


def test_coverage_is_deterministic_serializable_and_detached():
    first = get_source_coverage()
    second = get_source_coverage()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    first["sources"][0]["capabilities"]["standardized_dataset"] = False
    first["sources"][0]["limits"].append("mutated")
    first["providers"][0]["source_ids"].append("mutated")
    assert get_source_coverage() == second
    assert second["schema_version"] == COVERAGE_SCHEMA_VERSION
    assert second["dataset_contract_version"] == DATASET_CONTRACT_VERSION


def test_providers_group_source_routes_without_changing_adapter_identifiers():
    matrix = get_source_coverage()
    providers = {row["provider_id"]: row for row in matrix["providers"]}
    assert set(providers) == {
        "ons",
        "welsh_government",
        "mhclg",
        "scottish_government",
        "nisra",
        "uk_data_service",
    }
    assert providers["ons"]["source_ids"] == ["nomis", "ons_additional"]
    assert providers["mhclg"]["source_ids"] == ["deprivation"]
    assert providers["welsh_government"]["source_ids"] == ["statswales"]
    assert providers["scottish_government"]["source_ids"] == ["scotland"]
    assert providers["nisra"]["source_ids"] == ["northern_ireland"]
    bindings = [source_id for row in providers.values() for source_id in row["source_ids"]]
    assert set(bindings) == {
        source["source_id"] for source in matrix["sources"] if source["source_id"] != "local_files"
    }
    assert len(bindings) == len(set(bindings))
    assert all(row["source_urls"][0].startswith("https://") for row in providers.values())
    sources = {row["source_id"]: row for row in matrix["sources"]}
    assert sources["deprivation"]["provider"] == "deprivation"
    assert sources["northern_ireland"]["provider"] == "nisra"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("duplicate_provider", "provider identifiers must be unique"),
        ("duplicate_within_provider", "exactly one provider binding"),
        ("duplicate_across_providers", "exactly one provider binding"),
        ("unknown_source", "existing source identifiers"),
        ("missing_provider", "every non-local source"),
        ("missing_source_binding", "every non-local source"),
        ("local_source", "import support, not a provider"),
        ("local_provider", "import support, not a provider"),
    ],
)
def test_invalid_provider_bindings_rejected(case, message):
    matrix = get_source_coverage()
    if case == "duplicate_provider":
        matrix["providers"].append(matrix["providers"][0])
    elif case == "duplicate_within_provider":
        matrix["providers"][0]["source_ids"].append("nomis")
    elif case == "duplicate_across_providers":
        matrix["providers"][1]["source_ids"].append("nomis")
    elif case == "unknown_source":
        matrix["providers"][0]["source_ids"].append("unknown")
    elif case == "missing_provider":
        matrix["providers"].pop()
    elif case == "missing_source_binding":
        matrix["providers"][0]["source_ids"].remove("ons_additional")
    elif case == "local_source":
        matrix["providers"][0]["source_ids"].append("local_files")
    else:
        matrix["providers"][0]["provider_id"] = "local_files"
    with pytest.raises(ValidationError, match=message):
        CoverageMatrix.model_validate(matrix)


def test_provider_bindings_required_for_current_schema_but_legacy_documents_load():
    matrix = get_source_coverage()
    del matrix["providers"]
    with pytest.raises(ValidationError, match="every non-local source"):
        CoverageMatrix.model_validate(matrix)
    matrix["schema_version"] = "1.1.0"
    legacy = CoverageMatrix.model_validate(matrix)
    assert legacy.providers == ()
    assert legacy.sources == SOURCE_COVERAGE.sources


def test_provider_table_has_organisations_not_dataset_or_local_import_rows():
    markdown = provider_coverage_markdown()
    assert markdown == provider_coverage_markdown()
    lines = markdown.splitlines()
    assert lines[0] == (
        "| Provider | SSDU coverage | Available data or access routes | Gaps and plans |"
    )
    assert len(lines) == len(SOURCE_COVERAGE.providers) + 2
    rows = [line.split(" | ") for line in lines[2:]]
    assert [row[1] for row in rows] == ["Partial"] * 5 + ["Planned"]
    assert all(provider.name in row[0] for provider, row in zip(SOURCE_COVERAGE.providers, rows))
    assert "Local CSV and Parquet" not in markdown
    assert all("English deprivation 2025" not in row[0] for row in rows)
    assert "English deprivation 2025, corrected File 7" in rows[2][2]
    assert "Nomis" in rows[0][2] and "Additional ONS APIs" in rows[0][3]
    assert "None yet." in rows[-1][2]
    assert rows[0][2] == "Nomis"
    assert SOURCE_COVERAGE.sources[0].retrieval.scope not in markdown
    assert SOURCE_COVERAGE.sources[0].limits[0] in rows[0][3]
    assert SOURCE_COVERAGE.sources[0].limits[1] not in rows[0][3]
    assert ".;" not in markdown


def test_only_migrated_providers_claim_standardized_datasets():
    rows = {row["source_id"]: row for row in get_source_coverage()["sources"]}
    standardized = {
        source_id for source_id, row in rows.items() if row["capabilities"]["standardized_dataset"]
    }
    assert {rows[source_id]["provider"] for source_id in standardized} == {*provider_ids(), "local"}
    for source_id in standardized:
        local = source_id == "local_files"
        assert rows[source_id]["contract_version"] == (
            RECORDS_CONTRACT_VERSION if local else OBSERVATIONS_CONTRACT_VERSION
        )
        assert rows[source_id]["profiles"] == (["records"] if local else ["observations"])
        assert rows[source_id]["capabilities"]["retained_raw_inputs"]
        assert rows[source_id]["capabilities"]["offline_replay"]
    assert rows["local_files"]["capabilities"]["data_retrieval"]
    assert rows["local_files"]["normalization"]["status"] == "implemented"
    assert rows["local_files"]["contract_version"] == RECORDS_CONTRACT_VERSION
    assert get_source_coverage()["supported_contract_versions"] == ["1.0.0", "1.1.0"]
    assert not rows["local_files"]["capabilities"]["rate_limited_requests"]


def test_plans_and_catalogue_metadata_never_claim_retrieval_support():
    matrix = get_source_coverage()
    planned = [row for row in matrix["sources"] if row["status"] == "planned"]
    assert {row["source_id"] for row in planned} == {
        "ons_additional",
        "uk_data_service",
    }
    assert all(not any(row["capabilities"].values()) for row in planned)
    assert all(row["provider"] is None and row["source_urls"] for row in planned)
    assert all(row["contract_version"] is None for row in planned)
    assert any("Rich Context/OpenAlex" in note for note in matrix["notes"])
    assert all(family["status"] == "planned" for family in matrix["planned_adapter_families"])


def test_scope_does_not_overstate_release_or_geography_coverage():
    rows = {row["source_id"]: row for row in get_source_coverage()["sources"]}
    assert all(
        not row["capabilities"]["provider_historical_release_selection"] for row in rows.values()
    )
    assert rows["nomis"]["conformance"]["status"] == "partial"
    assert rows["statswales"]["catalogue"]["status"] == "partial"
    assert rows["statswales"]["conformance"]["status"] == "partial"
    limits = " ".join(rows["deprivation"]["limits"])
    assert "Only corrected 2025 File 7" in limits
    assert "LSOA 2021" in limits and "2024" in limits
    assert "Comparability" in limits
    ukds_access = rows["uk_data_service"]["access"]
    assert "Controlled data" in ukds_access and "approved secure services" in ukds_access


@pytest.mark.parametrize("field", ["data_retrieval", "standardized_dataset", "offline_replay"])
def test_planned_capabilities_fail_closed(field):
    row = next(row for row in get_source_coverage()["sources"] if row["status"] == "planned")
    row["capabilities"][field] = True
    with pytest.raises(ValidationError, match="planned sources cannot claim"):
        SourceCoverage.model_validate(row)


def test_normalization_claim_requires_versioned_profile():
    row = get_source_coverage()["sources"][0]
    row["contract_version"] = None
    with pytest.raises(ValidationError, match="implemented versioned contract"):
        SourceCoverage.model_validate(row)
    row["contract_version"] = DATASET_CONTRACT_VERSION
    row["profiles"] = []
    with pytest.raises(ValidationError, match="declared profile"):
        SourceCoverage.model_validate(row)


def test_duplicate_ids_and_unknown_fields_rejected():
    matrix = get_source_coverage()
    matrix["sources"].append(matrix["sources"][0])
    with pytest.raises(ValidationError, match="must be unique"):
        CoverageMatrix.model_validate(matrix)
    row = get_source_coverage()["sources"][0]
    row["all_uk_datasets"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceCoverage.model_validate(row)


def test_documentation_table_is_generated_from_each_registry_row():
    markdown = source_coverage_markdown()
    for source in SOURCE_COVERAGE.sources:
        assert source.name in markdown
        assert source.retrieval.scope in markdown
        assert source.normalization.status in markdown
    assert "| Source | Status | Catalogue | Retrieval | Standardization | Conformance |" in markdown
    assert "not implemented data retrieval" in markdown
    assert source_coverage_markdown() == markdown


def test_registered_providers_have_explicit_semantic_families_and_real_paths():
    from pathlib import Path

    rows = {row["provider"]: row for row in get_source_coverage()["sources"] if row["provider"]}
    root = Path(__file__).parents[1]
    for provider in provider_ids():
        families = get_conformance_families(provider)
        assert len(families) >= 3
        assert rows[provider]["conformance_family_ids"] == [f.family_id for f in families]
        assert all((root / path).is_file() for path in rows[provider]["implementation_paths"])
    markdown = source_coverage_markdown()
    assert all(family.family_id in markdown for family in get_conformance_families())


def test_unregistered_or_other_provider_families_fail_closed():
    row = get_source_coverage()["sources"][0]
    row["conformance_family_ids"] = ["invented_unregistered_family"]
    with pytest.raises(ValidationError, match="registered for this provider"):
        SourceCoverage.model_validate(row)
    row["conformance_family_ids"] = [get_conformance_families("statswales")[0].family_id]
    with pytest.raises(ValidationError, match="registered for this provider"):
        SourceCoverage.model_validate(row)


def test_four_nation_claims_exclude_unsupported_routes_and_release_pins():
    rows = {row["source_id"]: row for row in get_source_coverage()["sources"]}
    ni = rows["northern_ireland"]
    scotland = rows["scotland"]
    assert ni["provider"] == "nisra"
    assert ni["conformance"]["status"] == scotland["conformance"]["status"] == "partial"
    assert "Flexible Table Builder" in " ".join(ni["limits"])
    assert "100,000 cells" in " ".join(ni["limits"])
    assert "file/ZIP" in " ".join(scotland["limits"])
    assert "slice-attached" in " ".join(scotland["limits"])
    assert "independently invented" in " ".join(get_source_coverage()["notes"])
