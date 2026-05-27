"""Map Glean people-profile `location` strings to canonical IANA timezones.

The Glean profile's `location` field is a stringly-typed mix of forms:
  "GB Remote United Kingdom", "US Remote California",
  "US Office Mountain View CA", "CA Office Toronto", ...

The find-meeting-time skill (and anything else that needs to know an
attendee's working-hours timezone) consults this module to translate.

Coverage is best-effort: explicit mappings for locations Confluent
employees commonly use, country-prefix fallback for the rest, and
None for genuinely-ambiguous inputs (US/CA without a state/province)
so the caller can prompt for override rather than silently
mis-place someone.

State-level US strings are mapped to the most-populous timezone in
that state. The handful of ambiguous cases (Washington = state vs DC,
Tennessee = ET vs CT, Kentucky/Indiana = mixed) default to the
more-likely-tech-employee guess and rely on the caller's --timezone
override when wrong.
"""

from __future__ import annotations

LOCATION_TO_TZ: dict[str, str] = {
    # GB
    "GB Remote United Kingdom": "Europe/London",
    "GB Office London": "Europe/London",
    # US Pacific
    "US Remote California": "America/Los_Angeles",
    "US Office Mountain View CA": "America/Los_Angeles",
    "US Office San Francisco CA": "America/Los_Angeles",
    "US Remote Washington": "America/Los_Angeles",  # state, tech default; DC users override
    "US Office Seattle WA": "America/Los_Angeles",
    "US Remote Oregon": "America/Los_Angeles",
    "US Remote Nevada": "America/Los_Angeles",
    # US Mountain
    "US Remote Colorado": "America/Denver",
    "US Office Denver CO": "America/Denver",
    "US Remote Utah": "America/Denver",
    "US Remote Montana": "America/Denver",
    "US Remote New Mexico": "America/Denver",
    "US Remote Idaho": "America/Denver",
    "US Remote Wyoming": "America/Denver",
    "US Remote Arizona": "America/Phoenix",  # no DST
    # US Central
    "US Remote Texas": "America/Chicago",
    "US Remote Illinois": "America/Chicago",
    "US Remote Minnesota": "America/Chicago",
    "US Remote Wisconsin": "America/Chicago",
    "US Remote Missouri": "America/Chicago",
    "US Remote Iowa": "America/Chicago",
    "US Remote Oklahoma": "America/Chicago",
    "US Remote Arkansas": "America/Chicago",
    "US Remote Louisiana": "America/Chicago",
    "US Remote Alabama": "America/Chicago",
    "US Remote Mississippi": "America/Chicago",
    "US Remote Kansas": "America/Chicago",
    "US Remote Nebraska": "America/Chicago",
    "US Remote Tennessee": "America/Chicago",
    # US Eastern
    "US Remote New York": "America/New_York",
    "US Office New York NY": "America/New_York",
    "US Remote Virginia": "America/New_York",
    "US Remote Florida": "America/New_York",
    "US Remote Georgia": "America/New_York",
    "US Remote North Carolina": "America/New_York",
    "US Remote South Carolina": "America/New_York",
    "US Remote Massachusetts": "America/New_York",
    "US Remote Pennsylvania": "America/New_York",
    "US Remote Maryland": "America/New_York",
    "US Remote Connecticut": "America/New_York",
    "US Remote New Jersey": "America/New_York",
    "US Remote Maine": "America/New_York",
    "US Remote Vermont": "America/New_York",
    "US Remote New Hampshire": "America/New_York",
    "US Remote Rhode Island": "America/New_York",
    "US Remote Delaware": "America/New_York",
    "US Remote Michigan": "America/New_York",
    "US Remote Ohio": "America/New_York",
    "US Remote Indiana": "America/New_York",
    "US Remote Kentucky": "America/New_York",
    "US Remote West Virginia": "America/New_York",
    "US Remote DC": "America/New_York",
    # Hawaii / Alaska
    "US Remote Hawaii": "Pacific/Honolulu",
    "US Remote Alaska": "America/Anchorage",
    # CA (Canada)
    "CA Remote Ontario": "America/Toronto",
    "CA Office Toronto": "America/Toronto",
    "CA Remote Quebec": "America/Toronto",
    "CA Office Montreal": "America/Toronto",
    "CA Remote British Columbia": "America/Vancouver",
    "CA Office Vancouver": "America/Vancouver",
    "CA Remote Alberta": "America/Edmonton",
    "CA Remote Manitoba": "America/Winnipeg",
    "CA Remote Saskatchewan": "America/Regina",
    "CA Remote Nova Scotia": "America/Halifax",
    "CA Remote New Brunswick": "America/Halifax",
    # EU
    "DE Remote Germany": "Europe/Berlin",
    "DE Office Berlin": "Europe/Berlin",
    "DE Office Munich": "Europe/Berlin",
    "FR Remote France": "Europe/Paris",
    "FR Office Paris": "Europe/Paris",
    "IE Remote Ireland": "Europe/Dublin",
    "IE Office Dublin": "Europe/Dublin",
    "ES Remote Spain": "Europe/Madrid",
    "NL Remote Netherlands": "Europe/Amsterdam",
    "NL Office Amsterdam": "Europe/Amsterdam",
    "IT Remote Italy": "Europe/Rome",
    "SE Remote Sweden": "Europe/Stockholm",
    "CH Remote Switzerland": "Europe/Zurich",
    "BE Remote Belgium": "Europe/Brussels",
    "AT Remote Austria": "Europe/Vienna",
    "PL Remote Poland": "Europe/Warsaw",
    "PT Remote Portugal": "Europe/Lisbon",
    # APAC
    "IN Remote India": "Asia/Kolkata",
    "IN Office Bangalore": "Asia/Kolkata",
    "AU Remote Australia": "Australia/Sydney",
    "AU Office Sydney": "Australia/Sydney",
    "JP Remote Japan": "Asia/Tokyo",
    "JP Office Tokyo": "Asia/Tokyo",
    "SG Remote Singapore": "Asia/Singapore",
    "SG Office Singapore": "Asia/Singapore",
    "CN Remote China": "Asia/Shanghai",
    "HK Remote Hong Kong": "Asia/Hong_Kong",
    "TW Remote Taiwan": "Asia/Taipei",
    "KR Remote South Korea": "Asia/Seoul",
    "MY Remote Malaysia": "Asia/Kuala_Lumpur",
    "TH Remote Thailand": "Asia/Bangkok",
    "ID Remote Indonesia": "Asia/Jakarta",
    "PH Remote Philippines": "Asia/Manila",
    "VN Remote Vietnam": "Asia/Ho_Chi_Minh",
    # Latin America
    "BR Remote Brazil": "America/Sao_Paulo",
    "MX Remote Mexico": "America/Mexico_City",
    "AR Remote Argentina": "America/Argentina/Buenos_Aires",
    "CL Remote Chile": "America/Santiago",
    "CO Remote Colombia": "America/Bogota",
    # Middle East / Africa
    "IL Remote Israel": "Asia/Jerusalem",
    "AE Remote UAE": "Asia/Dubai",
    "ZA Remote South Africa": "Africa/Johannesburg",
    "EG Remote Egypt": "Africa/Cairo",
    "NG Remote Nigeria": "Africa/Lagos",
    "KE Remote Kenya": "Africa/Nairobi",
}

