"""Glean-flavored seniority fetch: parse a Glean `app=people` record into
the source-agnostic SeniorityFields contract defined in seniority.py.

The Glean MCP server isn't directly callable from Python — Claude makes
the MCP call inside the skill flow, then passes the fields through
record_seniority.py. This module owns the *field mapping* from Glean's
JSON shape; the actual network call lives in Claude's tool use. That
keeps the source-of-truth fetching swappable: an LDAP/SCIM/manual
implementation just needs its own `parse_<source>_record` function
returning the same SeniorityFields shape.

The Glean record shape, as of 2026-05, looks roughly like:

    {
      "name": "Matthew Seal",
      "title": "Principal Engineer II",
      "department": "Cloud Infrastructure & Platform",
      "teams": ["CIP - Experiences", "Cloud Infrastructure & Platform"],
      "email": "mseal@confluent.io",
      "manager": {
        "title": "Director II, Engineering",
        "directReportsCount": 16,
        "totalReportsCount": 87,
        ...
      },
      ...
    }
"""

from __future__ import annotations

from seniority import SeniorityFields


def parse_glean_record(record: dict, email: str | None = None) -> SeniorityFields:
    """Map a Glean person record → SeniorityFields.

    `email` is optional; if not in the record, pass it explicitly. The
    returned SeniorityFields always carries source='glean' so the
    written entry in seniority.yaml is attributable.
    """
    e = (record.get("email") or email or "").strip().lower()
    teams_raw = record.get("teams") or []
    teams = tuple(t for t in teams_raw if isinstance(t, str))
    manager = record.get("manager") or {}
    return SeniorityFields(
        email=e,
        title=record.get("title"),
        department=record.get("department"),
        teams=teams,
        manager_title=manager.get("title"),
        total_reports_count=manager.get("totalReportsCount"),
        source="glean",
    )
