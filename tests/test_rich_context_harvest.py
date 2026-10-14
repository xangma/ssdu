"""The full import reuses verified raw closure without changing its identity."""

import json

import pytest
from test_rich_context_full import graph

from ssdu.http import HttpClient
from ssdu.literature_ingest import RICH_CONTEXT_REVISION
from ssdu.literature_sources import parse_rich_context_full
from ssdu.literature_store import LiteratureStore
from ssdu.models import RetrievalError
from ssdu.rich_context_harvest import import_rich_context


def retained(root, *, revision=RICH_CONTEXT_REVISION, licence=b"CC0-1.0", include_licence=True):
    raw = json.dumps(graph()).encode()
    document = parse_rich_context_full(
        raw, source_url="https://example.org/corpus.jsonld", revision=revision
    )
    resources = {"rich-context.jsonld": raw}
    if include_licence:
        resources["rich-context-LICENSE.txt"] = licence
    return LiteratureStore(root).publish(document, resources)


@pytest.fixture(autouse=True)
def deny_http(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Retained full import attempted provider access")

    monkeypatch.setattr(HttpClient, "request", denied)


def test_verified_source_import_round_trip_reparse_has_identical_identity(tmp_path):
    source = tmp_path / "source"
    pilot = retained(source)
    target = tmp_path / "target"
    imported = import_rich_context(target, from_snapshot=pilot, source_store=source, offline=True)
    assert imported["provider_attempts"] == 0
    store = LiteratureStore(target)
    sid = imported["snapshot_id"]
    document = store.read(sid)
    assert document["harvest_type"] == "rich_context_full"
    assert imported["counts"]["papers"] == 2
    archive = tmp_path / "full.zip"
    store.export(sid, archive)
    relocated = tmp_path / "relocated"
    assert LiteratureStore(relocated).import_bundle(archive) == sid
    replayed = import_rich_context(relocated, from_snapshot=sid, offline=True)
    assert replayed["snapshot_id"] == sid
    assert LiteratureStore(relocated).read(sid) == document


@pytest.mark.parametrize(
    "options,exception,message",
    [
        ({"revision": "other-commit"}, ValueError, "pinned Rich Context"),
        ({"include_licence": False}, ValueError, "raw closure"),
        ({"licence": b"unknown terms"}, RetrievalError, "licence"),
    ],
)
def test_retained_source_requires_pinned_identity_and_licence(
    tmp_path, options, exception, message
):
    sid = retained(tmp_path / "source", **options)
    with pytest.raises(exception, match=message):
        import_rich_context(
            tmp_path / "target", from_snapshot=sid, source_store=tmp_path / "source"
        )
    assert not LiteratureStore(tmp_path / "target").list()


@pytest.mark.parametrize(
    "options",
    [
        {"offline": True, "refresh": True},
        {"from_snapshot": "unused", "refresh": True},
        {"source_store": "unused"},
    ],
)
def test_incompatible_policy_fails_before_acquisition(tmp_path, options):
    with pytest.raises(ValueError):
        import_rich_context(tmp_path, **options)
