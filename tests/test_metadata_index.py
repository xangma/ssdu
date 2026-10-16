"""Invented catalogues: current discovery never mutates a pinned interpretation."""

import copy
import hashlib
from datetime import UTC, datetime

import pytest

from ssdu.metadata_contract import MetadataError
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_store import MetadataStore


def publish(
    root,
    ids=("a", "b"),
    *,
    provider="nomis",
    day=1,
    checked=True,
    complete=True,
    authoritative=True,
    scope=None,
    title=None,
    deleted=(),
):
    clock = f"2026-10-{day:02d}T12:00:00+00:00"
    raw = b'{"notice":"Invented metadata; not provider observations"}'
    records = [
        {
            "provider": provider,
            "native_id": native,
            "dataset_id": f"{provider}:{native}",
            "title": title or f"Invented {native} housing statistics",
            "description": "Synthetic rents",
            "raw_resources": ["catalogue.json"],
            "references": [
                {"title": "Invented provider reference", "url": "https://example.org/catalogue"}
            ],
            "source_metadata": {"invented": True},
            "captured_at": clock,
            "last_checked_at": clock if checked else None,
            **({"deleted": True} if native in deleted else {}),
        }
        for native in ids
    ]
    report = {
        "provider": provider,
        "scope": scope or {"query": "", "dataset_ids": []},
        "completeness": {
            "status": "complete" if complete else "partial",
            "catalogue_complete": complete,
            "descriptions_complete": complete,
            "discovered_count": len(records),
            "described_count": len(records),
            "inventory_authoritative": authoritative and complete,
        },
        "freshness": {"captured_at": clock, "last_checked_at": clock if checked else None},
        "references": [{"title": "Invented catalogue", "url": "https://example.org/catalogue"}],
    }
    document = {
        "contract_version": "1.0.0",
        "captured_at": clock,
        "providers": [report],
        "records": records,
    }
    return MetadataStore(root).publish(document, {"catalogue.json": raw})


def index(root):
    return MetadataIndex(root, clock=lambda: datetime(2026, 10, 4, 12, tzinfo=UTC))


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_source_checks_prefer_description_and_survive_rebuild(reverse, tmp_path):
    store = MetadataStore(tmp_path)
    summary = publish(
        tmp_path,
        ("a",),
        authoritative=False,
        scope={"detail": "catalogue", "query": "", "dataset_ids": []},
    )
    rich = store.read(summary)
    rich["providers"][0]["scope"] = {"detail": "dataset", "query": "", "dataset_ids": ["a"]}
    rich["providers"][0]["completeness"]["catalogue_complete"] = False
    rich["records"][0]["source_metadata"] = {"descriptor": {"title": "Invented description"}}
    rich["records"][0]["mapping_version"] = "1.2.0"
    described = store.publish(
        rich, {"catalogue.json": b'{"notice":"Invented metadata; not provider observations"}'}
    )
    current = index(tmp_path)
    for snapshot in (described, summary) if reverse else (summary, described):
        current.import_snapshot(snapshot)
    before = current.get("nomis:a")
    assert before["metadata_snapshot_id"] == described
    assert before["metadata_profile"]["description_available"]
    current.rebuild()
    assert current.get("nomis:a") == before
    newer = publish(
        tmp_path,
        ("a",),
        day=2,
        authoritative=False,
        scope={"detail": "catalogue", "query": "", "dataset_ids": []},
    )
    current.import_snapshot(newer)
    assert current.get("nomis:a")["metadata_snapshot_id"] == newer
    assert not current.get("nomis:a")["metadata_profile"]["description_available"]
    assert current.get("nomis:a")["retained_description"]["metadata_snapshot_id"] == described


