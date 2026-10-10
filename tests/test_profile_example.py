"""
config/profile.example.yaml is the documented surface for every knob, and it is what an owner
copies to create a profile that then OVERRIDES the code defaults. So a stale example is worse
than no example: it teaches the wrong value and then ships it.
"""

from pathlib import Path

import yaml

import engine
import engine_heuristic as eh

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "profile.example.yaml"


def example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_the_example_scoring_block_equals_the_code_defaults():
    # The test that stops the first tuning commit from changing one and not the other.
    assert example()["scoring"] == eh.DEFAULTS


def test_the_example_documents_the_engine_key():
    rt = example()["runtime"]
    assert rt["engine"] in engine.VALID
    body = EXAMPLE.read_text(encoding="utf-8")
    for name in engine.VALID:
        assert name in body, name


def test_the_example_profile_still_builds_a_scoring_context():
    # Catches a vocabulary edit in the example that would fail the startup validators, which an
    # owner would otherwise discover on their first run after copying it.
    ctx = eh.build_context(example(), None)
    assert ctx.skills


def test_every_vocabulary_term_in_the_example_is_lowercase_and_a_string():
    sc = example()["scoring"]
    for tier, terms in sc["skills"].items():
        for term in terms:
            assert isinstance(term, str), (tier, term)
            assert term == term.lower(), (tier, term)
    for key in ("in_band_titles", "above_band_titles", "below_band_titles"):
        for term in sc[key]:
            assert isinstance(term, str), (key, term)
            assert term == term.lower(), (key, term)


def test_the_example_numerals_load_as_strings_not_ints():
    # An unquoted integer would reach the whole-word matcher, which takes a str.
    assert "2" in example()["scoring"]["in_band_titles"]
    assert 2 not in example()["scoring"]["in_band_titles"]


def test_every_penalty_key_in_the_example_is_negative():
    w = example()["scoring"]["weights"]
    for key in eh.PENALTY_KEYS:
        assert w[key] < 0, key
    for key in eh.SATURATE_KEYS:
        assert w[key] > 0, key
