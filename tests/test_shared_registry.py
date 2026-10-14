import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pandas as pd
import pytest

from ssdu.adapters import deprivation
from ssdu.http import HttpClient
from ssdu.models import Request, RetrievalError
from ssdu.registry import Registry
from ssdu.shared import SharedDeprivation
from ssdu.store import Store, canonical


def client(body):
    return HttpClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))


def test_national_request_fetches_cold_reuses_offline_and_refreshes(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path)
    request = deprivation.national_request()
    with client(iod_csv) as transport:
        source = store.fetch(request, client=transport)
        assert transport.attempts == 1
    assert source["kind"] == "source" and source["rows"] == 4
    assert source["request"] == request.to_dict()
    assert len(store.list()) == 1
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    assert store.fetch(request, offline=True) == source
    monkeypatch.undo()
    with client(iod_csv.replace(b"12.375", b"13.375")) as transport:
        refreshed = store.fetch(request, refresh=True, client=transport)
        assert transport.attempts == 1
    assert refreshed["product_id"] != source["product_id"]
    assert store.fetch(request, offline=True) == refreshed
    assert store.verify(source["product_id"]) == source


def test_national_collection_missing_offline_and_replay_without_area_filter(
    tmp_path, iod_csv, monkeypatch
):
    from ssdu.collections import Collections

    store = Store(tmp_path)
    request = deprivation.national_request()
    spec = {
        "name": "national",
        "entries": {"deprivation": {"request": request.to_dict(), "query": {"limit": 10}}},
    }
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    with pytest.raises(RetrievalError, match="prepare online once"):
        Collections(store).resolve(spec, offline=True)
    monkeypatch.undo()
    with client(iod_csv) as transport:
        source = store.fetch(request, client=transport)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    lock = Collections(store).resolve(spec, offline=True)
    assert lock.entries["deprivation"].snapshot_id == source["product_id"]
    result = Collections(store).replay(lock)["entries"]["deprivation"]
    assert result["total_rows"] == 4
    assert len({row["local_authority_code"] for row in result["rows"]}) > 1


def test_shared_source_second_lad_is_offline_and_has_lineage(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path)
    with client(iod_csv) as transport:
        source = SharedDeprivation(store).source(client=transport)
        assert transport.attempts == 1
    assert source["rows"] == 4
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    first = store.fetch(
        Request("deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]})
    )
    second = store.fetch(
        Request("deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E09000001"]})
    )
    assert first["rows"] == 2 and second["rows"] == 1
    assert first["parent_snapshot_ids"] == second["parent_snapshot_ids"] == [source["product_id"]]
    assert not first["resources"] and not second["resources"]
    assert not list((store.path(second["product_id"]) / "raw").iterdir())
    pd.testing.assert_frame_equal(
        store.replay(first["product_id"]), store.read_intake(first["product_id"])
    )
    assert Registry(tmp_path).get(deprivation.DATASET_ID)["snapshots"][0]["kind"] == "source"


def test_unchanged_refresh_deduplicates_content_but_records_check(tmp_path, iod_csv):
    shared = SharedDeprivation(Store(tmp_path))
    with client(iod_csv) as transport:
        old = shared.source(client=transport)
        again = shared.source(client=transport, refresh=True)
    assert old == again
    assert len(shared.store.list()) == 1
    assert len(shared.registry.get(deprivation.DATASET_ID)["freshness"]["recent_checks"]) == 2


def test_changed_and_failed_refresh_preserve_prior_replay(tmp_path, iod_csv):
    store = Store(tmp_path)
    shared = SharedDeprivation(store)
    with client(iod_csv) as transport:
        old = shared.source(client=transport)
    old_bytes = (store.path(old["product_id"]) / "data.parquet").read_bytes()
    with client(iod_csv.replace(b"12.375", b"13.375")) as transport:
        new = shared.source(client=transport, refresh=True)
    assert old["product_id"] != new["product_id"]
    assert (store.path(old["product_id"]) / "data.parquet").read_bytes() == old_bytes
    assert store.replay(old["product_id"]).loc[1, "imd_score"] == 12.375
    with client(b"<html>broken</html>") as transport, pytest.raises(RetrievalError):
        shared.source(client=transport, refresh=True)
    assert shared.source(offline=True)["product_id"] == new["product_id"]
    assert (
        shared.registry.get(deprivation.DATASET_ID)["freshness"]["recent_checks"][0]["status"]
        == "refresh_failed"
    )


