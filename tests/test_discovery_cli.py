import gzip
import json

import pytest
import test_discovery_workflow as workflow_fixtures

from ssdu.cli import main
from ssdu.discovery_cli import read_json
from ssdu.discovery_workflow import write_json


@pytest.fixture
def workflow_inputs(tmp_path):
    return workflow_fixtures.workflow_inputs.__wrapped__(tmp_path)


def test_review_cli_exports_and_imports_an_unjudged_pack(workflow_inputs, tmp_path, capsys):
    baseline, _ = workflow_inputs
    output = tmp_path / "review"
    assert main(["discovery", "review", str(baseline), "--output", str(output)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["completed_judgments"] == 0
    result = tmp_path / "judgments.json"
    assert (
        main(
            [
                "discovery",
                "import-judgments",
                str(output / "queue.json"),
                str(output / "judgments.template.json"),
                "--output",
                str(result),
            ]
        )
        == 0
    )
    assert read_json(result)["counts"]["human_judgments"] == 0
    assert read_json(result)["counts"]["pending_pairs"] > 0


def test_diagnostics_compression_is_deterministic_and_references_round_trip(tmp_path):
    first, second = tmp_path / "first.json.gz", tmp_path / "second.json.gz"
    value = {"references": [{"id": "source", "title": "Provider", "url": "https://example.org"}]}
    write_json(first, value)
    write_json(second, value)
    assert first.read_bytes() == second.read_bytes()
    assert read_json(first) == value
    duplicate = tmp_path / "duplicate.json.gz"
    duplicate.write_bytes(gzip.compress(b'{"same": 1, "same": 2}'))
    with pytest.raises(ValueError, match="Duplicate JSON"):
        read_json(duplicate)