def test_verified_index_search_pagination_references_and_provider_identity(tmp_path):
    first = publish(tmp_path, ("same", "second"))
    second = publish(tmp_path, ("same",), provider="nisra")
    current = index(tmp_path)
    assert current.import_snapshot(first)["indexed"] == 2
    current.import_snapshot(second)
    page = current.search("housing", limit=1)
    assert page["total"] == 3 and page["next_offset"] == 1
    assert page["items"][0]["references"][0]["url"] == "https://example.org/catalogue"
    assert page["items"][0]["metadata_snapshot_id"] == second
    assert (
        page["items"][0]["raw_resource_hashes"]["catalogue.json"]
        == hashlib.sha256(b'{"notice":"Invented metadata; not provider observations"}').hexdigest()
    )
    assert current.search("hou rent", provider="nomis")["total"] == 2
    assert current.search("unknown")["total"] == 0
    with pytest.raises(ValueError, match="ambiguous"):
        current.get("same")
    assert current.get("nisra:same")["provider"] == "nisra"
    assert current.import_snapshot(first)["reused"] is True


def test_latest_metadata_updates_but_pinned_observations_do_not(tmp_path):
    from test_dataset_store import provider_client

    from ssdu import Store
    from ssdu.adapters.nomis import default_request
    from ssdu.registry import Registry

    store = Store(tmp_path)
    request = default_request()
    with provider_client() as client:
        dataset = store.fetch_dataset(request, client=client)
    registry = Registry(tmp_path)
    before = copy.deepcopy(registry.get(f"nomis:{request.dataset_id}"))
    contract = copy.deepcopy(dataset.contract)
    current = index(tmp_path)
    first = publish(tmp_path, (request.dataset_id,), title="Invented first description")
    second = publish(tmp_path, (request.dataset_id,), day=2, title="Invented revised description")
    current.import_snapshot(first)
    version = current.get(f"nomis:{request.dataset_id}")["metadata_version"]
    current.import_snapshot(second)
    assert current.get(f"nomis:{request.dataset_id}")["title"] == "Invented revised description"
    assert current.get(f"nomis:{request.dataset_id}")["metadata_version"] != version
    assert Registry(tmp_path).get(f"nomis:{request.dataset_id}") == before
    assert store.open_dataset(dataset.snapshot_id).contract == contract
    assert dataset.to_arrow().equals(store.open_dataset(dataset.snapshot_id).to_arrow())


def test_freshness_replay_keeps_provider_check_time_and_ignores_older_imports(tmp_path):
    current = index(tmp_path)
    newer = publish(tmp_path, day=3, title="Newer verified metadata")
    older = publish(tmp_path, day=1, title="Older metadata")
    offline = publish(tmp_path, day=4, checked=False, title="Unverified cached metadata")
    current.import_snapshot(newer)
    assert current.import_snapshot(older)["ignored_older"] == 2
    assert current.import_snapshot(offline)["ignored_older"] == 2
    row = current.get("nomis:a")
    assert row["title"] == "Newer verified metadata"
    assert row["freshness"]["last_checked_at"] == "2026-10-03T12:00:00+00:00"
    assert row["freshness"]["state"] == "fresh"
    assert current.get("nomis:a", max_age_seconds=1)["freshness"]["state"] == "stale"
    fresh = tmp_path / "fresh"
    sid = publish(fresh, checked=False)
    unknown = index(fresh)
    unknown.import_snapshot(sid)
    assert unknown.get("nomis:a")["freshness"]["state"] == "unknown"
    assert unknown.status()["providers"][0]["freshness"]["last_checked_at"] is None


def test_unchanged_metadata_content_version_ignores_check_clock(tmp_path):
    current = index(tmp_path)
    first, second = publish(tmp_path, day=1), publish(tmp_path, day=2)
    current.import_snapshot(first)
    version = current.get("nomis:a")["metadata_version"]
    current.import_snapshot(second)
    assert current.get("nomis:a")["metadata_version"] == version
    assert current.get("nomis:a")["metadata_snapshot_id"] == second


def test_partial_filtered_changed_scope_and_empty_inventory_preserve_existing(tmp_path):
    current = index(tmp_path)
    current.import_snapshot(publish(tmp_path))
    current.import_snapshot(publish(tmp_path, ("a",), day=2, complete=False))
    assert current.get("nomis:b")["status"] == "active"
    current.import_snapshot(
        publish(tmp_path, ("a",), day=3, scope={"query": "housing"}, authoritative=False)
    )
    assert current.get("nomis:b")["status"] == "active"
    current.import_snapshot(
        publish(tmp_path, ("a",), day=4, scope={"query": "", "dataset_ids": [], "language": "cy"})
    )
    assert current.get("nomis:b")["status"] == "active"
    current.import_snapshot(publish(tmp_path, (), day=5))
    assert current.get("nomis:b")["status"] == "active"
    current.import_snapshot(publish(tmp_path, ("a",), day=6, authoritative=False))
    assert current.get("nomis:b")["status"] == "active"


