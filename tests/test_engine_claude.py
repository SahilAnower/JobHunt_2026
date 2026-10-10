"""
The claude engine must behave exactly as the three stages did before this branch existed.

These tests monkeypatch claudecall rather than calling the real CLI, so they prove the wiring
without consuming a session: one preflight then one batched call, batch-size grouping
preserved, global `n` numbering, and the two failure modes mapped to the two different lines
the stages print.

claudecall.py itself is byte-identical on this branch, which tests/test_claudecall.py already
covers.
"""

import subprocess

import pytest

import claudecall
import engine
import engine_claude
import score

PROFILE = {
    "candidate": {"name": "X", "short_name": "X", "email": "x@x", "phone": "0",
                  "base_city": "Hyderabad", "country": "India",
                  "links": {"linkedin": "l", "github": "g", "leetcode": "lc"}},
    "resume_summary": "Java and Spring Boot.",
    "roles": {"band": "SDE II", "lane_a": "backend", "lane_b": "agentic"},
    "comp": {"target_lpa": 45, "floor_lpa": 38, "min_fixed_lpa": 28, "currency": "INR",
             "note": "n", "dealbreakers": ["x"]},
    "situation": "urgent",
    "filters": {"include_titles": ["software engineer"], "exclude_titles": ["senior"],
                "lane_a_titles": ["software engineer"]},
    "geo": {"country_terms": ["india"], "home_terms": ["hyderabad"]},
    "targets": {"companies": [], "comp_risk": []},
    "runtime": {"claude_bin": "claude", "max_parallel_claude": 4},
}


def row(i):
    return {"key": f"x::{i}", "company": "Acme", "title": "Software Engineer II",
            "location": "Hyderabad, India", "url": "", "source": "greenhouse",
            "description": "Java and Spring Boot. 3+ years.", "lane": "A", "geo": "home",
            "score": 7, "score_reason": "r", "speed_note": "s", "human_path": None,
            "notes": None, "req_id": str(i), "posted_at": None, "source_name": None}


def reply(n):
    import json
    return json.dumps([{"n": i, "score": 7, "lane": "A", "reason": f"r{i}", "speed": "s"}
                       for i in range(1, n + 1)])


def test_the_claude_engine_still_batches_and_calls_run_once(monkeypatch):
    """
    The property that matters most and would be easiest to lose: ONE call into claudecall.run,
    which preflights once and then runs every prompt concurrently. A per-row engine interface
    would have made these serial and reinstated the 1086-second draft stage claudecall.py was
    written to kill.
    """
    seen = {}

    def fake_run(prompts, claude_bin, timeout, workers, announce=""):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["n_prompts"] = len(prompts)
        seen["workers"] = workers
        seen["bin"] = claude_bin
        seen["announce"] = announce
        return [(reply(12), None)] * len(prompts)

    monkeypatch.setattr(claudecall, "run", fake_run)
    rows = [row(i) for i in range(24)]
    engine_claude.ClaudeEngine().score_reqs(PROFILE, rows, batch_size=12, announce="hello")
    assert seen["calls"] == 1
    assert seen["n_prompts"] == 2
    # Read from the profile, not passed as a parameter: a value with no CLI override is the
    # engine's to look up.
    assert seen["workers"] == 4
    assert seen["bin"] == "claude"
    assert seen["announce"] == "hello"


def test_plan_groups_by_batch_size_for_the_claude_engine():
    rows = [row(i) for i in range(25)]
    got = engine_claude.ClaudeEngine().plan(rows, batch_size=12)
    assert [len(b) for b in got] == [12, 12, 1]


def test_n_is_global_not_per_batch(monkeypatch):
    # This is what lets the stage do rows[r["n"] - 1], which is score.py's original
    # batch[r["n"] - 1] with one substitution. Per-batch numbering would write every score
    # against one of the first twelve rows.
    monkeypatch.setattr(claudecall, "run",
                        lambda p, *a, **k: [(reply(12), None), (reply(12), None)])
    rows = [row(i) for i in range(24)]
    got = engine_claude.ClaudeEngine().score_reqs(PROFILE, rows, batch_size=12)
    assert [r["n"] for r in got] == list(range(1, 25))


