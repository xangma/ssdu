"""English IoD 2025: retain the national CSV and support complete LAD selections."""

import csv
import re
from io import BytesIO, StringIO

import pandas as pd

from ssdu.http import HttpClient
from ssdu.models import Acquisition, Request, RetrievalError

PROVIDER = "deprivation"
DATASET_ID = "english-indices-of-deprivation-2025"
TITLE = "English indices of deprivation 2025 — LSOA ranks, scores and populations"
SOURCE_URL = (
    "https://assets.publishing.service.gov.uk/media/691ded56d140bbbaa59a2a7d/"
    "File_7_IoD2025_All_Ranks_Scores_Deciles_Population_Denominators.csv"
)
RELEASE_URL = "https://www.gov.uk/government/statistics/english-indices-of-deprivation-2025"
LICENCE = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
CITATION = (
    "Ministry of Housing, Communities and Local Government (2025), English indices of "
    "deprivation 2025, File 7: All ranks, scores, deciles and population denominators "
    "(corrected release). Crown copyright, Open Government Licence v3.0. " + RELEASE_URL
)

LSOA_CODE = "LSOA code (2021)"
LAD_CODE = "Local Authority District code (2024)"
IDENTIFIERS = {
    LSOA_CODE: "lsoa_code",
    "LSOA name (2021)": "lsoa_name",
    LAD_CODE: "local_authority_code",
    "Local Authority District name (2024)": "local_authority_name",
}
IMD = {
    "Index of Multiple Deprivation (IMD) Score": "imd_score",
    "Index of Multiple Deprivation (IMD) Rank (where 1 is most deprived)": "imd_rank",
    "Index of Multiple Deprivation (IMD) Decile (where 1 is most deprived 10% of LSOAs)": (
        "imd_decile"
    ),
}


def default_request() -> Request:
    return national_request()


def search(query: str, client: HttpClient) -> list[dict]:
    """A curated entry for the pinned official release; no implied global search."""
    haystack = f"{TITLE} {DATASET_ID} IMD England".casefold()
    if all(word in haystack for word in query.casefold().split()):
        return [
            {
                "id": DATASET_ID,
                "title": TITLE,
                "provider": PROVIDER,
                "source_url": RELEASE_URL,
                "access": "open",
                "licence": LICENCE,
            }
        ]
    return []


def _selectors(request: Request) -> list[str]:
    if request.provider != PROVIDER or request.dataset_id != DATASET_ID:
        raise RetrievalError(f"Unsupported deprivation dataset: {request.dataset_id}")
    if set(request.params) != {"local_authority_codes"}:
        raise RetrievalError("Specify only local_authority_codes as a non-empty list of GSS codes")
    codes = request.params["local_authority_codes"]
    if not isinstance(codes, list) or not codes:
        raise RetrievalError("local_authority_codes must be a non-empty list of GSS strings")
    if any(not isinstance(code, str) or not re.fullmatch(r"E0[6789]\d{6}", code) for code in codes):
        raise RetrievalError("Use English LAD GSS strings with the E06, E07, E08 or E09 prefix")
    if len(set(codes)) != len(codes):
        raise RetrievalError("local_authority_codes must not contain duplicates")
    return codes


def _column_name(source: str) -> str:
    return (
        IDENTIFIERS.get(source)
        or IMD.get(source)
        or re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    )


def fetch(request: Request, client: HttpClient) -> Acquisition:
    national = request == national_request()
    if not national:
        _selectors(request)  # Invalid selections must fail before provider access.
    resource = client.get(SOURCE_URL)
    return from_resource(request, resource, national=national)


def national_request() -> Request:
    return Request(PROVIDER, DATASET_ID, {"scope": "england"}, max_rows=100000)


