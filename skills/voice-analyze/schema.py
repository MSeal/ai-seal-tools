"""Schema versioning + validation for the voice-analyze skill family.

The voice profile (profile.yaml) and the source-hash log (sources_seen.yaml)
each declare a top-level `schema_version`. On load, callers should pass the
parsed YAML through `migrate_forward()` to bring older versions up to the
current schema, then `validate()` to enforce structure.

Adding a field to either schema requires:
  1. Bumping LATEST_PROFILE_SCHEMA / LATEST_SOURCES_SEEN_SCHEMA.
  2. Writing schemas/<kind>_v<N>.json with the new structure.
  3. Writing migrations/<kind>_v<N-1>_to_v<N>.py with a `migrate(data) -> dict`
     function. The new file is auto-discovered by filename.
  4. Adding a regression test that the migration runs cleanly on a v<N-1>
     fixture and produces a valid v<N> document.

The schemas use additionalProperties:false at every level — any unknown
field fails validation loudly, which is the proactive guard against
"someone added a field but forgot the schema bump."
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema

SKILL_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = SKILL_DIR / "schemas"
MIGRATIONS_DIR = SKILL_DIR / "migrations"

LATEST_PROFILE_SCHEMA = 1
LATEST_SOURCES_SEEN_SCHEMA = 1
LATEST_PROPOSAL_SCHEMA = 1
LATEST_SOURCE_INDEX_SCHEMA = 1
LATEST_SCRUB_FEEDBACK_SCHEMA = 1

VALID_KINDS = ("profile", "sources_seen", "proposal", "source_index", "scrub_feedback")


def latest(kind: str) -> int:
    if kind == "profile":
        return LATEST_PROFILE_SCHEMA
    if kind == "sources_seen":
        return LATEST_SOURCES_SEEN_SCHEMA
    if kind == "proposal":
        return LATEST_PROPOSAL_SCHEMA
    if kind == "source_index":
        return LATEST_SOURCE_INDEX_SCHEMA
    if kind == "scrub_feedback":
        return LATEST_SCRUB_FEEDBACK_SCHEMA
    raise ValueError(f"unknown schema kind: {kind!r} (expected one of {VALID_KINDS})")


def load_schema(kind: str, version: int) -> dict[str, Any]:
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown schema kind: {kind!r}")
    path = SCHEMAS_DIR / f"{kind}_v{version}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"No schema file at {path}. Versions are sequential — every "
            f"intermediate version must have a schema and a migration."
        )
    return json.loads(path.read_text())


def validate(kind: str, data: dict[str, Any], version: int | None = None) -> None:
    """Validate `data` against the schema for `kind` at `version`.

    Defaults `version` to data['schema_version']. Raises jsonschema.ValidationError
    on structural problems and ValueError for missing schema_version.
    """
    declared = data.get("schema_version")
    if declared is None:
        raise ValueError(f"{kind} data missing required field 'schema_version'")
    use_version = version if version is not None else declared
    schema = load_schema(kind, use_version)
    jsonschema.validate(data, schema)


def migrate_forward(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Migrate data forward to the latest schema, one version at a time.

    Returns the (possibly new) data dict. Raises if data is at a future
    schema version this code doesn't know how to read, or if a needed
    migration file is missing.
    """
    declared = data.get("schema_version")
    if declared is None:
        raise ValueError(f"{kind} data missing required field 'schema_version'")
    target = latest(kind)
    if declared > target:
        raise ValueError(
            f"{kind} schema_version {declared} is newer than this code "
            f"supports (latest known: {target}). Update the voice-analyze "
            f"skill before continuing."
        )
    while data["schema_version"] < target:
        cur = data["schema_version"]
        nxt = cur + 1
        mod = _load_migration(kind, cur, nxt)
        data = mod.migrate(data)
        if data.get("schema_version") != nxt:
            raise RuntimeError(
                f"Migration {kind} v{cur}->v{nxt} did not bump schema_version "
                f"to {nxt} (got {data.get('schema_version')!r})."
            )
    return data


def _load_migration(kind: str, frm: int, to: int) -> ModuleType:
    fname = f"{kind}_v{frm}_to_v{to}.py"
    path = MIGRATIONS_DIR / fname
    if not path.is_file():
        raise RuntimeError(
            f"Missing migration {fname} — cannot move {kind} from v{frm} to v{to}. "
            f"Migrations live in {MIGRATIONS_DIR} and must form an unbroken chain."
        )
    spec = importlib.util.spec_from_file_location(f"_voice_migration_{kind}_{frm}_{to}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load migration spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "migrate"):
        raise RuntimeError(f"Migration {path} missing required `migrate(data)` function")
    return mod
