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
    source_coverage_markdown,
)


def test_coverage_is_deterministic_serializable_and_detached():
    first = get_source_coverage()
    second = get_source_coverage()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    first["sources"][0]["capabilities"]["standardized_dataset"] = False
    first["sources"][0]["limits"].append("mutated")
    assert get_source_coverage() == second
    assert second["schema_version"] == COVERAGE_SCHEMA_VERSION
    assert second["dataset_contract_version"] == DATASET_CONTRACT_VERSION


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
