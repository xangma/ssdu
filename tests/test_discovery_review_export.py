"""The review pack preserves bindings, pending status and untrusted source text."""

import copy
import csv
import io
import json
import re
import shutil
import subprocess

import pytest

from ssdu.discovery_review_export import ReviewExportError, export_review_pack


@pytest.fixture
def queue():
    reference = {"id": "paper", "title": "Published study", "url": "https://example.org/paper"}
    return {
        "binding": {"benchmark_id": "frozen-benchmark", "manifest_sha256": "a" * 64},
        "queue_sha256": "b" * 64,
        "questions": [
            {
                "id": "question",
                "text": "Which release fits this study?",
                "constraints": {"place": "Portsmouth"},
                "references": [reference],
                "selection_reasons": ["agent selection"],
                "scenario_basis": "Published study",
            }
        ],
        "rows": [
            {
                "id": "review-pair:1",
                "question_id": "question",
                "dataset_id": "nomis:NM_1_1",
                "dataset": {
                    "title": "Dataset title",
                    "description": "Official metadata",
                    "coverage": {"countries": ["England"]},
                },
                "question_references": [reference],
                "dataset_references": [
                    {"id": "provider", "title": "Provider metadata", "url": "https://example.org"}
                ],
                "coverage_check": {"status": "unknown", "checks": []},
                "review_status": "pending",
                "human_judgment": None,
            }
        ],
        "pooling": [{"method": "hybrid", "prior_grade": 3}],
    }


def embedded_data(html):
    payload = re.search(
        r'<script id="review-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert payload
    return json.loads(payload.group(1))


def test_pack_is_blind_pending_idempotent_and_bound(queue, tmp_path):
    untouched = copy.deepcopy(queue)
    output = tmp_path / "review"
    receipt = export_review_pack(queue, output)
    assert queue == untouched
    assert receipt["completed_judgments"] == 0
    assert receipt["row_count"] == receipt["question_count"] == 1
    assert receipt == export_review_pack(queue, output)
    assert json.loads((output / "queue.json").read_text()) == queue
    template = json.loads((output / "judgments.template.json").read_text())
    assert template == {
        "queue_sha256": queue["queue_sha256"],
        "benchmark_id": "frozen-benchmark",
        "judgments": [],
    }
    html = (output / "review.html").read_text()
    data = embedded_data(html)
    assert data["rows"][0]["row_id"] == "review-pair:1"
    assert len(data["rows"][0]["references"]) == 2
    assert data["rows"][0]["constraint_checks"]["status"] == "unknown"
    assert "pooling" not in data
    assert "selection_reasons" not in data["questions"][0]
    assert "prior_grade" not in html
    csv_rows = list(csv.DictReader(io.StringIO((output / "rows.csv").read_text())))
    assert csv_rows[0]["relevance"] == csv_rows[0]["reviewer_name"] == ""
    assert len(json.loads(csv_rows[0]["references_json"])) == 2


def test_untrusted_text_cannot_close_script_or_become_spreadsheet_formula(queue, tmp_path):
    malicious = '=1+1 </script><script>fetch("https://evil.example")</script>\u2028&'
    queue["questions"][0]["text"] = malicious
    queue["rows"][0]["dataset"]["title"] = '<img src="https://evil.example" onerror="alert(1)">'
    queue["rows"][0]["dataset_references"][0]["url"] = "javascript:alert(1)"
    export_review_pack(queue, tmp_path)
    html = (tmp_path / "review.html").read_text()
    data = embedded_data(html)
    assert data["questions"][0]["text"] == malicious
    assert "<img" not in html
    assert '</script><script>fetch("https://evil.example")' not in html
    assert "innerHTML" not in html
    assert "textContent" in html
    assert "['https:','http:'].includes(url.protocol)" in html
    assert "connect-src 'none'" in html
    rows = list(csv.DictReader(io.StringIO((tmp_path / "rows.csv").read_text())))
    assert rows[0]["question"] == "'" + malicious
    assert json.loads((tmp_path / "queue.json").read_text())["questions"][0]["text"] == malicious


def test_export_refuses_conflicts_and_symlinks(queue, tmp_path):
    output = tmp_path / "pack"
    export_review_pack(queue, output)
    changed = copy.deepcopy(queue)
    changed["queue_sha256"] = "c" * 64
    with pytest.raises(ReviewExportError, match="different review queue"):
        export_review_pack(changed, output)
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    (unrelated / "rows.csv").write_text("important existing content")
    with pytest.raises(ReviewExportError, match="not a review pack"):
        export_review_pack(queue, unrelated)
    linked = tmp_path / "linked"
    linked.symlink_to(output, target_is_directory=True)
    with pytest.raises(ReviewExportError, match="symlink"):
        export_review_pack(queue, linked)
    (output / "review.html").unlink()
    (output / "review.html").symlink_to(unrelated / "rows.csv")
    with pytest.raises(ReviewExportError, match="symlink"):
        export_review_pack(queue, output)
    assert (unrelated / "rows.csv").read_text() == "important existing content"


def test_html_javascript_parses(queue, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript syntax check")
    export_review_pack(queue, tmp_path / "pack")
    html = (tmp_path / "pack" / "review.html").read_text()
    script = re.search(r"<script>\n(.*?)\n</script>", html, re.DOTALL).group(1)
    source = tmp_path / "review.js"
    source.write_text(script)
    result = subprocess.run(
        [node, "--check", str(source)], check=False, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
