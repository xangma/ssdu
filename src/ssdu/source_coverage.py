"""Declared source support; catalogue entries alone never imply data retrieval."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ssdu.conformance import get_conformance_families
from ssdu.dataset_contract import CONTRACT_VERSION as OBSERVATIONS_CONTRACT_VERSION
from ssdu.dataset_contract import RECORDS_CONTRACT_VERSION

COVERAGE_SCHEMA_VERSION = "1.3.0"
DATASET_CONTRACT_VERSION = RECORDS_CONTRACT_VERSION
StageStatus = Literal["implemented", "partial", "planned", "not_in_scope"]


class CoverageStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: StageStatus
    scope: str = Field(min_length=1)


class SourceCapabilities(BaseModel):
    """True means implemented within the row's declared scope, never a roadmap promise."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data_retrieval: bool = False
    standardized_dataset: bool = False
    retained_raw_inputs: bool = False
    offline_replay: bool = False
    rate_limited_requests: bool = False
    provider_historical_release_selection: bool = False


class SourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    status: Literal["implemented", "planned"]
    provider: str | None = None
    catalogue: CoverageStage
    retrieval: CoverageStage
    normalization: CoverageStage
    conformance: CoverageStage
    profiles: tuple[str, ...] = ()
    contract_version: str | None = None
    conformance_family_ids: tuple[str, ...] = ()
    capabilities: SourceCapabilities = Field(default_factory=SourceCapabilities)
    access: str = Field(min_length=1)
    limits: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    implementation_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_claims(self) -> "SourceCoverage":
        if self.status == "planned":
            if any(self.capabilities.model_dump().values()):
                raise ValueError("planned sources cannot claim implemented capabilities")
            if (
                self.contract_version
                or self.profiles
                or self.implementation_paths
                or self.conformance_family_ids
            ):
                raise ValueError("planned sources cannot claim implemented normalization or code")
            stages = (self.catalogue, self.retrieval, self.normalization, self.conformance)
            if any(stage.status != "planned" for stage in stages):
                raise ValueError("planned source stages must remain planned")
        if self.capabilities.standardized_dataset:
            if self.normalization.status != "implemented" or not self.contract_version:
                raise ValueError("standardized datasets require an implemented versioned contract")
            if not self.profiles:
                raise ValueError("standardized datasets require a declared profile")
        elif self.contract_version or self.profiles:
            raise ValueError("contract versions and profiles require standardized dataset support")
        if self.capabilities.data_retrieval and self.retrieval.status not in {
            "implemented",
            "partial",
        }:
            raise ValueError("retrieval capability requires implemented retrieval")
        registered = {family.family_id for family in get_conformance_families(self.provider)}
        if len(self.conformance_family_ids) != len(set(self.conformance_family_ids)):
            raise ValueError("conformance family identifiers must be unique")
        if not set(self.conformance_family_ids).issubset(registered):
            raise ValueError("conformance families must be registered for this provider")
        return self


