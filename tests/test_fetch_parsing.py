"""
The location mergers, and the closure probe's status mapping.

Both location helpers exist for the same reason: the field a board calls "location" is often
not a place, and the real city is somewhere else in the payload. If either merger regresses to
reading one field, India reqs go invisible without any error — which is exactly how Cloudflare's
India reqs were missed.

No network. url_is_live's HTTP layer is replaced with a fake that records the methods it was
asked for, so nothing opens a socket.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

import fetch


# --- _avature_location

def test_avature_merges_every_location_before_the_role_id_marker():
    # India is not always first: 98 of EA's 336 reqs are multi-location, and keeping only the
    # leading segment loses them.
    sub = ("Stockholm, Sweden \u2022 Hyderabad, India \u2022 Role ID 216197 \u2022 "
           "Regular Employee \u2022 CT - Frostbite")
    assert fetch._avature_location(sub) == "Stockholm, Sweden ; Hyderabad, India"


def test_avature_handles_entity_encoded_bullets():
    sub = ("Stockholm, Sweden &bull; Hyderabad, India &bull; Role ID 216197 &bull; "
           "Regular Employee")
    assert fetch._avature_location(sub) == "Stockholm, Sweden ; Hyderabad, India"


def test_avature_drops_the_metadata_tail():
    # "CT - Frostbite" is a studio name. Feeding it to the geo gate is noise.
    sub = "Hyderabad, India \u2022 Role ID 1 \u2022 Regular Employee \u2022 CT - Frostbite"
    assert fetch._avature_location(sub) == "Hyderabad, India"


def test_avature_without_the_marker_falls_back_to_the_first_segment():
    # The documented fallback, not a bug: all 336 subtitles carry the marker, and the first
    # segment is the safe half of the guess if one ever does not.
    assert fetch._avature_location("Stockholm, Sweden \u2022 Hyderabad, India") == (
        "Stockholm, Sweden")


def test_avature_empty_subtitle():
    assert fetch._avature_location("") == ""


# --- _greenhouse_location

def test_greenhouse_merges_all_three_sources_and_dedupes():
    """
    The Cloudflare incident. `location.name` holds a work arrangement ("Hybrid") and the real
    city is in offices[] and a "Job Posting Location" metadata field. The arrangement is kept
    AND the city appended, which is what lets the geo gate see Bengaluru.
    """
    j = {
        "location": {"name": "Hybrid"},
        "offices": [{"name": "Bengaluru"}, {"name": "Hybrid"}],
        "metadata": [
            {"name": "Job Posting Location", "value": ["Bengaluru", "Remote India"]},
            {"name": "Department", "value": "Eng"},
        ],
    }
    assert fetch._greenhouse_location(j) == "Hybrid ; Bengaluru ; Remote India"


def test_greenhouse_empty_payload():
    assert fetch._greenhouse_location({}) == ""


def test_greenhouse_explicit_nulls():
    # Boards send null rather than omitting the key, so `or {}` / `or []` has to absorb it.
    assert fetch._greenhouse_location(
        {"location": None, "offices": None, "metadata": None}) == ""


def test_greenhouse_metadata_name_matches_on_substring_case_insensitively():
    assert fetch._greenhouse_location(
        {"metadata": [{"name": "location", "value": "Pune"}]}) == "Pune"


def test_greenhouse_ignores_non_location_metadata():
    assert fetch._greenhouse_location(
        {"metadata": [{"name": "Department", "value": "Eng"}]}) == ""


# --- url_is_live

class _FakeResponse:
    """Just enough of an http.client.HTTPResponse for the `with urlopen(...) as r: r.status`."""

    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(sequence):
    """
    Answer each call with the next entry in `sequence`, repeating the last one forever. A code
    >= 400 is raised as an HTTPError, because that is how urllib really delivers it; an
    Exception instance is raised as-is. Records the HTTP method of every call so a test can
    prove the GET fallback did or did not fire.
    """
    calls: list[str] = []

    def fake(req, timeout=None, context=None):
        calls.append(req.method)
        item = sequence[min(len(calls) - 1, len(sequence) - 1)]
        if isinstance(item, Exception):
            raise item
        if item >= 400:
            raise urllib.error.HTTPError(req.full_url, item, "fake", {}, None)
        return _FakeResponse(item)

    return fake, calls


@pytest.mark.parametrize("code, expected", [
    (200, True),    # still being served
    (302, True),    # a redirect code counts as live
    (404, False),   # the one real "closed" signal
    (410, False),   # ...and its sibling
    (403, None),    # blocked a script. Says nothing about the req
    (429, None),    # rate limited. Also says nothing
    (500, None),    # their problem, not an answer
])
def test_url_is_live_status_mapping(monkeypatch, code, expected):
    fake, calls = _fake_urlopen([code])
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake)
    assert fetch.url_is_live("https://example.test/job/1", "ua") == expected
    # A conclusive answer must not cost a second request.
    assert calls == ["HEAD"]


def test_head_405_falls_back_to_get(monkeypatch):
    # Plenty of career sites answer 405 to HEAD. Without the fallback every one of them reads
    # as "no answer" and closure falls back to the miss counter alone.
    fake, calls = _fake_urlopen([405, 200])
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake)
    assert fetch.url_is_live("https://example.test/job/1", "ua") is True
    assert calls == ["HEAD", "GET"]


def test_head_405_then_get_404_is_closed(monkeypatch):
    fake, calls = _fake_urlopen([405, 404])
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake)
    assert fetch.url_is_live("https://example.test/job/1", "ua") is False
    assert calls == ["HEAD", "GET"]


def test_no_answer_at_all_is_none(monkeypatch):
    # DNS, TLS or timeout. None is the important return here: it hands the decision back to
    # the consecutive-miss counter instead of writing off a req over a network blip.
    fake, calls = _fake_urlopen([OSError("unreachable")])
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake)
    assert fetch.url_is_live("https://example.test/job/1", "ua") is None
    assert calls == ["HEAD", "GET"]
