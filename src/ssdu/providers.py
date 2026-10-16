"""Trusted built-in provider registrations; adapters and transforms load lazily.

Requests select registered identifiers, never Python module names. Adding a
provider means adding one reviewed registration and its conformance fixtures.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

from ssdu.models import Request


@dataclass(frozen=True)
class ProviderRegistration:
    id: str
    adapter_module: str
    normalizer_module: str
    normalizer_name: str
    transform_id: str
    transform_version: str
    allowed_hosts: frozenset[str]
    accepts_raw_frame: bool = False
    workflow: bool = False
    profile: str = "observations"

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.id):
            raise ValueError("Provider identifiers must be lowercase names")
        if self.adapter_module != f"ssdu.adapters.{self.id}":
            raise ValueError("Provider adapters must use their fixed ssdu.adapters module")
        if self.normalizer_module not in {"ssdu.normalization", f"ssdu.normalization_{self.id}"}:
            raise ValueError("Provider normalizers must use their fixed ssdu module")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.normalizer_name):
            raise ValueError("Invalid provider normalizer function")
        if not self.transform_id or not re.fullmatch(r"\d+\.\d+\.\d+", self.transform_version):
            raise ValueError("Provider transforms require an identifier and semantic version")
        if self.profile not in {"observations", "records"}:
            raise ValueError("Unsupported standardized dataset profile")
        if (
            not isinstance(self.allowed_hosts, frozenset)
            or not self.allowed_hosts
            or any(
                not isinstance(host, str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or ".." in host
                for host in self.allowed_hosts
            )
        ):
            raise ValueError("Provider hosts must be fixed lowercase HTTPS hostnames")

    def adapter(self):
        return importlib.import_module(self.adapter_module)

    def validate_request(self, request: Request):
        """Check local syntax without downloading metadata or native code lists."""
        if request.provider != self.id:
            raise ValueError("Request identifies a different provider")
        if self.id in DEFAULT_PROVIDERS:
            _validate_existing_request(request)
        else:
            self.adapter().validate_request(request)

    def normalize(self, acquisition, request=None, raw_frame=None):
        function = getattr(importlib.import_module(self.normalizer_module), self.normalizer_name)
        if self.accepts_raw_frame:
            return function(acquisition, request=request, raw_frame=raw_frame)
        return function(acquisition, request=request)

    def normalization_paths(self) -> tuple[Path, ...]:
        """All local interpretation code, including shared transform primitives."""
        root = Path(__file__).parent
        paths = [
            root / "dataset_contract.py",
            root / "normalization.py",
            Path(__file__),
            root / "adapters" / f"{self.id}.py",
        ]
        if self.normalizer_module != "ssdu.normalization":
            paths.append(root / f"normalization_{self.id}.py")
        if self.workflow:
            paths.extend(
                root / name
                for name in ("connectors.py", "connector_cache.py", "metadata_mapping.py")
            )
        if self.profile == "records":
            paths.append(root / "records.py")
        if self.id == "mhclg":
            paths.append(root / "distributions.py")
        if self.id == "scotland":
            paths.append(root / "adapters" / "scotland_ckan.py")
        return tuple(paths)

    def implementation_paths(self) -> tuple[Path, ...]:
        return tuple(
            dict.fromkeys(
                (Path(__file__).parent / "adapters" / f"{self.id}.py", *self.normalization_paths())
            )
        )


class ProviderRegistry:
    """An immutable set of reviewed registrations, rejecting duplicate identities."""

    def __init__(self, registrations):
        providers = {}
        for registration in registrations:
            if not isinstance(registration, ProviderRegistration):
                raise TypeError("Expected a ProviderRegistration")
            if registration.id in providers:
                raise ValueError(f"Duplicate provider registration: {registration.id}")
            providers[registration.id] = registration
        self._providers = MappingProxyType(providers)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def get(self, provider: str) -> ProviderRegistration:
        try:
            return self._providers[provider]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"Unknown provider: {provider}. Choose {', '.join(self.ids())}"
            ) from exc

    def hosts(self) -> frozenset[str]:
        return frozenset(
            host for provider in self._providers.values() for host in provider.allowed_hosts
        )


DEFAULT_PROVIDERS = ("nomis", "statswales", "deprivation")
REGISTRY = ProviderRegistry(
    (
        ProviderRegistration(
            "nomis",
            "ssdu.adapters.nomis",
            "ssdu.normalization",
            "nomis",
            "ssdu.nomis.observations",
            "1.0.0",
            frozenset({"www.nomisweb.co.uk"}),
            workflow=True,
        ),
        ProviderRegistration(
            "statswales",
            "ssdu.adapters.statswales",
            "ssdu.normalization",
            "statswales",
            "ssdu.statswales.observations",
            "1.1.0",
            frozenset({"api.stats.gov.wales"}),
            workflow=True,
        ),
        ProviderRegistration(
            "deprivation",
            "ssdu.adapters.deprivation",
            "ssdu.normalization",
            "deprivation",
            "ssdu.english-deprivation.observations",
            "1.0.0",
            frozenset({"deprivation.communities.gov.uk", "assets.publishing.service.gov.uk"}),
            accepts_raw_frame=True,
        ),
        ProviderRegistration(
            "nisra",
            "ssdu.adapters.nisra",
            "ssdu.normalization_nisra",
            "normalize",
            "ssdu.nisra.observations",
            "1.0.0",
            frozenset({"ws-data.nisra.gov.uk"}),
            accepts_raw_frame=True,
            workflow=True,
        ),
        ProviderRegistration(
            "scotland",
            "ssdu.adapters.scotland",
            "ssdu.normalization_scotland",
            "normalize",
            "ssdu.scotland.observations",
            "1.0.0",
            frozenset({"statistics.gov.scot", "api.data.gov.scot"}),
            accepts_raw_frame=True,
            workflow=True,
        ),
        ProviderRegistration(
            "mhclg",
            "ssdu.adapters.mhclg",
            "ssdu.normalization_mhclg",
            "normalize",
            "ssdu.mhclg.records",
            "1.0.0",
            frozenset(
                {"www.gov.uk", "assets.publishing.service.gov.uk", "open-data.communities.gov.uk"}
            ),
            accepts_raw_frame=True,
            workflow=True,
            profile="records",
        ),
    )
)


def provider_ids() -> tuple[str, ...]:
    return REGISTRY.ids()


def get_provider(provider: str) -> ProviderRegistration:
    return REGISTRY.get(provider)


def adapter(provider: str):
    return get_provider(provider).adapter()


def allowed_hosts() -> frozenset[str]:
    return REGISTRY.hosts()


def _validate_existing_request(request):
    if request.provider == "deprivation":
        module = adapter("deprivation")
        if request != module.national_request():
            module._selectors(request)
        return
    if request.provider == "nomis":
        if not re.fullmatch(r"NM_\d+_\d+", request.dataset_id):
            raise ValueError("Invalid Nomis identifier")
        if any(
            not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key)
            for key in request.params
        ):
            raise ValueError("Invalid Nomis selector names")
        for value in request.params.values():
            values = value if isinstance(value, list) else [value]
            if not values or any(
                isinstance(item, bool) or not isinstance(item, (str, int)) or item == ""
                for item in values
            ):
                raise ValueError("Invalid Nomis selector values")
        return
    UUID(request.dataset_id)
    if set(request.params) - {"filters", "lang", "sort_by"}:
        raise ValueError("Unsupported StatsWales parameters")
    if request.params.get("lang", "en-gb") not in {"en", "en-gb", "cy", "cy-gb"}:
        raise ValueError("Invalid StatsWales language")
    if "sort_by" in request.params and (
        not isinstance(request.params["sort_by"], str) or not request.params["sort_by"]
    ):
        raise ValueError("Invalid StatsWales sort")
    filters = request.params.get("filters", [])
    if not isinstance(filters, list) or any(
        not isinstance(item, dict)
        or not item
        or any(
            not isinstance(key, str)
            or not isinstance(values, list)
            or not values
            or any(not isinstance(code, str) or not code for code in values)
            for key, values in item.items()
        )
        for item in filters
    ):
        raise ValueError("Invalid StatsWales filters")
