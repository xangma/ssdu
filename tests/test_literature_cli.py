"""The local captured pilot works through the CLI without acquisition or models."""

import json
import socket
from pathlib import Path

import pytest

from ssdu.cli import main
from ssdu.http import HttpClient

PILOT = Path(__file__).resolve().parents[1] / "examples" / "literature"


def test_pilot_cli_transfer_and_review_only_derivation(tmp_path, monkeypatch, capsys):
    if not all((PILOT / name).is_file() for name in ("pilot.json", "pilot.ssdu-literature.zip")):
        pytest.skip("Source-only publication excludes captured literature pilot data")

    def blocked(*args, **kwargs):
        raise AssertionError("Offline literature operation attempted networking")

    monkeypatch.setattr(HttpClient, "request", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    prefix = ["--store", str(tmp_path / "fresh"), "literature"]

    def command(*args):
        assert main([*prefix, *map(str, args)]) == 0
        return json.loads(capsys.readouterr().out)

    pilot = json.loads((PILOT / "pilot.json").read_bytes())
    imported = command("import", PILOT / "pilot.ssdu-literature.zip")
    snapshot_id = imported["snapshot_id"]
    assert snapshot_id == pilot["snapshot_id"] and imported["counts"] == pilot["counts"]
    document = command("show", snapshot_id)
    assert len(document["papers"]) == 30
    assert sum(bool(p["abstract"]) for p in document["papers"]) == 13
    search = command("search", snapshot_id, "--query", "Portsmouth")
    assert search["rows"] and all(row["references"] for row in search["rows"])
    baseline = tmp_path / "baseline"
    command("derive", snapshot_id, "--output", baseline)
    pending = tmp_path / "pending"
    command("derive", snapshot_id, "--output", pending, "--review", baseline / "review.json")
    assert (baseline / "questions.json").read_bytes() == (pending / "questions.json").read_bytes()
    review = json.loads((baseline / "review.json").read_bytes())
    row = review["records"][0]
    row.update(status="human_reviewed", reviewer="Test fixture reviewer", suitability_judged=True)
    row["expected_suitable"] = [row["relationships"][0]["dataset_id"]]
    review_path = tmp_path / "test-review.json"
    review_path.write_text(json.dumps(review))
    reviewed_dir = tmp_path / "reviewed"
    result = command("derive", snapshot_id, "--output", reviewed_dir, "--review", review_path)
    assert result["human_review_pending"] == 29
    questions = json.loads((reviewed_dir / "questions.json").read_bytes())["questions"]
    checked = next(q for q in questions if q["id"] == row["question_id"])
    assert checked["suitability_judged"] and checked["generation"]["human_validated"]
    assert all(q["references"] for q in questions)
