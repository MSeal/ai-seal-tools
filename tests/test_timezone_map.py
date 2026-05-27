"""Tests for timezone_map.infer_timezone — maps Glean location
strings to IANA timezones used by the find-meeting-time skill."""

from __future__ import annotations

import pytest

import timezone_map as tzm


# ---------------------------------------------------------------------------
# Explicit-match table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location,expected", [
    # Confluent-employee locations we've actually encountered in this repo.
    ("GB Remote United Kingdom", "Europe/London"),
    ("GB Office London", "Europe/London"),
    ("US Remote California", "America/Los_Angeles"),
    ("US Remote Colorado", "America/Denver"),
    ("US Remote Texas", "America/Chicago"),
    ("US Remote New York", "America/New_York"),
    ("US Remote Virginia", "America/New_York"),
    ("US Remote Florida", "America/New_York"),
    ("US Remote Washington", "America/Los_Angeles"),  # state default
    ("US Remote Nevada", "America/Los_Angeles"),
    ("US Remote Georgia", "America/New_York"),
    ("US Office Mountain View CA", "America/Los_Angeles"),
    ("CA Remote Ontario", "America/Toronto"),
    ("CA Office Toronto", "America/Toronto"),
    ("DE Remote Germany", "Europe/Berlin"),
    ("IN Remote India", "Asia/Kolkata"),
])
def test_known_locations_resolve(location, expected):
    assert tzm.infer_timezone(location) == expected


# ---------------------------------------------------------------------------
# Country-prefix fallback
# ---------------------------------------------------------------------------


def test_unknown_gb_suffix_falls_back_to_country_prefix():
    """An unfamiliar 'GB Office Edinburgh' should still resolve via the
    GB country-code prefix rather than returning None."""
    assert tzm.infer_timezone("GB Office Edinburgh") == "Europe/London"


def test_unknown_jp_suffix_falls_back():
    assert tzm.infer_timezone("JP Office Osaka") == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# Ambiguity guard
# ---------------------------------------------------------------------------


def test_us_without_state_is_ambiguous():
    """US spans 4+ timezones; an un-stated 'US Remote Foo' should not
    silently pick one."""
    assert tzm.infer_timezone("US Remote Foo") is None


def test_ca_without_province_is_ambiguous():
    """Canada spans 6 zones; same reasoning as US."""
    assert tzm.infer_timezone("CA Remote Nowhere") is None


# ---------------------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_blank_returns_none(bad):
    assert tzm.infer_timezone(bad) is None


def test_unrecognized_country_returns_none():
    assert tzm.infer_timezone("XX Remote Atlantis") is None


def test_leading_trailing_whitespace_tolerated():
    assert tzm.infer_timezone("  GB Remote United Kingdom  ") == "Europe/London"
