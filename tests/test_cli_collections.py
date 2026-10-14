import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from ssdu.cli import main
from ssdu.http import HttpClient
from ssdu.models import RetrievalError

SNAPSHOT_ID = "a" * 64
LOCK_ID = "b" * 64


@pytest.fixture(autouse=True)
def no_provider_http(monkeypatch):
    monkeypatch.setattr(
        HttpClient, "request", lambda *args, **kwargs: pytest.fail("Collection CLI attempted HTTP")
    )


@pytest.fixture
def cli_fakes(tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], failure=None)
    locked = SimpleNamespace(
        model_dump=lambda: {
            "lock_id": LOCK_ID,
            "spec": {"name": "sample"},
            "entries": {"table": {"snapshot_id": SNAPSHOT_ID}},
        }
    )

    class FakeStore:
        def __init__(self, root):
            self.root = Path(root)

        def catalogue(self, product_ids=None, output=None):
            state.calls.append(("catalogue", product_ids, output))
            return output

    class FakeCollections:
        def __init__(self, store):
            self.store = store

        def resolve(self, spec, *, offline=False, refresh=False):
            state.calls.append(("resolve", spec, offline, refresh))
            return locked

        def get(self, ref):
            state.calls.append(("get", ref))
            if state.failure:
                raise state.failure
            return locked

        def list(self):
            state.calls.append(("list",))
            return [{"name": "sample", "current_lock_id": LOCK_ID}]

        def replay(self, ref):
            state.calls.append(("replay", ref))
            return {"lock_id": LOCK_ID, "results": {"table": {"rows": [{"value": 1}]}}}

    def read_document(path):
        state.calls.append(("read_document", path))
        return yaml.safe_load(path.read_text())

    def import_local(store, path, spec):
        state.calls.append(("import_local", path, spec))
        return {
            "product_id": SNAPSHOT_ID,
            "provider": "local",
            "dataset_id": "fixture",
            "title": "Fixture table",
            "rows": 1,
            "release": {"kind": "local"},
            "created_at": "2026-10-13T00:00:00Z",
        }

    def export_bundle(store, lock, output):
        state.calls.append(("export_bundle", lock, output))
        return {"lock_id": LOCK_ID, "path": str(output)}

    def import_bundle(store, bundle, *, activate=False):
        state.calls.append(("import_bundle", bundle, activate))
        return {"lock_id": LOCK_ID, "activated": activate}

    for name, attributes in (
        ("ssdu.collections", {"Collections": FakeCollections, "read_document": read_document}),
        ("ssdu.local_import", {"import_local": import_local}),
        ("ssdu.bundles", {"export_bundle": export_bundle, "import_bundle": import_bundle}),
    ):
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr("ssdu.cli.Store", FakeStore)
    state.store = tmp_path / "store"
    state.spec = tmp_path / "spec.yaml"
    state.spec.write_text(
        "name: sample\nentries:\n  table:\n    snapshot_id: " + SNAPSHOT_ID + "\n"
    )
    state.args = ["--store", str(state.store)]
    return state


@pytest.mark.parametrize(
    "flag,offline,refresh",
    [(None, False, False), ("--offline", True, False), ("--refresh", False, True)],
)
def test_collection_lock_forwards_resolution_policy(cli_fakes, capsys, flag, offline, refresh):
    args = [*cli_fakes.args, "collection", "lock", str(cli_fakes.spec)]
    assert main(args + ([flag] if flag else [])) == 0
    assert cli_fakes.calls[-1] == (
        "resolve",
        {"name": "sample", "entries": {"table": {"snapshot_id": SNAPSHOT_ID}}},
        offline,
        refresh,
    )
    assert json.loads(capsys.readouterr().out)["lock_id"] == LOCK_ID


