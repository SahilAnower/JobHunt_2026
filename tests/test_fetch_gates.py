"""
The two gates in fetch.py, which between them decide what you ever see.

These are the highest-value tests in the repo because the gates are where a quiet mistake is
most expensive: a wrong exclusion drops a req you would have wanted and nothing anywhere says
so. Every expected value below was read off the real function, not derived from the docstrings.

The filter dicts are small literals, never config/profile.yaml. That file is gitignored and
personal, so a test reading it would fail on a fresh clone and would couple these assertions to
one person's private data.
"""

from __future__ import annotations

import pytest

import fetch

# Modelled on config/profile.example.yaml. `member of technical staff` has to be in
# include_titles as well as exclude_overrides, or the override case below would pass for the
# unrelated reason "no include_titles match" and prove nothing.
FILTERS = {
    "exclude_titles": ["senior", "sr", "staff", "principal", "lead", "manager", "mgr", "smts"],
    "include_titles": ["software engineer", "member of technical staff", "backend"],
    "exclude_overrides": ["member of technical staff"],
}


# --- title_verdict

def test_in_band_title_is_kept():
    assert fetch.title_verdict("Software Engineer II", FILTERS) == (
        True, "matched: software engineer")


def test_exclusion_beats_inclusion():
    # The title matches include_titles too. The ceiling has to win, or a profile capped at
    # SDE II keeps every Staff req on the board.
    assert fetch.title_verdict("Staff Software Engineer", FILTERS) == (
        False, "excluded title term: staff")


def test_sr_does_not_fire_inside_sre():
    # `sr` IS in exclude_titles here. _term_re's trailing boundary is the only thing keeping
    # this req. Drop the boundary and every SRE posting disappears silently.
    assert fetch.title_verdict("SRE Software Engineer", FILTERS) == (
        True, "matched: software engineer")


def test_lead_does_not_fire_inside_leader():
    # Same property, other term: `lead` is in the list, "Leader" survives it. The profile
    # comment says to list "leader" separately if you want it, which only works if `lead`
    # really is whole-word.
    assert fetch.title_verdict("Leader Software Engineer", FILTERS) == (
        True, "matched: software engineer")
    # ...and the bare word is still caught, so the boundary has not disarmed the term.
    assert fetch.title_verdict("Lead Software Engineer", FILTERS) == (
        False, "excluded title term: lead")


@pytest.mark.parametrize("title", ["Sr. Software Engineer", "Sr Software Engineer"])
def test_trailing_period_is_optional(title):
    # One `sr` entry has to cover both spellings; boards use them interchangeably.
    assert fetch.title_verdict(title, FILTERS) == (False, "excluded title term: sr")


def test_abbreviated_bands_are_caught():
    assert fetch.title_verdict("SMTS Software Engineer", FILTERS) == (
        False, "excluded title term: smts")


def test_exclude_override_keeps_an_in_band_title_containing_staff():
    # REGRESSION. "Member of Technical Staff II" is an IC band roughly equal to SDE II but it
    # carries the word "staff". exclude_overrides blanks the phrase from the gated copy, while
    # the include check still runs against the unblanked title — which is why the reason comes
    # back naming the phrase.
    assert fetch.title_verdict("Member of Technical Staff II", FILTERS) == (
        True, "matched: member of technical staff")


def test_exclude_override_does_not_lift_the_ceiling():
    # The other half of that mechanism, and the reason it blanks rather than whitelists: with
    # the phrase removed, "Sr" is still sitting there and still wins.
    assert fetch.title_verdict("Sr Member of Technical Staff", FILTERS) == (
        False, "excluded title term: sr")


def test_mgr_abbreviation_regression():
    """
    REGRESSION, and the config gap is the point: the gate was correct both times, only the
    list changed.

    NetApp abbreviates manager as "mgr" in its URL slugs, so "mgr-software-engineer"
    de-slugifies to "Mgr Software Engineer". An exclude_titles list holding only "manager"
    let it straight through into the digest. See the trap note in fetch_radancy and the "mgr"
    entry in profile.example.yaml.
    """
    without_mgr = {**FILTERS, "exclude_titles": ["senior", "sr", "staff", "manager"]}
    assert fetch.title_verdict("Mgr Software Engineer", without_mgr) == (
        True, "matched: software engineer")
    assert fetch.title_verdict("Mgr Software Engineer", FILTERS) == (
        False, "excluded title term: mgr")