def test_dry_run_preview_is_todays_prompt_unchanged():
    rows = [row(0)]
    assert (engine_claude.ClaudeEngine().dry_run_preview(PROFILE, rows)
            == score.build_prompt(PROFILE, rows))


def test_the_master_resume_is_accepted_and_ignored_on_the_claude_path():
    # score.build_prompt does not use the resume, so honouring it would change the claude
    # path's prompt. Deliberately unused.
    rows = [row(0)]
    with_master = engine_claude.ClaudeEngine().dry_run_preview(PROFILE, rows, master="RESUME")
    assert "RESUME" not in with_master
    assert with_master == score.build_prompt(PROFILE, rows)


def test_unusable_maps_to_the_before_any_work_message(monkeypatch):
    def boom(*a, **k):
        raise claudecall.Unusable("session expired")

    monkeypatch.setattr(claudecall, "run", boom)
    with pytest.raises(engine.EngineUnusable) as e:
        engine_claude.ClaudeEngine().score_reqs(PROFILE, [row(0)], batch_size=12)
    # The stage prints "claude is not usable: ..." for this one.
    assert e.value.mid_stage is False
    assert "session expired" in str(e.value)


def test_auth_expired_maps_to_the_mid_stage_message(monkeypatch):
    def boom(*a, **k):
        raise claudecall.AuthExpired("run mwinit")

    monkeypatch.setattr(claudecall, "run", boom)
    with pytest.raises(engine.EngineUnusable) as e:
        engine_claude.ClaudeEngine().score_reqs(PROFILE, [row(0)], batch_size=12)
    # The stage prints "aborted: ..." for this one, because rows may already be written.
    assert e.value.mid_stage is True


def test_an_unparseable_reply_is_its_own_error_type(monkeypatch):
    monkeypatch.setattr(claudecall, "run", lambda p, *a, **k: [("not json at all", None)])
    seen = []
    got = engine_claude.ClaudeEngine().score_reqs(
        PROFILE, [row(0)], batch_size=12,
        on_batch=lambda i, n, nreqs, results, error: seen.append(error))
    assert got == []
    # score.py prints "unparseable reply: ..." for this and "failed: ..." for everything else.
    assert isinstance(seen[0], engine.UnparseableReply)


def test_a_timeout_reaches_the_callback_as_itself(monkeypatch):
    err = subprocess.TimeoutExpired("claude", 240)
    monkeypatch.setattr(claudecall, "run", lambda p, *a, **k: [(None, err)])
    seen = []
    engine_claude.ClaudeEngine().score_reqs(
        PROFILE, [row(0)], batch_size=12,
        on_batch=lambda i, n, nreqs, results, error: seen.append(error))
    # The stage branches on this type to print the timeout line with its own CALL_TIMEOUT.
    assert seen[0] is err


def test_a_failed_batch_does_not_cost_the_successful_ones(monkeypatch):
    monkeypatch.setattr(claudecall, "run",
                        lambda p, *a, **k: [(reply(12), None), (None, RuntimeError("boom"))])
    rows = [row(i) for i in range(24)]
    got = engine_claude.ClaudeEngine().score_reqs(PROFILE, rows, batch_size=12)
    assert len(got) == 12
    assert [r["n"] for r in got] == list(range(1, 13))


def test_draft_and_tailor_results_are_positionally_aligned(monkeypatch):
    monkeypatch.setattr(
        claudecall, "run",
        lambda p, *a, **k: [("MESSAGE:\nhi\n\nRISKS:\n- r", None),
                            (None, RuntimeError("boom"))])
    rows = [row(0), row(1)]
    got = engine_claude.ClaudeEngine().draft_referrals(PROFILE, rows, "referral")
    assert got[0] == ("hi", "- r")
    assert isinstance(got[1], Exception)


def test_claudecall_is_unchanged_on_this_branch():
    # C7: the one module this branch promised not to touch. Its public surface is what the
    # claude engine is built on, so a rename here would be a silent behaviour change.
    for name in ("call", "preflight", "gather", "run", "AuthExpired", "Unusable",
                 "DEFAULT_TIMEOUT", "DEFAULT_WORKERS"):
        assert hasattr(claudecall, name), name
