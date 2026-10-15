"""Local data management, immutable collections and UK data discovery services."""

__version__ = "0.3.0"

__all__ = [
    "Connector",
    "Dataset",
    "DatasetQuerySpec",
    "LocalImportSpec",
    "MetadataIndex",
    "MetadataStore",
    "MetadataSync",
    "Request",
    "Service",
    "Store",
    "get_source_coverage",
]


def __getattr__(name):
    """Keep the public SDK convenient without importing storage during package setup."""
    if name == "MetadataSync":
        from ssdu.metadata_sync import MetadataSync

        return MetadataSync
    if name == "MetadataStore":
        from ssdu.metadata_store import MetadataStore

        return MetadataStore
    if name == "MetadataIndex":
        from ssdu.metadata_index import MetadataIndex

        return MetadataIndex
    if name == "Connector":
        from ssdu.connectors import Connector

        return Connector
    if name == "Dataset":
        from ssdu.dataset_contract import Dataset

        return Dataset
    if name == "Request":
        from ssdu.models import Request

        return Request
    if name == "Store":
        from ssdu.store import Store

        return Store
    if name == "Service":
        from ssdu.service import Service

        return Service
    if name == "DatasetQuerySpec":
        from ssdu.contracts import DatasetQuerySpec

        return DatasetQuerySpec
    if name == "LocalImportSpec":
        from ssdu.local_import import LocalImportSpec

        return LocalImportSpec
    if name == "get_source_coverage":
        from ssdu.source_coverage import get_source_coverage

        return get_source_coverage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
