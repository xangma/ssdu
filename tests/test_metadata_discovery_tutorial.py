"""The downloaded teaching pair works outside the checkout with no live network."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_downloaded_metadata_lesson_and_transferred_cache(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    for name in ("metadata_discovery.ipynb", "metadata_discovery_fixture.py"):
        shutil.copy2(repository / "examples/notebooks" / name, tmp_path / name)
    runner = r"""
import json
import shutil
import socket
from pathlib import Path
from unittest.mock import patch

from ssdu.http import HttpClient
from ssdu.metadata_index import MetadataIndex

def denied(*args, **kwargs):
    raise AssertionError("The downloaded lesson must not contact any network")

with patch.object(socket, "getaddrinfo", denied), \
     patch.object(socket, "create_connection", denied), \
     patch.object(socket.socket, "connect", denied):
    # A second run tests restarting a notebook without manually deleting its cache.
    for _ in range(2):
        namespace = {}
        notebook = json.loads(Path("metadata_discovery.ipynb").read_text())
        for position, cell in enumerate(notebook["cells"]):
            if cell["cell_type"] == "code":
                assert cell["execution_count"] is None and cell["outputs"] == []
                exec(compile("".join(cell["source"]), f"cell-{position}", "exec"), namespace)
        assert namespace["matches"]["total"] == 2
        assert namespace["scottish"]["field_status"]["licence"]["status"] == "not_provided"

    # The original store and fixture helper are no longer available to reopening.
    shutil.rmtree("data/metadata-discovery/catalogue")
    Path("metadata_discovery_fixture.py").unlink()
    with patch.object(HttpClient, "request", denied):
        index = MetadataIndex("data/metadata-discovery/reopened-catalogue")
        items = index.search("community")["items"]
        assert {row["provider"] for row in items} == {"mhclg", "scotland"}
        assert all(row["metadata_profile"]["completeness"]["catalogue_complete"] for row in items)
        assert all(row["freshness"]["last_checked_at"] for row in items)
print("PORTABLE_METADATA_LESSON_PASSED")
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(repository / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PORTABLE_METADATA_LESSON_PASSED" in result.stdout
