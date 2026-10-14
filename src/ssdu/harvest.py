"""Public metadata acquisition shares verified checkpoints and the host rate ledger."""

from ssdu.literature_ingest import HOSTS, LiteratureClient
from ssdu.models import RetrievalError

METADATA_HOSTS = HOSTS | frozenset({"www.nomisweb.co.uk", "api.stats.gov.wales", "www.gov.uk"})


class MetadataClient(LiteratureClient):
    def __init__(self, root, *, client=None, max_requests=150, max_total_bytes=100_000_000):
        super().__init__(
            root,
            client=client,
            allowed_hosts=METADATA_HOSTS,
            cache_namespace="harvest",
            max_requests=max_requests,
            max_total_bytes=max_total_bytes,
        )
        self.exhausted_hosts = set()

    def before_request(self, host):
        if host in self.exhausted_hosts:
            raise RetrievalError("Observed provider daily quota exhausted; resume later from cache")

    def get(self, url, *, params=None, offline=False, refresh=False):
        from urllib.parse import urlparse

        capture = super().get(url, params=params, offline=offline, refresh=refresh)
        remaining = capture.metadata["headers"].get("x-ratelimit-remaining")
        if remaining is not None and not capture.cache_hit:
            try:
                exhausted = float(remaining) <= 0
            except ValueError:
                exhausted = False
            if exhausted:
                self.exhausted_hosts.add(urlparse(url).hostname)
        return capture
