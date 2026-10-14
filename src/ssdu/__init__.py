"""Local data management, immutable collections and UK data discovery services."""

__version__ = "0.3.0"

__all__ = ["Dataset", "Request", "Store", "get_source_coverage"]


def __getattr__(name):
    """Keep the public SDK convenient without importing storage during package setup."""
    if name == "Dataset":
        from ssdu.dataset_contract import Dataset

        return Dataset
    if name == "Request":
        from ssdu.models import Request

        return Request
    if name == "Store":
        from ssdu.store import Store

        return Store
    if name == "get_source_coverage":
        from ssdu.source_coverage import get_source_coverage

        return get_source_coverage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
