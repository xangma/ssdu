"""Invented provider mappings survive verified transfer with no original store or HTTP."""

from unittest.mock import patch

import pytest
from test_metadata_sync import FIXTURE, snapshot, spec, submit, sync_for

from ssdu.http import HttpClient
from ssdu.metadata_index import MetadataIndex
from ssdu.metadata_mapping import MAPPING_VERSION
from ssdu.metadata_store import MetadataStore
from ssdu.metadata_sync import MetadataSync


@pytest.mark.parametrize("provider", FIXTURE.DATASETS)
def test_mapped_fields_and_source_evidence_replay_from_portable_snapshot(provider, tmp_path):
    source, fresh = tmp_path / "source", tmp_path / "fresh"
    sync, calls, clock = sync_for(source)
    configuration = spec(provider, detail="dataset")
    job, document = snapshot(sync, submit(sync, configuration))
    sid = job["result"]["snapshot_id"]
    row = document["records"][0]
    assert document["contract_version"] == "1.0.0"
    assert row["mapping_version"] == MAPPING_VERSION
    mapping = row["source_metadata"]["mapping"]
    assert mapping["version"] == MAPPING_VERSION
    assert any(entry["field"] == "access" for entry in mapping["evidence"])
    for entry in mapping["evidence"]:
        assert entry["source_path"]
        assert entry["resource_paths"]
        assert set(entry["resource_paths"]) <= set(row["raw_resources"])
    store = MetadataStore(source)
    manifest = store.verify(sid)
    bundle = store.export(sid, tmp_path / "invented-metadata.zip")
    source.rename(tmp_path / "unavailable-original")
    before = len(calls)
    with patch.object(HttpClient, "request", side_effect=AssertionError("Offline HTTP")):
        transferred = MetadataStore(fresh)
        assert transferred.import_bundle(bundle) == sid
        assert transferred.verify(sid) == manifest
        assert transferred.read(sid) == document
        replay = MetadataSync(fresh, clock=clock)
        plan = replay.plan_sync(configuration, offline=True)
        offline = replay.submit_plan(plan["plan_id"], idempotency_key="invented-mapping-replay")
        result = replay.run_job(offline["job_id"])
        assert result["status"] == "succeeded" and result["requests"] == 0, result
        repeated = transferred.read(result["result"]["snapshot_id"])
        assert repeated["records"] == document["records"]
        assert repeated["providers"] == document["providers"]
        index = MetadataIndex(fresh)
        current = index.get(row["dataset_id"])
        assert current["description"] == row["description"]
        assert current["measures"] == row["measures"]
        assert current["coverage"] == row["coverage"]
        assert current["licence"] == row["licence"]
        assert current["access"] == row["access"]
        assert current["source_metadata"]["mapping"] == mapping
        index.rebuild()
        assert index.get(row["dataset_id"])["source_metadata"]["mapping"] == mapping
    assert len(calls) == before
    assert MetadataStore(tmp_path / "unavailable-original").read(sid) == document
