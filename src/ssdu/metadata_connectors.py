"""Reviewed public-metadata connectors, independent of observation retrieval.

Registering a catalogue only enables metadata capture. It neither registers a
data adapter nor grants access to the datasets described by that catalogue.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit


def _https_url(value, *, allowed_hosts=None):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Metadata connector URLs must be bounded HTTPS URLs")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or (allowed_hosts is not None and parsed.hostname not in allowed_hosts)
    ):
        raise ValueError("Metadata connector URLs must use reviewed HTTPS hosts")
    return value.rstrip("/")


@dataclass(frozen=True)
class MetadataConnectorRegistration:
    """A fixed metadata protocol and tenant, with no data-fetch capability.

    Custom registrations require an explicit API base and catalogue scope.
    SSDU does not guess API hosts from browser URLs or discover executable code.
    """

    id: str
    api_base: str
    catalogue_url: str
    allowed_hosts: frozenset[str]
    scope: str
    family: str = "metadataworks"
    api_version: str = "2.0"

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", self.id):
            raise ValueError("Metadata connector identifiers must be lowercase names")
        if self.family != "metadataworks" or self.api_version != "2.0":
            raise ValueError("Unsupported metadata connector protocol or API version")
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
            raise ValueError("Metadata connector hosts must be fixed lowercase hostnames")
        if not isinstance(self.scope, str) or not self.scope.strip() or len(self.scope) > 2000:
            raise ValueError("Metadata connectors require an explicit bounded catalogue scope")
        object.__setattr__(
            self, "api_base", _https_url(self.api_base, allowed_hosts=self.allowed_hosts)
        )
        object.__setattr__(self, "catalogue_url", _https_url(self.catalogue_url))

    @property
    def name(self):
        return self.id

    @property
    def headers(self):
        return MappingProxyType({"X-API-Version": self.api_version})

    @property
    def fingerprint(self):
        value = {
            "id": self.id,
            "family": self.family,
            "api_version": self.api_version,
            "api_base": self.api_base,
            "catalogue_url": self.catalogue_url,
            "allowed_hosts": sorted(self.allowed_hosts),
            "scope": self.scope,
            "mapping_version": "1.0.0",
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def cache_scope(self):
        """Isolate responses whose meaning depends on protocol headers and tenant."""
        return f"metadata:{self.family}:{self.api_version}:{self.fingerprint}"

    def backend(self):
        from ssdu.adapters.metadataworks import MetadataWorksBackend

        return MetadataWorksBackend(self)


class MetadataConnectorRegistry:
    """An immutable set of explicit metadata-only registrations."""

    def __init__(self, registrations):
        connectors = {}
        for registration in registrations:
            if not isinstance(registration, MetadataConnectorRegistration):
                raise TypeError("Expected a MetadataConnectorRegistration")
            if registration.id in connectors:
                raise ValueError(f"Duplicate metadata connector: {registration.id}")
            connectors[registration.id] = registration
        self._connectors = MappingProxyType(connectors)

    def get(self, connector):
        try:
            return self._connectors[connector]
        except (TypeError, KeyError) as exc:
            raise ValueError(f"Unknown metadata connector: {connector}") from exc

    def registrations(self):
        return tuple(self._connectors.values())


REGISTRY = MetadataConnectorRegistry(
    (
        MetadataConnectorRegistration(
            "adr_uk",
            "https://api-datacatalogue.adruk.org/api",
            "https://datacatalogue.adruk.org",
            frozenset({"api-datacatalogue.adruk.org"}),
            "Public ADR UK federated catalogue; source origins and partnership membership remain distinct",
        ),
        MetadataConnectorRegistration(
            "nisra_secure",
            "https://api-nisra.metadata.works/api",
            "https://nisra.metadata.works",
            frozenset({"api-nisra.metadata.works"}),
            "Public NISRA secure-research catalogue metadata, excluding underlying restricted records",
        ),
        MetadataConnectorRegistration(
            "research_data_scotland",
            "https://rds-api.metadata.works/api",
            "https://www.researchdata.scot/metadata-catalogue/",
            frozenset({"rds-api.metadata.works"}),
            "Public Research Data Scotland catalogue; not every record is an ADR Scotland partnership dataset",
        ),
    )
)


def get_metadata_connector(connector, *, registry=REGISTRY):
    """Resolve a reviewed metadata connector without importing a data adapter."""
    if isinstance(connector, MetadataConnectorRegistration):
        return connector
    return registry.get(connector)


def list_metadata_connectors(*, registry=REGISTRY):
    """Return the reviewed registrations, not observation-retrieval providers."""
    return registry.registrations()


def register_metadata_connector(registration, *, registry=REGISTRY):
    """Return a new registry with one reviewed tenant; leave built-ins unchanged."""
    return MetadataConnectorRegistry((*registry.registrations(), registration))