def test_no_include_list_keeps_everything_not_excluded():
    assert fetch.title_verdict("Anything At All", {"exclude_titles": ["senior"]}) == (
        True, "no include list")


def test_unmatched_title_is_dropped():
    assert fetch.title_verdict("Data Scientist", FILTERS) == (False, "no include_titles match")


# --- geo_verdict

GEO = {
    "country_terms": ["india", "bharat"],
    "home_terms": ["hyderabad", "telangana"],
    "relocation_terms": ["bengaluru", "pune"],
    "remote_terms": ["remote", "work from home"],
    "require_india_signal": True,
}
GEO_OPEN = {**GEO, "require_india_signal": False}


def test_empty_location_drops_when_the_india_signal_is_required():
    assert fetch.geo_verdict("", GEO) == (None, "no location")


def test_empty_location_is_kept_as_unclear_when_it_is_not():
    assert fetch.geo_verdict("", GEO_OPEN) == ("unclear", "empty")


def test_home_beats_country():
    # Precedence is the whole point of the function: a Hyderabad req needs no relocation, so
    # it must not come back as a generic India req.
    assert fetch.geo_verdict("Hyderabad, India", GEO) == ("home", "hyderabad")


def test_remote_plus_country():
    assert fetch.geo_verdict("Remote, India", GEO) == ("remote", "remote + india")


def test_whole_field_remote_is_surfaced_with_the_country_unstated():
    assert fetch.geo_verdict("Remote", GEO) == ("remote", "remote, country unstated")


def test_relocation_city():
    assert fetch.geo_verdict("Bengaluru", GEO) == ("relocate", "bengaluru")


def test_relocation_beats_country_when_both_are_present():
    assert fetch.geo_verdict("Pune, India", GEO) == ("relocate", "pune")


def test_country_only():
    assert fetch.geo_verdict("India", GEO) == ("india", "india")


def test_foreign_location_is_dropped():
    assert fetch.geo_verdict("Austin, TX", GEO) == (None, "no India signal")


def test_foreign_location_is_kept_when_the_signal_is_not_required():
    assert fetch.geo_verdict("Austin, TX", GEO_OPEN) == ("unclear", "kept, location unclear")


# --- Indiana, and whole-word geo matching generally.
#
# geo_verdict's hit() used a plain substring test, so "india" matched inside "Indianapolis" and
# "Indiana" and a US Midwest req read as an India one. "Remote - Indiana" was the worst case: it
# matched a remote term AND a country term, so it arrived looking like a remote-India role, the
# most attractive bucket there is.
#
# The same boundary check already existed in india_facet_values() for the Workday and Oracle
# location facets, which is what the README's "Indiana contains india" note describes. It was
# never wired into this gate, the one every req passes through. Masked because Workday and
# Oracle narrow to India server-side, and the boards without a server-side filter had not yet
# posted an in-band Midwest role.
#
# Fixed by routing every geo list through _term_re, the same whole-word helper the title gate
# uses. Replaying all 636 stored reqs through the old and new matcher gave identical verdicts,
# so the tracked applications were not re-filtered.

def test_indiana_is_not_india():
    assert fetch.geo_verdict("Indiana", GEO)[0] is None
    assert fetch.geo_verdict("Indianapolis, IN", GEO)[0] is None


def test_remote_indiana_is_not_remote_india():
    """The regression that mattered most: two false hits compounding into the best bucket."""
    bucket, _ = fetch.geo_verdict("Remote - Indiana", GEO)
    assert bucket != "remote" or "india" not in fetch.geo_verdict("Remote - Indiana", GEO)[1]


def test_real_india_locations_still_match():
    """The fix must not cost a single genuine India req."""
    assert fetch.geo_verdict("Hyderabad, India", GEO) == ("home", "hyderabad")
    assert fetch.geo_verdict("Bengaluru, India", GEO)[0] == "relocate"
    assert fetch.geo_verdict("IN - Bengaluru, India", GEO)[0] == "relocate"
    assert fetch.geo_verdict("Remote - India", GEO)[0] == "remote"
    assert fetch.geo_verdict("India", GEO) == ("india", "india")
    assert fetch.geo_verdict("Gurgaon/ Pune, India", GEO)[0] == "relocate"
    assert fetch.geo_verdict("Bengaluru- We Work", GEO)[0] == "relocate"


def test_ncr_does_not_fire_inside_another_word():
    """`ncr` is in relocation_terms; "Concrete" is a real town in Washington."""
    assert fetch.geo_verdict("Concrete, Washington", GEO)[0] is None
