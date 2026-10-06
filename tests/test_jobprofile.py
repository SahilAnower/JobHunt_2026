"""
The shared profile loader.

Never reads the real config/profile.yaml: it is gitignored and personal, so a test touching it
would fail on a fresh clone and would couple these assertions to one person's data. load() is
exercised against a fixture written into tmp_path.
"""

from __future__ import annotations

import pytest

import jobprofile


# --- user_agent

def test_a_wrapped_user_agent_is_collapsed_to_one_line():
    # profile.example.yaml wraps the UA across two YAML lines, so it arrives with a newline
    # and leading spaces in the middle. Sent raw that is a malformed header.
    wrapped = {"runtime": {"user_agent": "Mozilla/5.0 (Mac)\n  Chrome/124.0 Safari/537.36"}}
    assert jobprofile.user_agent(wrapped) == "Mozilla/5.0 (Mac) Chrome/124.0 Safari/537.36"


@pytest.mark.parametrize("profile", [
    {"runtime": {}},                    # runtime block with no UA in it
    {},                                 # no runtime block at all
    {"runtime": None},                  # `runtime:` present and empty, which parses to None
    {"runtime": {"user_agent": ""}},    # key present but blank
    {"runtime": {"user_agent": None}},  # key present and null
])
def test_a_missing_user_agent_falls_back(profile):
    # This tolerance is the behaviour fetch.py already had. discover.py did not, and raised
    # KeyError on the same profile; it now shares this.
    assert jobprofile.user_agent(profile) == jobprofile.DEFAULT_UA
    assert jobprofile.DEFAULT_UA == "jobhunt/1.0"


# --- runtime

@pytest.mark.parametrize("profile, expected", [
    ({"runtime": {"score_threshold": 7}}, {"score_threshold": 7}),
    ({}, {}),
    ({"runtime": None}, {}),
])
def test_runtime_always_returns_a_dict(profile, expected):
    # The `or {}` is what stops `.get` being called on None at startup, which is the shape a
    # profile takes the moment someone comments out everything under `runtime:`.
    assert jobprofile.runtime(profile) == expected


# --- load

def test_load_reads_the_path_it_is_given(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("runtime:\n  score_threshold: 9\n  user_agent: test/1.0\n")
    got = jobprofile.load(p)
    assert got == {"runtime": {"score_threshold": 9, "user_agent": "test/1.0"}}
    assert jobprofile.user_agent(got) == "test/1.0"


def test_the_default_path_points_at_the_config_directory():
    # Asserted on the path, not by reading the file, so this holds on a fresh clone where
    # config/profile.yaml does not exist yet.
    assert jobprofile.PROFILE.name == "profile.yaml"
    assert jobprofile.PROFILE.parent.name == "config"
