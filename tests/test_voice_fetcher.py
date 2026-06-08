"""Tests for the content fetcher.

The fetcher has two responsibilities: read local files directly, and read
pre-cached content for remote entries. We also test the cache write path
since orchestration scripts use it.
"""
from __future__ import annotations

import pytest

from fetcher import (
    FetchUnavailable,
    fetch_content,
    fetch_from_cache,
    fetch_local,
    write_cache,
)


def test_fetch_local_reads_file(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("hello world")
    fc = fetch_local(str(p))
    assert fc.text == "hello world"
    assert fc.source == "local"


def test_fetch_local_missing_raises(tmp_path):
    with pytest.raises(FetchUnavailable, match="not found"):
        fetch_local(str(tmp_path / "missing.md"))


def test_fetch_local_resolves_relative_path():
    """Repo-relative paths should resolve against the repo root."""
    # README.md exists at repo root? Use pyproject.toml instead — it definitely exists
    fc = fetch_local("pyproject.toml")
    assert "ai-seal-tools" in fc.text


def test_fetch_from_cache_reads_file(tmp_path):
    write_cache("my_id_abc12345", "cached body", cache_dir=tmp_path)
    fc = fetch_from_cache("my_id_abc12345", cache_dir=tmp_path)
    assert fc.text == "cached body"
    assert fc.source == "cache"


def test_fetch_from_cache_missing_raises(tmp_path):
    with pytest.raises(FetchUnavailable, match="no cached content"):
        fetch_from_cache("missing_id", cache_dir=tmp_path)


def test_fetch_content_dispatches_local(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("local body")
    entry = {"id": "local_x", "location": "local", "path": str(p)}
    fc = fetch_content(entry)
    assert fc.text == "local body"


def test_fetch_content_dispatches_cache(tmp_path):
    write_cache("confluence_xyz", "cached confluence body", cache_dir=tmp_path)
    entry = {"id": "confluence_xyz", "location": "confluence", "url": "https://x/1"}
    fc = fetch_content(entry, cache_dir=tmp_path)
    assert fc.text == "cached confluence body"


def test_fetch_content_local_missing_path():
    """If location=local but no path AND no cached content, fall through to
    a cache-miss FetchUnavailable error (since the index might still have
    the source_id cached even with location=local)."""
    entry = {"id": "local_xx_nonexistent_zzz", "location": "local"}
    with pytest.raises(FetchUnavailable):
        fetch_content(entry)
