"""Bounded, offline semantic certifications; family rows never certify a whole portal.

Fixtures are independently invented protocol-shaped examples, not redistributed
provider data. Documentation references identify formats rather than data provenance.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ConformanceFamily:
    family_id: str
    provider: str
    scope: str
    references: tuple[str, ...]
    test_node: str
    evidence_kind: str = "synthetic_boundary"


def _family(family_id: str, provider: str, scope: str, *references: str) -> ConformanceFamily:
    return ConformanceFamily(
        family_id,
        provider,
        scope,
        references,
        f"tests/test_conformance_families.py::test_registered_family_contract[{family_id}]",
    )


_NOMIS = "https://www.nomisweb.co.uk/api/v01/help"
_WALES = "https://api.stats.gov.wales/docs/?urls.primaryName=API+v2+%28English%29"
_IOD = "https://www.gov.uk/government/statistics/english-indices-of-deprivation-2025"

_JSONSTAT = "https://json-stat.org/format/"
_PXSTAT = "https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful"
_SCOTLAND = "https://guides.statistics.gov.scot/article/22-querying-data-with-sparql"
_LOCAL = "https://github.com/xangma/ssdu/blob/main/docs/content/concepts/dataset-contract.md"
_GOVUK_CONTENT = "https://content-api.publishing.service.gov.uk/"
_CSVW = "https://www.w3.org/TR/tabular-metadata/"

CONFORMANCE_FAMILIES = (
    _family(
        "nomis_category_time_series",
        "nomis",
        "Category-by-time counts with leading-zero codes and explicit count units.",
        _NOMIS,
    ),
    _family(
        "nomis_independent_measures",
        "nomis",
        "Independent MEASURE/MEASURES identities and representation-specific units.",
        _NOMIS,
    ),
    _family(
        "nomis_missing_and_ambiguous_metadata",
        "nomis",
        "Unavailable cells, missing and omitted dimension codes, conflicting unit metadata.",
        _NOMIS,
    ),
    _family(
        "statswales_hierarchical_multi_measure",
        "statswales",
        "Hierarchical categories, three measure identities and multi-year periods.",
        _WALES,
    ),
    _family(
        "statswales_ambiguous_dimensions",
        "statswales",
        "Repeated or unknown labels and unresolved measure identity remain explicit unknowns.",
        _WALES,
    ),
    _family(
        "statswales_welsh_formatted_cells",
        "statswales",
        "Welsh fields, formatted scientific notation, malformed numeric cells and notes.",
        _WALES,
    ),
    _family(
        "deprivation_file7_measure_types",
        "deprivation",
        "File 7 score, rank, decile and population meanings and exact numeric strings.",
        _IOD,
    ),
    _family(
        "deprivation_suppression_and_population",
        "deprivation",
        "Suppressed and absent measures remain distinct from numeric population zeros.",
        _IOD,
    ),
    _family(
        "deprivation_complete_lad_selection",
        "deprivation",
        "Complete LAD selections reuse a national parent and preserve selected source tokens.",
        _IOD,
    ),
    ConformanceFamily(
        "nisra_dense_time_series",
        "nisra",
        "Dense JSON-stat time axes preserve category order, codes and raw values.",
        (_JSONSTAT, _PXSTAT),
        "tests/test_nisra.py::test_dense_time_series_semantics",
    ),
    ConformanceFamily(
        "nisra_multidimensional",
        "nisra",
        "Multidimensional JSON-stat axes, measure-specific units and source notes.",
        (_JSONSTAT, _PXSTAT),
        "tests/test_nisra.py::test_multidimensional_order_units_and_notes",
    ),
    ConformanceFamily(
        "nisra_sparse_status",
        "nisra",
        "Sparse JSON-stat values, suppression statuses and missing cells.",
        (_JSONSTAT, _PXSTAT),
        "tests/test_nisra.py::test_sparse_missing_status_and_source_strings",
    ),
    ConformanceFamily(
        "scotland_linked_series",
        "scotland",
        "Linked-data cubes preserve URI members, temporal codes and numeric lexemes.",
        (_SCOTLAND,),
        "tests/test_scotland.py::test_series_preserves_uri_codes_and_raw_lexemes",
    ),
    ConformanceFamily(
        "nisra_connector_workflow",
        "nisra",
        "Paged collection metadata, native code lists and validated selections use shared cached retrieval and replay offline.",
        (_PXSTAT, _JSONSTAT),
        "tests/test_connector_nisra.py::test_nisra_connector_workflow_metadata_selection_fetch_and_offline",
    ),
    ConformanceFamily(
        "scotland_connector_workflow",
        "scotland",
        "Linked-cube catalogue pages, native URI member codes and metadata-scoped acquisition replay from a transferred store offline.",
        (_SCOTLAND,),
        "tests/test_connector_scotland.py::test_common_workflow_cache_and_fresh_store_snapshot_replay_are_offline",
    ),
    ConformanceFamily(
        "scotland_multiple_measures",
        "scotland",
        "Wide and measureType RDF observations preserve separate measure identities.",
        (_SCOTLAND,),
        "tests/test_scotland.py::test_multiple_measures_preserve_variable_identities",
    ),
    ConformanceFamily(
        "scotland_suppression",
        "scotland",
        "Linked-data missing or suppressed cells retain source status and notes.",
        (_SCOTLAND,),
        "tests/test_scotland.py::test_suppression_status_and_notes_are_retained",
    ),
    ConformanceFamily(
        "mhclg_csv_records",
        "mhclg",
        "Unrelated publication CSV layouts preserve literal source cells without inferred geography, measures or missingness.",
        (_GOVUK_CONTENT,),
        "tests/test_mhclg.py::test_general_csv_publications_preserve_source_cells",
    ),
    ConformanceFamily(
        "mhclg_csvw_declared_records",
        "mhclg",
        "Supported CSVW types, primary keys and null tokens preserve typed records alongside original CSV lexemes.",
        (_GOVUK_CONTENT, _CSVW),
        "tests/test_mhclg.py::test_csvw_explicit_types_keys_nulls_and_source_lexemes",
    ),
    ConformanceFamily(
        "mhclg_publication_release_replay",
        "mhclg",
        "Changed publication revisions and distribution identities retain the earlier records contract for offline replay.",
        (_GOVUK_CONTENT,),
        "tests/test_mhclg.py::test_revision_attachment_changes_and_offline_contract_replay",
    ),
    ConformanceFamily(
        "local_typed_csv",
        "local",
        "Invented CSV identifiers, declared field types and exact null-token meanings.",
        (_LOCAL,),
        "tests/test_records_store.py::test_records_typed_csv_identifiers_and_explicit_null_policy",
    ),
    ConformanceFamily(
        "local_native_parquet",
        "local",
        "Invented native Parquet scalar types retain precision and temporal meaning.",
        (_LOCAL,),
        "tests/test_records_store.py::test_records_native_parquet_types_are_preserved",
    ),
    ConformanceFamily(
        "local_declared_keys",
        "local",
        "Invented records preserve row grain and reject duplicate or null declared composite keys.",
        (_LOCAL,),
        "tests/test_records_store.py::test_records_declared_composite_keys_grain_and_validation",
    ),
    *(
        ConformanceFamily(
            f"{provider}_metadata_sync",
            provider,
            "Synthetic metadata-only catalogue, descriptions and native codes retain referenced raw evidence in verified immutable snapshots.",
            (reference,),
            f"tests/test_metadata_sync.py::test_each_provider_sync_keeps_raw_references_and_metadata_only[{provider}]",
        )
        for provider, reference in (
            ("nomis", _NOMIS),
            ("statswales", _WALES),
            ("mhclg", _GOVUK_CONTENT),
            ("nisra", _PXSTAT),
            ("scotland", _SCOTLAND),
        )
    ),
)


def get_conformance_families(provider: str | None = None) -> tuple[ConformanceFamily, ...]:
    """Return immutable family declarations, optionally restricted to a provider."""
    return tuple(
        family for family in CONFORMANCE_FAMILIES if provider is None or family.provider == provider
    )


def conformance_coverage() -> dict:
    """Detached JSON-compatible certification metadata for API and documentation use."""
    return {
        "schema_version": "1.0.0",
        "fixture_policy": "Independently invented protocol-shaped fixtures; no published data cells.",
        "families": [
            {**asdict(family), "references": list(family.references)}
            for family in CONFORMANCE_FAMILIES
        ],
    }
