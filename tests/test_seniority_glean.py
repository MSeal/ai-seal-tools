"""Tests for seniority_glean.parse_glean_record — verifies the Glean
people-record shape maps cleanly to the source-agnostic SeniorityFields.
Any future LDAP/SCIM/manual implementation should ship analogous tests
against its own parse_X_record."""

from __future__ import annotations

import seniority
import seniority_glean


def test_parse_glean_record_full_shape():
    """The realistic full shape we observed from app=people during scoping."""
    record = {
        "name": "Example User",
        "email": "you@example.com",
        "title": "Principal Engineer II",
        "department": "Cloud Infrastructure & Platform",
        "teams": ["CIP - Experiences", "Cloud Infrastructure & Platform", "Announcements Global"],
        "location": "US Remote California",
        "manager": {
            "name": "Some Director",
            "email": "director@example.com",
            "title": "Director II, Engineering",
            "directReportsCount": 16,
            "totalReportsCount": 87,
        },
    }
    fields = seniority_glean.parse_glean_record(record)
    assert isinstance(fields, seniority.SeniorityFields)
    assert fields.email == "you@example.com"
    assert fields.title == "Principal Engineer II"
    assert fields.department == "Cloud Infrastructure & Platform"
    assert fields.teams == (
        "CIP - Experiences",
        "Cloud Infrastructure & Platform",
        "Announcements Global",
    )
    assert fields.manager_title == "Director II, Engineering"
    assert fields.total_reports_count == 87
    assert fields.source == "glean"


def test_parse_glean_record_lowercases_email():
    """Glean sometimes returns mixed-case emails; we standardize to lower."""
    record = {"email": "Alice@Example.com", "title": "VP, Engineering"}
    fields = seniority_glean.parse_glean_record(record)
    assert fields.email == "alice@example.com"


def test_parse_glean_record_accepts_email_override():
    """If the record itself doesn't include email, caller can pass it."""
    record = {"title": "Director, Platform"}
    fields = seniority_glean.parse_glean_record(record, email="director@example.com")
    assert fields.email == "director@example.com"
    assert fields.title == "Director, Platform"


def test_parse_glean_record_missing_optional_fields():
    """Many Glean records lack manager/teams/department. Parsing should not
    crash; missing fields come through as None / empty tuple."""
    record = {"email": "alice@x", "title": "Engineer"}
    fields = seniority_glean.parse_glean_record(record)
    assert fields.teams == ()
    assert fields.manager_title is None
    assert fields.total_reports_count is None
    assert fields.department is None


def test_parse_glean_record_drives_correct_tier_inference():
    """End-to-end through the contract: Glean shape → SeniorityFields →
    infer_tier produces the expected tier."""
    record = {
        "email": "exec@x",
        "title": "Chief Technology Officer",
        "manager": {"totalReportsCount": 500},
    }
    fields = seniority_glean.parse_glean_record(record)
    assert seniority.infer_tier(fields) == 5
