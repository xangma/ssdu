import pytest
from test_deprivation import iod_csv  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_provider_limits(tmp_path, monkeypatch):
    """Fixture HTTP never sleeps or changes the operator's live quota ledger."""
    monkeypatch.setenv("SSDU_RATE_DB", str(tmp_path / "rate.sqlite"))
    monkeypatch.setenv("SSDU_RATE_INTERVAL_SECONDS", "0")
