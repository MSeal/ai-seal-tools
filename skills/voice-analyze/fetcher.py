"""Content fetchers for source-index entries.

Two layers:
1. `fetch_local(path)` — reads a local file directly.
2. `fetch_from_cache(source_id)` — reads
   `~/.cache/ai-seal-tools/voice/content/<source_id>.md` if present.

The cache pattern exists because Confluence and Drive content fetching needs
authenticated APIs that aren't always accessible from a fresh Python process
(MCP tools live in the Claude Code session; Drive needs OAuth-scope extension).
The cache lets an orchestrating script — or me, the Claude session — pre-fill
content for entries, then `propose-batch` runs over whatever's available
without each script needing to know how to fetch each location type.

Top-level `fetch_content(entry)` dispatches:
- location=local → fetch_local using entry["path"]
- everything else → fetch_from_cache using entry["id"]
- raise FetchUnavailable if neither path works
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTENT_CACHE_DIR = Path.home() / ".cache" / "ai-seal-tools" / "voice" / "content"


class FetchUnavailable(Exception):
    """Raised when no content is available for an entry — typically means
    the cache hasn't been pre-filled. The caller should skip the entry and
    surface the message in batch output."""


@dataclass
class FetchedContent:
    text: str
    source: str  # "local" | "cache"
    notes: str = ""


def fetch_local(path: str) -> FetchedContent:
    p = Path(path).expanduser()
    if not p.is_absolute():
        # Resolve relative paths against the repo root (3 levels up from this file)
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = (repo_root / path).resolve()
    if not p.is_file():
        raise FetchUnavailable(f"local file not found: {p}")
    return FetchedContent(text=p.read_text(), source="local")


def fetch_from_cache(source_id: str, cache_dir: Path = CONTENT_CACHE_DIR) -> FetchedContent:
    path = cache_dir / f"{source_id}.md"
    if not path.is_file():
        raise FetchUnavailable(
            f"no cached content for {source_id} (looked for {path})"
        )
    return FetchedContent(text=path.read_text(), source="cache", notes=str(path))


def fetch_content(entry: dict[str, Any], cache_dir: Path = CONTENT_CACHE_DIR) -> FetchedContent:
    """Prefer a local path if one exists (even on confluence-merged entries
    where the dedup overwrote `location` but kept `path`). Otherwise look
    in the cache."""
    path = entry.get("path")
    if path:
        try:
            return fetch_local(path)
        except FetchUnavailable:
            # Fall through to cache lookup — the local file may have been moved
            pass
    if entry["location"] == "local":
        raise FetchUnavailable(f"entry {entry['id']} is location=local but has no usable path")
    return fetch_from_cache(entry["id"], cache_dir=cache_dir)


def write_cache(source_id: str, text: str, cache_dir: Path = CONTENT_CACHE_DIR) -> Path:
    """Write content to the per-source cache. Caller pre-fetched the content
    through MCP / Drive API / etc."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{source_id}.md"
    path.write_text(text)
    return path