def test_complete_comparable_inventory_infers_absence_not_deletion(tmp_path):
    current = index(tmp_path)
    current.import_snapshot(publish(tmp_path))
    missing = publish(tmp_path, ("a",), day=3)
    assert current.import_snapshot(missing)["not_observed"] == 1
    row = current.get("nomis:b")
    assert row["status"] == "not_observed"
    assert row["status_evidence"] == {
        "assertion": "inferred_absence",
        "as_of": "2026-10-03T12:00:00+00:00",
        "snapshot_id": missing,
    }
    assert current.search()["total"] == 1
    assert current.search(include_retired=True)["total"] == 2
    # An older delayed page cannot resurrect an entity absent from a later inventory.
    current.import_snapshot(publish(tmp_path, ("b",), day=2, complete=False))
    assert current.get("nomis:b")["status"] == "not_observed"
    current.import_snapshot(publish(tmp_path, ("b",), day=4, complete=False))
    assert current.get("nomis:b")["status"] == "active"


def test_explicit_deletion_remains_distinct_and_hidden_from_default_search(tmp_path):
    current = index(tmp_path)
    current.import_snapshot(publish(tmp_path))
    current.import_snapshot(publish(tmp_path, day=2, deleted=("b",)))
    row = current.get("nomis:b")
    assert row["status"] == "deleted"
    assert row["status_evidence"]["assertion"] == "provider_deletion"
    assert current.search()["total"] == 1


def test_tamper_rejects_new_or_idempotent_import_without_changing_current(tmp_path):
    current = index(tmp_path)
    old = publish(tmp_path)
    current.import_snapshot(old)
    before = current.search()
    corrupt = publish(tmp_path, day=2)
    raw_path = MetadataStore(tmp_path).path(corrupt) / "resources" / "catalogue.json"
    raw_path.write_bytes(b"tampered")
    with pytest.raises(MetadataError):
        current.import_snapshot(corrupt)
    assert current.search() == before
    with pytest.raises(MetadataError):
        current.rebuild()
    assert current.search() == before
    raw_path = MetadataStore(tmp_path).path(old) / "resources" / "catalogue.json"
    raw_path.write_bytes(b"tampered")
    with pytest.raises(MetadataError):
        current.import_snapshot(old)


