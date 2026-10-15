"""One cached discovery-to-download workflow across supported provider services."""

from contextlib import contextmanager

from ssdu.connector_cache import CachedClient, seed_from_snapshots
from ssdu.models import Request
from ssdu.providers import get_provider
from ssdu.store import Store


class Connector:
    """Inspect provider metadata, plan a selection and retain reproducible data.

    Python and CLI calls may explicitly contact providers. Local REST/MCP reads use
    offline=True; acquisition remains in the durable worker. Native selection codes
    and supported formats are described by each provider's metadata.
    """

    def __init__(self, provider, root="data", *, client=None):
        self.registration = get_provider(provider)
        if not self.registration.workflow:
            raise ValueError(f"Common connector workflow is not implemented for {provider}")
        self.provider = provider
        self.store = root if isinstance(root, Store) else Store(root)
        self.client = client

    @contextmanager
    def _client(self, offline, refresh):
        if offline and refresh:
            raise ValueError("Offline requests cannot refresh providers")
        if offline:
            seed_from_snapshots(self.store, provider=self.provider)
        with CachedClient(
            self.store.root,
            client=self.client,
            offline=offline,
            refresh=refresh,
            allowed_hosts=self.registration.allowed_hosts,
        ) as client:
            yield client

    @staticmethod
    def _bounds(limit, offset):
        if (
            isinstance(limit, bool)
            or isinstance(offset, bool)
            or not isinstance(limit, int)
            or not isinstance(offset, int)
            or not 1 <= limit <= 100
            or not 0 <= offset <= 100000
        ):
            raise ValueError("Use limit=1..100 and offset=0..100000")

    def list_datasets(self, query="", *, limit=20, offset=0, offline=False, refresh=False):
        self._bounds(limit, offset)
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("Invalid catalogue query")
        with self._client(offline, refresh) as client:
            return self.registration.adapter().list_datasets(
                query, client, limit=limit, offset=offset
            )

    def describe_dataset(self, dataset_id, *, offline=False, refresh=False):
        with self._client(offline, refresh) as client:
            return self.registration.adapter().describe_dataset(dataset_id, client)

    def list_dimension_values(
        self, dataset_id, dimension, *, search="", limit=20, offset=0, offline=False, refresh=False
    ):
        self._bounds(limit, offset)
        if not isinstance(search, str) or len(search) > 512:
            raise ValueError("Invalid dimension query")
        with self._client(offline, refresh) as client:
            return self.registration.adapter().list_dimension_values(
                dataset_id, dimension, client, search=search, limit=limit, offset=offset
            )

    def plan_fetch(self, request, *, offline=False, refresh=False):
        if isinstance(request, dict):
            request = Request(**request)
        if request.provider != self.provider:
            raise ValueError("Request identifies a different provider")
        self.registration.validate_request(request)
        descriptor = self.describe_dataset(request.dataset_id, offline=offline, refresh=refresh)
        validator = getattr(self.registration.adapter(), "validate_selection", None)
        if validator is not None:
            validator(request, descriptor)
        return {
            "provider": self.provider,
            "request": request.to_dict(),
            "dataset": descriptor,
            "steps": [
                "resolve_metadata",
                "acquire_selection_or_distribution",
                "normalize",
                "verify_snapshot",
            ],
            "limits": {"max_rows": request.max_rows, "page_size": request.page_size},
            "selector_validation": "metadata" if validator else "requires_acquisition",
            "value_validation": "requires_acquisition",
            "historical_replay": "retained_snapshot",
            "provider_requests_for_values": 0,
        }

    def fetch(self, request, *, offline=False, refresh=False):
        if isinstance(request, dict):
            request = Request(**request)
        if request.provider != self.provider:
            raise ValueError("Request identifies a different provider")
        self.registration.validate_request(request)
        with self._client(offline, refresh) as client:
            return self.store.fetch(request, offline=offline, refresh=refresh, client=client)

    def fetch_dataset(self, request, *, offline=False, refresh=False):
        manifest = self.fetch(request, offline=offline, refresh=refresh)
        if "dataset_contract" not in manifest:
            return self.store.migrate_dataset(manifest["product_id"])
        return self.store.open_dataset(manifest["product_id"])