def test_collection_list_show_and_replay(cli_fakes, capsys):
    assert main([*cli_fakes.args, "collection", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["current_lock_id"] == LOCK_ID
    for ref in ("sample", LOCK_ID, str(cli_fakes.spec)):
        assert main([*cli_fakes.args, "collection", "show", ref]) == 0
        assert cli_fakes.calls[-1] == ("get", ref)
        assert json.loads(capsys.readouterr().out)["entries"]["table"]["snapshot_id"] == SNAPSHOT_ID
    assert main([*cli_fakes.args, "collection", "replay", "sample"]) == 0
    assert json.loads(capsys.readouterr().out)["results"]["table"]["rows"] == [{"value": 1}]


def test_collection_export_dispatch(cli_fakes, tmp_path, capsys):
    target = tmp_path / "bundle.tar.gz"
    assert main([*cli_fakes.args, "collection", "export", "sample", "--output", str(target)]) == 0
    assert cli_fakes.calls[-2] == ("get", "sample")
    assert cli_fakes.calls[-1][0] == "export_bundle" and cli_fakes.calls[-1][2] == target
    assert json.loads(capsys.readouterr().out)["path"] == str(target)


@pytest.mark.parametrize("activate", [False, True])
def test_collection_import_activation_policy(cli_fakes, tmp_path, capsys, activate):
    bundle = tmp_path / "bundle.tar.gz"
    args = [*cli_fakes.args, "collection", "import", str(bundle)] + (
        ["--activate"] if activate else []
    )
    assert main(args) == 0
    assert cli_fakes.calls[-1] == ("import_bundle", bundle, activate)
    assert json.loads(capsys.readouterr().out)["activated"] == activate


@pytest.mark.parametrize("format", ["json", "yaml"])
def test_import_local_metadata_and_isolated_catalogue(cli_fakes, tmp_path, capsys, format):
    table = tmp_path / "table.csv"
    table.write_text("code,value\n001,1\n")
    metadata = tmp_path / f"metadata.{format}"
    spec = {"dataset_id": "fixture", "title": "Fixture table"}
    metadata.write_text(json.dumps(spec) if format == "json" else yaml.safe_dump(spec))
    assert main([*cli_fakes.args, "import-local", str(table), "--metadata", str(metadata)]) == 0
    assert cli_fakes.calls[-2] == ("import_local", table, spec)
    assert cli_fakes.calls[-1][1] == [SNAPSHOT_ID]
    result = json.loads(capsys.readouterr().out)
    assert result["product_id"] == SNAPSHOT_ID
    assert result["catalogue"] == str(cli_fakes.store / f"catalog-{SNAPSHOT_ID}.yaml")


@pytest.mark.parametrize(
    "args",
    [
        ["collection"],
        ["collection", "lock", "spec.json", "--offline", "--refresh"],
        ["collection", "show", "sample", "--refresh"],
        ["collection", "export", "sample"],
        ["collection", "import", "bundle.tar.gz", "--overwrite"],
        ["import-local", "table.csv"],
        ["import-local", "table.csv", "--metadata", "meta.json", "--refresh"],
    ],
)
def test_collection_parser_rejects_invalid_arguments_before_mutation(cli_fakes, args):
    with pytest.raises(SystemExit) as caught:
        main([*cli_fakes.args, *args])
    assert caught.value.code == 2
    assert cli_fakes.calls == []


def test_collection_lookup_error_is_failure(cli_fakes, capsys):
    cli_fakes.failure = RetrievalError("Collection not retained")
    assert main([*cli_fakes.args, "collection", "show", "missing"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "Collection not retained" in output.err


def test_real_local_collection_transfer_and_activation_offline(tmp_path, capsys):
    """Import a local table, pin a query, transfer it and preserve existing collection current."""
    source_store = tmp_path / "source-store"
    destination_store = tmp_path / "destination-store"
    table = tmp_path / "table.csv"
    table.write_text("code,value\n001,4\n002,7\n")
    metadata = tmp_path / "metadata.yaml"
    metadata.write_text(
        yaml.safe_dump(
            {
                "dataset_id": "cli-fixture",
                "title": "CLI local fixture",
                "description": "Generated two-row test table",
                "licence": "CC0-1.0",
                "citation": "Generated test fixture",
                "column_types": {"code": "string", "value": "int64"},
                "columns": {
                    "code": {"label": "Test code", "definition": "Fixture identifier"},
                    "value": {"definition": "Fixture measurement", "unit": "count"},
                },
            }
        )
    )
    source = ["--store", str(source_store)]
    destination = ["--store", str(destination_store)]

    def run(args):
        assert main(args) == 0
        return json.loads(capsys.readouterr().out)

    imported = run([*source, "import-local", str(table), "--metadata", str(metadata)])
    snapshot_id = imported["product_id"]
    assert imported["provider"] == "local" and imported["rows"] == 2
    assert Path(imported["catalogue"]).is_file()
    spec = {
        "name": "local-project",
        "entries": {
            "survey": {
                "snapshot_id": snapshot_id,
                "query": {
                    "columns": ["code", "value"],
                    "filters": [{"column": "value", "op": "gte", "value": 5}],
                },
            }
        },
    }
    spec_path = tmp_path / "collection.yaml"
    spec_path.write_text(yaml.safe_dump(spec))
    locked = run([*source, "collection", "lock", str(spec_path), "--offline"])
    lock_id = locked["lock_id"]
    assert locked["entries"]["survey"]["snapshot_id"] == snapshot_id
    lock_path = source_store / "collections" / "local-project" / "locks" / f"{lock_id}.json"
    for ref in ("local-project", lock_id, str(lock_path)):
        assert run([*source, "collection", "show", ref])["lock_id"] == lock_id
    original = run([*source, "collection", "replay", lock_id])
    assert original["entries"]["survey"]["rows"] == [{"code": "002", "value": 7}]
    assert original["entries"]["survey"]["provenance"]["collection_lock_id"] == lock_id
    # The frozen snapshot and lock continue to work after the original file is removed.
    table.unlink()
    bundle = tmp_path / "collection.tar.gz"
    assert (
        run([*source, "collection", "export", lock_id, "--output", str(bundle)])["lock_id"]
        == lock_id
    )
    transferred = run([*destination, "collection", "import", str(bundle)])
    assert transferred["lock_id"] == lock_id and transferred["current"] is True
    assert run([*destination, "collection", "replay", "local-project"]) == original
    assert run([*destination, "collection", "list"])[0]["current_lock_id"] == lock_id

    spec["entries"]["survey"]["query"]["filters"][0]["value"] = 0
    spec_path.write_text(yaml.safe_dump(spec))
    later = run([*destination, "collection", "lock", str(spec_path), "--offline"])
    later_id = later["lock_id"]
    assert later_id != lock_id
    assert (
        len(
            run([*destination, "collection", "replay", "local-project"])["entries"]["survey"][
                "rows"
            ]
        )
        == 2
    )
    transferred = run([*destination, "collection", "import", str(bundle)])
    assert transferred["current"] is False
    assert run([*destination, "collection", "show", "local-project"])["lock_id"] == later_id
    assert run([*destination, "collection", "replay", lock_id]) == original
    assert run([*destination, "collection", "import", str(bundle), "--activate"])["current"] is True
    assert run([*destination, "collection", "show", "local-project"])["lock_id"] == lock_id
