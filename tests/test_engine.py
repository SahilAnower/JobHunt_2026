"""
The engine selector, and the one guarantee this branch exists for: with engine=heuristic
nothing in the process can reach the `claude` binary.
"""

import subprocess
import sys

import pytest

import engine


# --------------------------------------------------------------------------- resolve

def test_absent_engine_key_means_heuristic():
    # The live config/profile.yaml has no engine: key and must keep working untouched.
    assert engine.resolve({}) == "heuristic"
    assert engine.resolve(None) == "heuristic"
    assert engine.resolve({"timezone": "Asia/Kolkata"}) == "heuristic"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_or_blank_engine_means_heuristic(value):
    # Unset, empty and whitespace-only all mean the same thing to someone editing YAML, so they
    # must behave the same. An earlier form defaulted on "" and raised on "   ".
    assert engine.resolve({"engine": value}) == "heuristic"


@pytest.mark.parametrize("value,want", [
    ("claude", "claude"), ("CLAUDE", "claude"), ("Claude ", "claude"),
    ("heuristic", "heuristic"), (" heuristic", "heuristic"),
])
def test_engine_name_is_case_and_space_insensitive(value, want):
    assert engine.resolve({"engine": value}) == want


def test_override_beats_the_profile():
    assert engine.resolve({"engine": "heuristic"}, "claude") == "claude"
    assert engine.resolve({"engine": "claude"}, "heuristic") == "heuristic"


def test_unknown_engine_in_the_profile_raises_with_the_valid_names():
    # argparse guards --engine with choices=, so this path is specifically a YAML typo, which
    # has no argparse to catch it.
    with pytest.raises(engine.BadEngine) as e:
        engine.resolve({"engine": "gpt4"})
    assert "gpt4" in str(e.value)
    for name in engine.VALID:
        assert name in str(e.value)


# --------------------------------------------------------------------------- get

def test_ollama_is_accepted_as_a_value_but_refuses_to_run():
    # Deliberately not a stub that degrades to heuristic: an owner who wrote engine: ollama
    # wants a local model, and quietly giving them something else is worse than saying no.
    assert engine.resolve({"engine": "ollama"}) == "ollama"
    with pytest.raises(engine.BadEngine) as e:
        engine.get("ollama")
    assert "not implemented" in str(e.value)


def test_get_heuristic_returns_the_heuristic_engine():
    eng = engine.get("heuristic")
    assert eng.name == "heuristic"
    assert isinstance(eng, engine.EngineProtocol)


def test_heuristic_preflight_needs_nothing():
    ok, msg = engine.get("heuristic").preflight({})
    assert ok is True
    assert msg


def test_engine_unusable_carries_the_two_stage_messages_apart():
    # The three stages print "claude is not usable: ..." before any work and "aborted: ..."
    # once rows may already have been written. One type, two situations, told apart here.
    assert engine.EngineUnusable("x").mid_stage is False
    assert engine.EngineUnusable("x", mid_stage=True).mid_stage is True


# --------------------------------------------------------------------------- zero claude

def test_importing_the_heuristic_engine_never_imports_claudecall():
    """
    The mechanism behind the zero-subprocess guarantee, asserted rather than argued.

    Run in a clean subprocess because this test session has almost certainly imported
    claudecall already through tests/test_claudecall.py, so checking sys.modules in-process
    would pass or fail on test ordering.
    """
    code = (
        "import sys, engine\n"
        "eng = engine.get('heuristic')\n"
        "assert eng.name == 'heuristic'\n"
        "leaked = [m for m in ('claudecall', 'engine_claude') if m in sys.modules]\n"
        "assert not leaked, leaked\n"
        "print('clean')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "clean" in proc.stdout


def test_the_score_stage_makes_no_subprocess_call_on_the_heuristic_path(tmp_path, monkeypatch):
    """
    A real boundary assertion, not a code-reading argument: subprocess.run is replaced with
    something that fails the test if anything calls it, and then a full heuristic score runs.
    """
    import engine_heuristic

    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or pytest.fail(f"subprocess: {a}"))
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: pytest.fail(f"subprocess.Popen: {a}"))

    profile = _fixture_profile()
    rows = [_row(title="Software Engineer II", company="Rubrik",
                 description="Java, Spring Boot and AWS. 3+ years of experience.")]
    eng = engine.get("heuristic")
    got = eng.score_reqs(profile, rows, master="", batch_size=12)
    assert len(got) == 1
    assert 1 <= got[0]["score"] <= 9
    assert calls == []


def _fixture_profile() -> dict:
    return {
        "candidate": {"short_name": "Sahil"},
        "filters": {"include_titles": ["software engineer"],
                    "exclude_titles": ["senior", "staff"],
                    "lane_a_titles": ["software engineer"]},
        "geo": {"country_terms": ["india"], "home_terms": ["hyderabad"],
                "relocation_terms": ["bangalore"], "remote_terms": ["remote"]},
        "targets": {"companies": ["Rubrik"], "comp_risk": []},
    }


def _row(**kw) -> dict:
    base = {"key": "x::1", "company": "X", "title": "Software Engineer II", "location": "",
            "url": "", "source": "greenhouse", "description": "", "lane": "A", "geo": None,
            "score": None, "score_reason": None, "speed_note": None, "human_path": None}
    base.update(kw)
    return base