def test_legacy_capture_bootstraps_national_source_without_network(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path)
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
    )
    with client(iod_csv) as transport:
        key = hashlib.sha256(canonical(request.to_dict())).hexdigest()
        legacy = store._acquire(request, key, transport)
    assert legacy["schema_version"] == 1
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    source = SharedDeprivation(store).source(offline=True)
    assert source["rows"] == 4 and source["schema_version"] == 2
    assert (store.path(source["product_id"]) / "raw/0000.bin").stat().st_ino == (
        store.path(legacy["product_id"]) / "raw/0000.bin"
    ).stat().st_ino
    assert (
        SharedDeprivation(store).registry.get(deprivation.DATASET_ID)["freshness"][
            "provider_check_performed"
        ]
        is False
    )


def test_empty_offline_preparation_fails_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    with pytest.raises(RetrievalError, match="prepare online once"):
        SharedDeprivation(Store(tmp_path)).source(offline=True)


def test_offline_fetch_can_derive_new_selection_but_cannot_download(tmp_path, iod_csv, monkeypatch):
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    store = Store(tmp_path)
    request = Request(
        "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
    )
    with pytest.raises(RetrievalError, match="prepare online once"):
        store.fetch(request, offline=True)
    monkeypatch.undo()
    with client(iod_csv) as transport:
        source = SharedDeprivation(store).source(client=transport)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    selected = store.fetch(request, offline=True)
    assert selected["rows"] == 2
    assert selected["parent_snapshot_ids"] == [source["product_id"]]


def test_registry_restart_search_versions_and_partial_import(tmp_path, iod_csv):
    store = Store(tmp_path)
    registry = Registry(tmp_path)
    seed = registry.get(deprivation.DATASET_ID)
    assert not seed["availability"]["materialised"]
    with client(iod_csv) as transport:
        source = SharedDeprivation(store).source(client=transport)
    registry = Registry(tmp_path)
    current = registry.get(deprivation.DATASET_ID)
    assert current["metadata_version"] != seed["metadata_version"]
    assert registry.search("deprivation", provider="deprivation")["total"] == 1
    assert registry.search('" OR * - drop table;')["total"] == 0
    with registry.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM metadata_versions WHERE dataset_id=?",
                (current["dataset_id"],),
            ).fetchone()[0]
            == 2
        )
    # Empty/incomplete imports are additive; existing catalogue entries remain.
    registry.import_store(Store(tmp_path / "empty"))
    assert (
        registry.get(deprivation.DATASET_ID)["snapshots"][0]["snapshot_id"] == source["product_id"]
    )


def test_parent_corruption_fails_derived_replay(tmp_path, iod_csv):
    store = Store(tmp_path)
    with client(iod_csv) as transport:
        selected = store.fetch(
            Request(
                "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
            ),
            client=transport,
        )
    source_id = selected["parent_snapshot_ids"][0]
    (store.path(source_id) / "raw/0000.bin").write_bytes(b"bad")
    with pytest.raises(RetrievalError, match="hash mismatch"):
        store.replay(selected["product_id"])


def test_request_index_remains_correct_for_derived_selection(tmp_path, iod_csv):
    store = Store(tmp_path)
    with client(iod_csv) as transport:
        selected = store.fetch(
            Request(
                "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
            ),
            client=transport,
        )
    assert selected["product_id"] in json.loads((tmp_path / "latest.json").read_text()).values()