def test_offline_bundle_transfer_and_rebuild_are_deterministic(tmp_path, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("Discovery replay must not use the network")

    monkeypatch.setattr("ssdu.http.HttpClient.__init__", network_forbidden)
    source, destination = tmp_path / "source", tmp_path / "fresh"
    current = index(source)
    snapshots = [publish(source, day=1), publish(source, ("a",), day=2)]
    for sid in snapshots:
        current.import_snapshot(sid)
        bundle = MetadataStore(source).export(sid, tmp_path / f"{sid}.zip")
        assert MetadataStore(destination).import_bundle(bundle) == sid
    # Rebuild never relies on the original response cache, jobs, registry, or index.
    recovered = index(destination)
    assert recovered.rebuild()["snapshot_count"] == 2
    assert recovered.search(include_retired=True) == current.search(include_retired=True)
    assert recovered.status() == current.status()
    assert recovered.rebuild()["status"] == "rebuilt"
    assert recovered.search(include_retired=True) == current.search(include_retired=True)


@pytest.mark.parametrize(
    "kwargs", [{"limit": 0}, {"limit": True}, {"offset": -1}, {"max_age_seconds": float("nan")}]
)
def test_bounds_are_explicit(tmp_path, kwargs):
    current = index(tmp_path)
    current.import_snapshot(publish(tmp_path))
    with pytest.raises(ValueError):
        current.search(**kwargs)


def test_record_unknown_check_is_not_upgraded_by_other_provider_checks(tmp_path):
    store = MetadataStore(tmp_path)
    original = publish(tmp_path)
    document = store.read(original)
    document["records"][0]["last_checked_at"] = None
    sid = store.publish(document, {"catalogue.json": b"Invented response"})
    current = index(tmp_path)
    current.import_snapshot(sid)
    assert current.get("nomis:a")["freshness"]["state"] == "unknown"
    assert current.get("nomis:b")["freshness"]["state"] == "stale"


def test_history_retains_changed_and_unchanged_captures(tmp_path):
    current = index(tmp_path)
    captures = [
        publish(tmp_path, day=1),
        publish(tmp_path, day=2),
        publish(tmp_path, day=3, title="Changed provider assertion"),
    ]
    for sid in captures:
        current.import_snapshot(sid)
    first = current.history("nomis:a", limit=2)
    assert first["total"] == 3 and first["next_offset"] == 2
    assert first["items"][0]["snapshot_id"] == captures[-1]
    remaining = current.history("nomis:a", offset=2)
    assert first["items"][1]["metadata_version"] == remaining["items"][0]["metadata_version"]
    assert first["items"][0]["metadata_version"] != remaining["items"][0]["metadata_version"]


def test_empty_index_also_validates_freshness_and_search_budgets(tmp_path):
    current = index(tmp_path)
    with pytest.raises(ValueError, match="max_age_seconds"):
        current.search(max_age_seconds=-1)
    with pytest.raises(ValueError, match="max_age_seconds"):
        current.status(max_age_seconds=float("inf"))
    with pytest.raises(ValueError, match="64 terms"):
        current.search("word " * 65)
    assert current.search()["items"] == []
    assert current.status()["providers"] == []


def test_catalogue_refresh_exposes_scope_and_keeps_prior_description_separate(tmp_path):
    store = MetadataStore(tmp_path)
    draft = publish(tmp_path, ("a",), scope={"query": "", "detail": "dataset"})
    document = store.read(draft)
    document["records"][0]["dimensions"] = [{"id": "geography", "label": "Invented areas"}]
    document["records"][0]["source_metadata"]["descriptor"] = {"invented": True}
    detail = store.publish(document, {"catalogue.json": b"Invented detailed response"})
    summary = publish(
        tmp_path,
        ("a",),
        day=3,
        scope={"query": "", "detail": "catalogue"},
        title="New catalogue title",
    )
    current = index(tmp_path)
    current.import_snapshot(detail)
    current.import_snapshot(summary)
    row = current.get("nomis:a")
    assert row["title"] == "New catalogue title"
    assert row.get("dimensions", []) == []
    assert row["metadata_profile"]["requested_detail"] == "catalogue"
    assert row["metadata_profile"]["description_available"] is False
    assert row["metadata_profile"]["scope"] == {"query": "", "detail": "catalogue"}
    retained = row["retained_description"]
    assert retained["metadata_snapshot_id"] == detail
    assert retained["compatibility"] == "unverified" and retained["current"] is False
    old = current.get("nomis:a", snapshot_id=retained["metadata_snapshot_id"])
    assert old["status"] == "historical"
    assert old["dimensions"] == [{"id": "geography", "label": "Invented areas"}]
    assert old["freshness"]["last_checked_at"] == "2026-10-01T12:00:00+00:00"
    assert old["metadata_profile"]["description_available"] is True
    assert old["metadata_profile"]["requested_detail"] == "dataset"
    assert "retained_description" not in old
    with pytest.raises(KeyError):
        current.get("nomis:a", snapshot_id="a" * 64)


def test_freshness_accepts_epoch_clocks_and_keeps_future_checks_unknown(tmp_path):
    sid = publish(tmp_path, day=3)
    current = MetadataIndex(
        tmp_path, clock=lambda: datetime(2026, 10, 4, 12, tzinfo=UTC).timestamp()
    )
    current.import_snapshot(sid)
    assert current.get("nomis:a")["freshness"]["age_seconds"] == 86400
    assert current.status()["providers"][0]["freshness"]["state"] == "fresh"
    future = MetadataIndex(
        tmp_path, clock=lambda: datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp()
    )
    assert future.get("nomis:a")["freshness"]["state"] == "unknown"