class PlannedAdapterFamily(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family_id: str
    scope: str
    status: Literal["planned"] = "planned"
    source_urls: tuple[str, ...] = ()


class ProviderCoverage(BaseModel):
    """An organisation or service custodian, separate from its adapters and releases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)
    source_urls: tuple[str, ...] = ()
    overview: str = Field(default="", max_length=2000)


class CoverageMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = COVERAGE_SCHEMA_VERSION
    dataset_contract_version: str = DATASET_CONTRACT_VERSION
    supported_contract_versions: tuple[str, ...] = (
        OBSERVATIONS_CONTRACT_VERSION,
        RECORDS_CONTRACT_VERSION,
    )
    sources: tuple[SourceCoverage, ...]
    providers: tuple[ProviderCoverage, ...] = ()
    planned_adapter_families: tuple[PlannedAdapterFamily, ...] = ()
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_sources(self) -> "CoverageMatrix":
        ids = [source.source_id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source identifiers must be unique")
        # Previously emitted 1.1 documents predate institutional provider bindings.
        if self.schema_version == "1.1.0" and not self.providers:
            return self
        provider_ids = [provider.provider_id for provider in self.providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider identifiers must be unique")
        bindings: list[str] = []
        for provider in self.providers:
            if provider.provider_id in {"local", "local_files"}:
                raise ValueError("local files are import support, not a provider")
            if "local_files" in provider.source_ids:
                raise ValueError("local files are import support, not a provider")
            if not set(provider.source_ids).issubset(ids):
                raise ValueError("providers must reference existing source identifiers")
            bindings.extend(provider.source_ids)
        if len(bindings) != len(set(bindings)):
            raise ValueError("source identifiers must have exactly one provider binding")
        if set(bindings) != set(ids) - {"local_files"}:
            raise ValueError("every non-local source must have exactly one provider binding")
        return self


def _stage(status: StageStatus, scope: str) -> CoverageStage:
    return CoverageStage(status=status, scope=scope)


def _network_capabilities() -> SourceCapabilities:
    return SourceCapabilities(
        data_retrieval=True,
        standardized_dataset=True,
        retained_raw_inputs=True,
        offline_replay=True,
        rate_limited_requests=True,
    )


def _planned(source_id: str, name: str, scope: str, access: str, *urls: str) -> SourceCoverage:
    return SourceCoverage(
        source_id=source_id,
        name=name,
        status="planned",
        catalogue=_stage("planned", scope),
        retrieval=_stage("planned", scope),
        normalization=_stage(
            "planned", "Profile and transforms require provider-specific validation."
        ),
        conformance=_stage("planned", "No adapter conformance certification yet."),
        access=access,
        limits=(
            "Proposed integration; no implemented retrieval adapter or promised delivery date.",
        ),
        source_urls=urls,
    )


SOURCE_COVERAGE = CoverageMatrix(
    providers=(
        ProviderCoverage(
            provider_id="ons",
            name="Office for National Statistics (ONS)",
            overview="Selected statistics through Nomis; wider ONS access is planned.",
            source_ids=("nomis", "ons_additional"),
            source_urls=("https://www.ons.gov.uk/",),
        ),
        ProviderCoverage(
            provider_id="welsh_government",
            name="Welsh Government",
            overview="Selected Welsh statistics through StatsWales.",
            source_ids=("statswales",),
            source_urls=("https://www.gov.wales/",),
        ),
        ProviderCoverage(
            provider_id="mhclg",
            name="Ministry of Housing, Communities and Local Government (MHCLG)",
            overview="Selected housing, communities and local-government statistical tables.",
            source_ids=("mhclg", "deprivation"),
            source_urls=(
                "https://www.gov.uk/government/organisations/ministry-of-housing-communities-local-government",
            ),
        ),
        ProviderCoverage(
            provider_id="scottish_government",
            name="Scottish Government",
            overview="Selected Scottish official statistics through statistics.gov.scot.",
            source_ids=("scotland",),
            source_urls=("https://www.gov.scot/",),
        ),
        ProviderCoverage(
            provider_id="nisra",
            name="Northern Ireland Statistics and Research Agency (NISRA)",
            overview="Selected Northern Ireland statistics through the NISRA Data Portal.",
            source_ids=("northern_ireland",),
            source_urls=("https://www.nisra.gov.uk/",),
        ),
        ProviderCoverage(
            provider_id="uk_data_service",
            name="UK Data Service",
            overview="Research data and surveys; integration is planned.",
            source_ids=("uk_data_service",),
            source_urls=("https://ukdataservice.ac.uk/",),
        ),
    ),
    sources=(
        SourceCoverage(
            source_id="nomis",
            name="Nomis",
            provider="nomis",
            status="implemented",
            catalogue=_stage(
                "implemented",
                "Cached catalogue browsing, dataset definitions and native dimension values before retrieval.",
            ),
            retrieval=_stage(
                "implemented", "Metadata-validated native selectors and bounded CSV pagination."
            ),
            normalization=_stage(
                "implemented", "Observation profile for retained API columns and values."
            ),
            conformance=_stage(
                "partial",
                "Three registered semantic shapes: category/time, independent measures and unavailable/ambiguous metadata; pagination and revision checks remain scoped.",
            ),
            profiles=("observations",),
            contract_version=OBSERVATIONS_CONTRACT_VERSION,
            conformance_family_ids=tuple(f.family_id for f in get_conformance_families("nomis")),
            capabilities=_network_capabilities(),
            access="Public API; application caching and shared per-host request limiting.",
            limits=(
                "Native selectors remain dataset-specific and must be inspected before retrieval.",
                "Request max_rows is at most 100,000; page_size is at most 10,000.",
                "Revision and row counts are checked around paginated retrieval.",
                "Historical replay uses retained snapshots, not arbitrary provider release selection.",
                "Validation is scoped to exercised dataset structures; all Nomis families are not certified.",
                "Catalogue pages reuse the bounded complete metadata response; whole-portal semantic coverage is not claimed.",
            ),
            source_urls=("https://www.nomisweb.co.uk/api/v01/help",),
            implementation_paths=(
                "src/ssdu/adapters/nomis.py",
                "src/ssdu/connectors.py",
                "tests/test_nomis.py",
                "tests/test_connector_providers.py",
                "tests/test_conformance_families.py",
            ),
        ),
        SourceCoverage(
            source_id="statswales",
            name="StatsWales v2",
            provider="statswales",
            status="implemented",
            catalogue=_stage(
                "partial",
                "Paginated catalogue browsing and cached dataset/filter inspection before retrieval; metadata format coverage remains scoped.",
            ),
            retrieval=_stage(
                "implemented", "UUID datasets through filtered, bounded v2 pagination."
            ),
            normalization=_stage(
                "implemented",
                "Observation profile retaining formatted cells and Notes; codes recovered only when unambiguous.",
            ),
            conformance=_stage(
                "partial",
                "Three registered semantic shapes: hierarchical multi-measure, ambiguous references and Welsh formatted cells; not all published tables.",
            ),
            profiles=("observations",),
            contract_version=OBSERVATIONS_CONTRACT_VERSION,
            conformance_family_ids=tuple(
                f.family_id for f in get_conformance_families("statswales")
            ),
            capabilities=_network_capabilities(),
            access="Public v2 API; application caching and shared per-host request limiting.",
            limits=(
                "Request max_rows is at most 100,000; page_size is at most 10,000.",
                "Published revision is observed and checked, not a provider-side historical release pin.",
                "Ambiguous labels are not silently converted into dimension codes.",
                "Catalogue presence does not establish conformance for every dataset.",
            ),
            source_urls=(
                "https://api.stats.gov.wales/docs/?urls.primaryName=API+v2+%28English%29",
            ),
            implementation_paths=(
                "src/ssdu/adapters/statswales.py",
                "src/ssdu/connectors.py",
                "tests/test_statswales.py",
                "tests/test_connector_providers.py",
                "tests/test_conformance_families.py",
            ),
        ),
        SourceCoverage(
            source_id="mhclg",
            name="MHCLG statistical publications and tabular files",
            provider="mhclg",
            status="implemented",
            catalogue=_stage(
                "partial",
                "Paginated GOV.UK statistical-publication discovery for MHCLG and supported predecessors; cached publication and attachment metadata.",
            ),
            retrieval=_stage(
                "implemented",
                "Selected CSV distributions and supported CSVW metadata resolved from publication attachments; retained resource bytes are reused.",
            ),
            normalization=_stage(
                "implemented",
                "Records profile preserves source cells; supported CSVW declarations supply types, keys and field metadata without inferred statistical semantics.",
            ),
            conformance=_stage(
                "partial",
                "Synthetic publication/file families cover literal CSV records, supported CSVW declarations and release/distribution changes with offline replay; no whole-provider certification.",
            ),
            profiles=("records",),
            contract_version=RECORDS_CONTRACT_VERSION,
            conformance_family_ids=tuple(f.family_id for f in get_conformance_families("mhclg")),
            capabilities=_network_capabilities(),
            access="Public GOV.UK metadata and allowlisted HTTPS files through shared cached, rate-limited requests.",
            limits=(
                "GOV.UK statistical publications only; not every MHCLG service, historical publication or download format is integrated.",
                "CSV and a bounded CSVW subset only; unsupported metadata and file formats fail explicitly.",
                "Publication updates and resource hashes identify retained releases; arbitrary provider historical release selection is not claimed.",
                "Records do not infer geography, measures, units or comparability; versioned semantic transforms remain separate.",
            ),
            source_urls=(
                "https://docs.publishing.service.gov.uk/repos/search-api/using-the-search-api.html",
                "https://content-api.publishing.service.gov.uk/",
                "https://www.w3.org/TR/tabular-metadata/",
            ),
            implementation_paths=(
                "src/ssdu/adapters/mhclg.py",
                "src/ssdu/distributions.py",
                "src/ssdu/normalization_mhclg.py",
                "tests/test_mhclg.py",
            ),
        ),
        SourceCoverage(
            source_id="deprivation",
            name="Deprivation compatibility adapter",
            provider="deprivation",
            status="implemented",
            catalogue=_stage(
                "implemented", "One explicitly pinned official corrected File 7 asset."
            ),
            retrieval=_stage(
                "implemented", "Shared England release and complete local-authority selections."
            ),
            normalization=_stage(
                "implemented",
                "Observation profile preserves scores, ranks and deciles as distinct measures.",
            ),
            conformance=_stage(
                "implemented",
                "Three registered synthetic File 7 shapes: measure types, missing/suppressed cells and complete LAD selections; geography and offline replay checks.",
            ),
            profiles=("observations",),
            contract_version=OBSERVATIONS_CONTRACT_VERSION,
            conformance_family_ids=tuple(
                f.family_id for f in get_conformance_families("deprivation")
            ),
            capabilities=_network_capabilities(),
            access="Public official download; one cached national parent supports local selections.",
            limits=(
                "Compatibility route for existing requests and snapshots; use the MHCLG connector for new publication/file retrieval.",
                "Only corrected 2025 File 7 is supported; other files, editions and UK nations are outside scope.",
                "LSOA 2021 and local-authority 2024 geographies are explicit.",
                "Comparability across editions or differing boundary vintages is not established.",
                "National selection requires a max_rows budget sufficient for the complete release.",
            ),
            source_urls=(
                "https://www.gov.uk/government/statistics/english-indices-of-deprivation-2025",
            ),
            implementation_paths=(
                "src/ssdu/adapters/deprivation.py",
                "tests/test_deprivation.py",
                "tests/test_conformance_families.py",
            ),
        ),
        SourceCoverage(
            source_id="local_files",
            name="Local CSV and Parquet",
            provider="local",
            status="implemented",
            catalogue=_stage("not_in_scope", "User-supplied files; no external source catalogue."),
            retrieval=_stage(
                "implemented", "Import CSV or Parquet into immutable local snapshots."
            ),
            normalization=_stage(
                "implemented",
                "Records profile retains typed columns, supplied field meanings, declared keys and row grain.",
            ),
            conformance=_stage(
                "partial",
                "Three synthetic semantic shapes: typed CSV identifiers and null policy, native Parquet types, and declared composite keys; offline migration and transfer replay.",
            ),
            profiles=("records",),
            contract_version=RECORDS_CONTRACT_VERSION,
            conformance_family_ids=tuple(f.family_id for f in get_conformance_families("local")),
            capabilities=SourceCapabilities(
                data_retrieval=True,
                standardized_dataset=True,
                retained_raw_inputs=True,
                offline_replay=True,
            ),
            access="Local files accessible to the calling process; licence and semantics supplied by the user.",
            limits=(
                "File import does not infer units, survey weights, codebooks or geographic compatibility.",
                "Imports are bounded to 25 MB source bytes, 100 MB decoded bytes, 1,000 columns and 100,000 rows.",
                "Records are not automatically reshaped into observations; undeclared keys and row grain remain unknown.",
                "Local import is not an access mechanism for safeguarded or controlled survey data.",
            ),
            implementation_paths=(
                "src/ssdu/local_import.py",
                "src/ssdu/records.py",
                "tests/test_records_store.py",
            ),
        ),
        _planned(
            "ons_additional",
            "Additional ONS APIs",
            "Datasets and releases beyond the existing Nomis adapter.",
            "Provider-specific APIs, edition/version handling and bounded requests need implementation.",
            "https://developer.ons.gov.uk/",
            "https://developer.ons.gov.uk/dataset/datasets/",
        ),
        SourceCoverage(
            source_id="scotland",
            name="Scottish official statistics: linked-data cubes",
            provider="scotland",
            status="implemented",
            catalogue=_stage(
                "partial",
                "Paged linked-cube catalogue, pre-download descriptions and URI member codes through the common cached connector workflow; file datasets are excluded.",
            ),
            retrieval=_stage(
                "implemented",
                "Bounded statistics.gov.scot RDF cube selections with explicit dimensions and a declared data structure.",
            ),
            normalization=_stage(
                "implemented",
                "Observation profile preserves URI dimension members, separate measures, numeric lexemes and status/notes.",
            ),
            conformance=_stage(
                "partial",
                "Registered synthetic linked-cube shapes and connector discovery, release checks and offline replay; no whole-portal certification.",
            ),
            profiles=("observations",),
            contract_version=OBSERVATIONS_CONTRACT_VERSION,
            conformance_family_ids=tuple(f.family_id for f in get_conformance_families("scotland")),
            capabilities=_network_capabilities(),
            access="Public SPARQL endpoint through shared cached, rate-limited HTTP requests.",
            limits=(
                "Linked RDF cubes only; file/ZIP routes and slice-attached components are unsupported.",
                "Native URI/code selectors and request budgets remain dataset-specific.",
                "Metadata is checked around retrieval; provider historical release pinning is not claimed.",
                "Dimension code pages enumerate observed dataset members, not a complete external vocabulary.",
                "Fixture certification covers declared shapes, not every Scottish table or cross-nation comparability.",
            ),
            source_urls=(
                "https://guides.statistics.gov.scot/article/22-querying-data-with-sparql",
                "https://guides.statistics.gov.scot/article/34-understanding-the-data-structure",
            ),
            implementation_paths=(
                "src/ssdu/adapters/scotland.py",
                "src/ssdu/normalization_scotland.py",
                "tests/test_scotland.py",
                "tests/test_connector_scotland.py",
            ),
        ),
        SourceCoverage(
            source_id="northern_ireland",
            name="NISRA Data Portal",
            provider="nisra",
            status="implemented",
            catalogue=_stage(
                "partial",
                "Bounded English PxStat collection catalogue with deterministic local pagination, dataset descriptions and native code lists through the common cached connector workflow.",
            ),
            retrieval=_stage(
                "implemented",
                "Bounded complete English JSON-stat cubes, followed by native-code local selection.",
            ),
            normalization=_stage(
                "implemented",
                "Observation profile preserves JSON-stat category order, measures, units, sparse cells and source status.",
            ),
            conformance=_stage(
                "partial",
                "Registered synthetic JSON-stat shapes and connector discovery, pre-download selection validation, release checks and offline replay.",
            ),
            profiles=("observations",),
            contract_version=OBSERVATIONS_CONTRACT_VERSION,
            conformance_family_ids=tuple(f.family_id for f in get_conformance_families("nisra")),
            capabilities=_network_capabilities(),
            access="Public PxStat JSON-stat API through shared cached, rate-limited HTTP requests.",
            limits=(
                "NISRA Data Portal only; the separate Census Flexible Table Builder and record data are unsupported.",
                "Retrieval downloads complete cubes bounded to 100,000 cells before selection; larger cubes remain metadata-only within metadata bounds.",
                "English metadata only; native dimension codes are preserved rather than harmonized across nations.",
                "The selected result must fit max_rows; arbitrary provider historical releases are not addressable.",
                "Fixture certification covers declared shapes, not all NISRA datasets.",
            ),
            source_urls=(
                "https://www.nisra.gov.uk/statistics/ni-summary-statistics/nisra-data-portal",
                "https://github.com/CSOIreland/PxStat/wiki/API-Cube-RESTful",
            ),
            implementation_paths=(
                "src/ssdu/adapters/nisra.py",
                "src/ssdu/normalization_nisra.py",
                "tests/test_nisra.py",
                "tests/test_connector_nisra.py",
            ),
        ),
        _planned(
            "uk_data_service",
            "UK Data Service",
            "Catalogue metadata and downloads permitted by dataset access conditions.",
            "Open, safeguarded and controlled access differ; registration and agreements may apply. Controlled data are accessed through approved secure services.",
            "https://ukdataservice.ac.uk/find-data/",
            "https://ukdataservice.ac.uk/find-data/access-conditions/",
        ),
    ),
    planned_adapter_families=(
        PlannedAdapterFamily(
            family_id="ckan",
            scope="Catalogue and supported resource downloads for explicitly configured CKAN portals.",
            source_urls=("https://docs.ckan.org/en/latest/api/",),
        ),
        PlannedAdapterFamily(
            family_id="sdmx",
            scope="Provider-specific SDMX structures and observation retrieval after conformance validation.",
            source_urls=("https://github.com/sdmx-twg/sdmx-rest",),
        ),
        PlannedAdapterFamily(
            family_id="file_download",
            scope="Additional remote-file formats and publishers beyond the MHCLG CSV/CSVW route; explicit parser, schema, licence and transformation contracts remain required.",
        ),
    ),
    notes=(
        "Metadata synchronisation uses the five common connectors. Its freshness and completeness reports describe each configured scope; metadata discovery does not certify data retrieval or cross-source comparability.",
        "Provider coverage groups scoped adapters and releases; it does not certify all datasets from an organisation. Local file imports are separate input support.",
        "Harvested catalogue records and Rich Context/OpenAlex paper–dataset links are discovery metadata, not implemented data retrieval.",
        "Implemented capabilities apply only to each row's stated scope. False means no implemented support claimed; it does not describe the provider's own capabilities.",
        "Dataset contract and coverage schema versions are independent.",
        "The latest contract version is 1.1.0 for records; supported provider observation profiles retain contract 1.0.0. Each source row identifies its profile and version.",
        "Raw tables remain available; normalized observations do not establish comparability between sources.",
        "Conformance family fixtures are independently invented protocol-shaped examples, not redistributed published statistics. References identify protocols; certification remains bounded to tested shapes.",
    ),
)


def get_source_coverage() -> dict[str, Any]:
    """Return a detached, deterministic JSON-compatible source support matrix."""
    return SOURCE_COVERAGE.model_dump(mode="json")


def source_coverage_markdown() -> str:
    """Render documentation from the registry without a second maintained coverage list."""

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| Source | Status | Catalogue | Retrieval | Standardization | Conformance |",
        "|---|---|---|---|---|---|",
    ]
    for source in SOURCE_COVERAGE.sources:
        title = cell(source.name)
        if source.source_urls:
            title = f"[{title}]({source.source_urls[0]})"
        stages = [source.catalogue, source.retrieval, source.normalization, source.conformance]
        values = [title, source.status, *(f"{s.status}: {cell(s.scope)}" for s in stages)]
        if source.conformance_family_ids:
            values[-1] += " Families: " + ", ".join(
                f"`{family_id}`" for family_id in source.conformance_family_ids
            )
        lines.append("| " + " | ".join(values) + " |")
    lines.extend(["", *(f"- {note}" for note in SOURCE_COVERAGE.notes)])
    return "\n".join(lines) + "\n"


def provider_coverage_markdown() -> str:
    """Render broad provider coverage; exact dataset and release support is separate."""

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    sources = {source.source_id: source for source in SOURCE_COVERAGE.sources}
    lines = [
        "| Provider | SSDU coverage | Coverage overview |",
        "|---|---|---|",
    ]
    for provider in SOURCE_COVERAGE.providers:
        bound = [sources[source_id] for source_id in provider.source_ids]
        available = [source for source in bound if source.status == "implemented"]
        title = cell(provider.name)
        if provider.source_urls:
            title = f"[{title}]({provider.source_urls[0]})"
        status = "Partial" if available else "Planned"
        lines.append(f"| {title} | {status} | {cell(provider.overview)} |")
    return "\n".join(lines) + "\n"