def test_reverted_source_is_current_after_deduplication_and_restart(tmp_path, iod_csv):
    from ssdu.service import Service

    store = Store(tmp_path)
    shared = SharedDeprivation(store)
    with client(iod_csv) as transport:
        first = shared.source(client=transport)
    lines = iod_csv.splitlines(keepends=True)
    without_london = b"".join([lines[0], *lines[2:]])
    with client(without_london) as transport:
        second = shared.source(refresh=True, client=transport)
    assert second["product_id"] != first["product_id"]
    with client(iod_csv) as transport:
        reverted = shared.source(refresh=True, client=transport)
    assert reverted["product_id"] == first["product_id"]
    data = Service(tmp_path)
    described = data.describe_dataset(deprivation.DATASET_ID)
    assert described["current_snapshot_id"] == first["product_id"]
    assert described["source_metadata"]["raw_rows"] == 4
    values = data.list_dimension_values(deprivation.DATASET_ID, "local_authority_code", "London")
    assert values["snapshot_id"] == first["product_id"]
    assert values["items"] == [{"value": "E09000001", "label": "City of London"}]
    (tmp_path / "sources.json").unlink()
    assert shared.source(offline=True)["product_id"] == first["product_id"]


def test_missing_source_index_recovers_without_new_capture(tmp_path, iod_csv):
    shared = SharedDeprivation(Store(tmp_path))
    with client(iod_csv) as transport:
        source = shared.source(client=transport)
    (tmp_path / "sources.json").unlink()
    assert shared.source(offline=True)["product_id"] == source["product_id"]
    assert len(shared.store.list()) == 1


def test_request_index_rebuild_respects_reverted_shared_source(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path)
    shared = SharedDeprivation(store)
    with client(iod_csv) as transport:
        original = shared.source(client=transport)
    with client(iod_csv.replace(b"12.375", b"13.375")) as transport:
        shared.source(refresh=True, client=transport)
    with client(iod_csv) as transport:
        assert shared.source(refresh=True, client=transport)["product_id"] == original["product_id"]
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    (tmp_path / "latest.json").unlink()
    assert store.rebuild_index()["recovered_entries"] == 1
    assert (
        store.fetch(deprivation.national_request(), offline=True)["product_id"]
        == original["product_id"]
    )


def test_source_index_cannot_substitute_lad_selection(tmp_path, iod_csv):
    shared = SharedDeprivation(Store(tmp_path))
    with client(iod_csv) as transport:
        selected = shared.fetch(
            Request(
                "deprivation", deprivation.DATASET_ID, {"local_authority_codes": ["E06000044"]}
            ),
            client=transport,
        )
    (tmp_path / "sources.json").write_text(json.dumps({shared.key: selected["product_id"]}))
    with pytest.raises(RetrievalError, match="different dataset or selection"):
        shared.source(offline=True)


def test_concurrent_equivalent_selections_publish_once(tmp_path, iod_csv, monkeypatch):
    store = Store(tmp_path)
    with client(iod_csv) as transport:
        SharedDeprivation(store).source(client=transport)
    monkeypatch.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Provider access"))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: Store(tmp_path).fetch(
                    Request(
                        "deprivation",
                        deprivation.DATASET_ID,
                        {"local_authority_codes": ["E06000044"]},
                    )
                ),
                range(4),
            )
        )
    assert len({m["product_id"] for m in results}) == 1
    assert len(store.list()) == 2  # One source, one derived selection.


def test_bad_historical_snapshot_is_quarantined_without_disabling_current_data(tmp_path, iod_csv):
    from ssdu.contracts import QuerySpec
    from ssdu.service import Service

    shared = SharedDeprivation(Store(tmp_path))
    with client(iod_csv) as transport:
        old = shared.source(client=transport)
    with client(iod_csv.replace(b"12.375", b"13.375")) as transport:
        current = shared.source(refresh=True, client=transport)
    (shared.store.path(old["product_id"]) / "raw/0000.bin").write_bytes(b"corrupt")
    data = Service(tmp_path)
    assert data.query_data(current["product_id"], QuerySpec(count_only=True))["rows"] == [
        {"count": 4}
    ]
    products = data.describe_dataset(deprivation.DATASET_ID)["snapshots"]
    assert (
        next(p for p in products if p["snapshot_id"] == old["product_id"])["integrity_status"]
        == "verification_failed"
    )
    with pytest.raises(RetrievalError, match="hash mismatch"):
        data.query_data(old["product_id"], QuerySpec(count_only=True))