COUNTRY_PREFIX_TZ: dict[str, str] = {
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "ES": "Europe/Madrid",
    "IT": "Europe/Rome",
    "NL": "Europe/Amsterdam",
    "SE": "Europe/Stockholm",
    "CH": "Europe/Zurich",
    "BE": "Europe/Brussels",
    "AT": "Europe/Vienna",
    "PL": "Europe/Warsaw",
    "PT": "Europe/Lisbon",
    "IN": "Asia/Kolkata",
    "AU": "Australia/Sydney",
    "JP": "Asia/Tokyo",
    "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong",
    "TW": "Asia/Taipei",
    "KR": "Asia/Seoul",
    "BR": "America/Sao_Paulo",
    "MX": "America/Mexico_City",
    "AR": "America/Argentina/Buenos_Aires",
    "CL": "America/Santiago",
    "CO": "America/Bogota",
    "IL": "Asia/Jerusalem",
    "AE": "Asia/Dubai",
    "ZA": "Africa/Johannesburg",
}

# US and CA are intentionally absent from COUNTRY_PREFIX_TZ — both span
# multiple zones and a state/province-less string ("US Remote Foo") is
# more likely a typo than a confident continent-level mapping.
AMBIGUOUS_PREFIXES = {"US", "CA"}


def infer_timezone(location: str | None) -> str | None:
    """Map a Glean `location` string to an IANA timezone, or None when
    no confident mapping exists.

    Lookup order: exact match in LOCATION_TO_TZ → country-code prefix in
    COUNTRY_PREFIX_TZ (excluding US/CA) → None."""
    if not location:
        return None
    key = location.strip()
    if not key:
        return None
    if key in LOCATION_TO_TZ:
        return LOCATION_TO_TZ[key]
    parts = key.split(maxsplit=1)
    if parts:
        cc = parts[0].upper()
        if cc not in AMBIGUOUS_PREFIXES and cc in COUNTRY_PREFIX_TZ:
            return COUNTRY_PREFIX_TZ[cc]
    return None
