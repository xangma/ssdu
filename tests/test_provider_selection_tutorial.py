"""The downloadable provider lesson works with only its declared companion."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LESSON = ROOT / "examples" / "notebooks" / "provider_selection.ipynb"
COMPANION = LESSON.with_name("provider_selection_fixture.py")


@pytest.fixture
def lesson_folder(tmp_path):
    for source in (LESSON, COMPANION):
        shutil.copyfile(source, tmp_path / source.name)
    return tmp_path


def run_lesson(folder, source):
    environment = os.environ.copy()
    # No repository root or examples directory: match a downloaded notebook kit.
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=folder,
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
        check=True,
    )


TEACH = """
import json
import socket
from pathlib import Path
from unittest.mock import patch
import httpx


def forbidden(*args, **kwargs):
    raise AssertionError('The teaching notebook attempted a network connection')


notebook = json.loads(Path('provider_selection.ipynb').read_text())
namespace = {}
with patch.object(socket.socket, 'connect', forbidden), \\
     patch.object(socket, 'create_connection', forbidden), \\
     patch.object(httpx.HTTPTransport, 'handle_request', forbidden):
    for cell in notebook['cells']:
        if cell['cell_type'] != 'code':
            continue
        if cell['id'] == 'provider-selection-13':
            assert namespace['plan']['provider_requests_for_values'] == 0
            assert not any('.data.csv' in r.url for r in namespace['client'].resources)
        exec(''.join(cell['source']), namespace)
    frame = namespace['observations']
    assert frame['geography'].tolist() == ['001']
    assert frame['period'].tolist() == ['2024']
    assert frame['value'].tolist() == [12.0]
    assert frame['unit'].tolist() == ['dwellings']
    assert namespace['cached'].snapshot_id == namespace['dataset'].snapshot_id
    assert namespace['request'].max_rows == 10
    print('PORTABLE_TEACHING_SUCCESS')
"""


def test_downloaded_provider_lesson_and_fresh_session_reopen(lesson_folder):
    result = run_lesson(lesson_folder, TEACH)
    assert "PORTABLE_TEACHING_SUCCESS" in result.stdout
    # A second interpreter has no earlier notebook variables or injected client.
    reopen = """
import json
import socket
from pathlib import Path
from unittest.mock import patch
import httpx


def forbidden(*args, **kwargs):
    raise AssertionError('Reopening attempted provider access')


notebook = json.loads(Path('provider_selection.ipynb').read_text())
cell = next(c for c in notebook['cells'] if c['id'] == 'provider-selection-17')
with patch.object(socket.socket, 'connect', forbidden), \\
     patch.object(httpx.HTTPTransport, 'handle_request', forbidden):
    exec(''.join(cell['source']))
    assert reopened.to_pandas()['value'].tolist() == [12.0]
    assert reopened.snapshot_id == saved_id
print('FRESH_SESSION_SUCCESS')
"""
    result = run_lesson(lesson_folder, reopen)
    assert "FRESH_SESSION_SUCCESS" in result.stdout
    # Running all cells again also succeeds against the retained cache.
    assert "PORTABLE_TEACHING_SUCCESS" in run_lesson(lesson_folder, TEACH).stdout


def test_provider_notebook_source_has_no_saved_outputs():
    notebook = json.loads(LESSON.read_text())
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_teaching_transport_rejects_unrecognised_requests(lesson_folder):
    result = run_lesson(
        lesson_folder,
        """
import httpx
from provider_selection_fixture import respond
for url in ['https://example.org/', 'https://www.nomisweb.co.uk/api/v01/unknown']:
    try:
        respond(httpx.Request('GET', url))
    except AssertionError:
        pass
    else:
        raise AssertionError('Unexpected fixture route accepted')
print('CLOSED_TRANSPORT_SUCCESS')
""",
    )
    assert "CLOSED_TRANSPORT_SUCCESS" in result.stdout