def from_resource(request: Request, resource, *, national=False) -> Acquisition:
    """Validate and normalise retained bytes, without contacting a provider."""
    if national:
        if request != national_request():
            raise RetrievalError("Invalid national deprivation request")
        codes = []
    else:
        codes = _selectors(request)
    if len(resource.body) > 25_000_000:
        raise RetrievalError("Deprivation CSV exceeds the 25 MB raw-file limit")
    try:
        header = next(csv.reader(StringIO(resource.body.decode("utf-8-sig"))))
        if len(header) != len(set(header)):
            raise RetrievalError("Deprivation CSV contains duplicate column names")
        raw = pd.read_csv(
            BytesIO(resource.body), dtype="string", keep_default_na=False, encoding="utf-8-sig"
        )
    except (
        UnicodeDecodeError,
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
        StopIteration,
    ) as exc:
        raise RetrievalError("Invalid deprivation CSV") from exc
    required = set(IDENTIFIERS) | set(IMD)
    if not required.issubset(raw.columns) or raw.empty:
        raise RetrievalError("Deprivation CSV is empty or lacks the expected IoD 2025 schema")
    if (
        not raw[LSOA_CODE].str.fullmatch(r"E01\d{6}").all()
        or not raw[LAD_CODE].str.fullmatch(r"E0[6789]\d{6}").all()
    ):
        raise RetrievalError("Deprivation CSV has invalid or missing geography codes")
    if raw[LSOA_CODE].duplicated().any():
        raise RetrievalError("Deprivation CSV contains duplicate LSOA observations")
    missing = sorted(set(codes) - set(raw[LAD_CODE]))
    if missing:
        raise RetrievalError(f"LAD codes absent from the IoD 2025 release: {', '.join(missing)}")
    selected = (raw if national else raw.loc[raw[LAD_CODE].isin(codes)]).copy()
    selected = selected.reset_index(drop=True)
    source_cells = selected.copy()
    if len(selected) > request.max_rows:
        raise RetrievalError(
            f"Complete LAD selection has {len(selected)} rows, exceeding max_rows={request.max_rows}"
        )
    column_map = {column: _column_name(column) for column in raw.columns}
    if len(set(column_map.values())) != len(column_map):
        raise RetrievalError("Deprivation schema creates ambiguous normalised column names")
    flag_columns = {}
    # Read every field as text first. Numeric conversion applies only to measures;
    # non-numeric source tokens remain available in companion flag columns.
    for source, target in column_map.items():
        if source in IDENTIFIERS:
            continue
        text = selected[source]
        numeric = pd.to_numeric(text, errors="coerce").convert_dtypes()
        flagged = numeric.isna() & text.ne("")
        if flagged.any():
            flag_name = f"{target}_flag"
            if flag_name in column_map.values():
                raise RetrievalError("Deprivation schema creates an ambiguous flag column")
            selected[flag_name] = text.where(flagged, pd.NA).astype("string")
            flag_columns[target] = flag_name
        selected[source] = numeric
    frame = selected.rename(columns=column_map)
    acquired = Acquisition(
        provider=PROVIDER,
        dataset_id=DATASET_ID,
        title=TITLE,
        frame=frame,
        metadata={
            "release_page": RELEASE_URL,
            "publisher": "Ministry of Housing, Communities and Local Government",
            "access": "open",
            "raw_scope": "Complete official England File 7 CSV; no server-side subset",
            "raw_rows": len(raw),
            "raw_bytes": len(resource.body),
            "product_scope": (
                "Complete England File 7 release"
                if national
                else "All LSOAs in the requested Local Authority Districts"
            ),
            "product_rows": len(frame),
            "local_authority_codes": sorted(raw[LAD_CODE].unique()) if national else codes,
            "complete_selection": True,
            "source_columns": list(raw.columns),
            "column_map": column_map,
            "flag_columns": flag_columns,
            "missing_values": "Empty measures become null; non-numeric tokens retained as flags",
            "comparability": "Relative deprivation within England; editions are not directly comparable",
        },
        release={
            "edition": "2025",
            "published": "2025-10-30",
            "release_page_updated": "2025-11-17",
            "revision": "Corrected LSOA-to-LAD allocation; official asset 691ded56d140bbbaa59a2a7d",
            "lsoa_geography": "2021",
            "lad_geography": "2024",
            "population_reference_period": "mid-2022",
        },
        licence=LICENCE,
        citation=CITATION,
        source_url=resource.url,
    )
    from ssdu.normalization import normalize

    acquired.dataset = normalize(acquired, request, source_cells)
    return acquired
