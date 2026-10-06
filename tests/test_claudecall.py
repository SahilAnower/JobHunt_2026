"""
The shared claude entry point.

score, draft and tailor all reach `claude` through claudecall.run, so the abort policy is
decided here for all three. That policy is not cosmetic: it exists because an expired Midway
session once cost 25 minutes of wall clock across a 420s score stage and a 1086s draft stage
that between them produced one draft. The rules it encodes are

  - a dead session aborts the stage before any real work is spent (preflight), and
  - a single failed item does not sink the ones that succeeded (per-item errors in the result).

Every test here stubs claudecall.call, so no subprocess runs and `claude` is never invoked.
"""

from __future__ import annotations

import pytest

import claudecall

PREFLIGHT_PROMPT = "Reply with the single word: ok"


def stub_call(monkeypatch, handler):
    """Replace claudecall.call and record every prompt it is handed, in order."""
    seen: list[str] = []

    def fake(prompt, claude_bin="claude", timeout=claudecall.DEFAULT_TIMEOUT):
        seen.append(prompt)
        return handler(prompt, len(seen))

    monkeypatch.setattr(claudecall, "call", fake)
    return seen


def test_results_are_returned_in_prompt_order(monkeypatch):
    # Order is load-bearing: callers zip() the results back against their batches, so a
    # reordering would write each score onto the wrong req.
    seen = stub_call(monkeypatch, lambda p, n: f"reply:{p}")
    assert claudecall.run(["a", "b", "c"]) == [
        ("reply:a", None), ("reply:b", None), ("reply:c", None)]
    assert seen == [PREFLIGHT_PROMPT, "a", "b", "c"]


def test_preflight_failure_aborts_before_any_work(monkeypatch):
    # The whole point of preflight. One probe, then stop — not one timeout per item.
    def handler(prompt, n):
        raise FileNotFoundError("claude")

    seen = stub_call(monkeypatch, handler)
    with pytest.raises(claudecall.Unusable) as e:
        claudecall.run(["a", "b", "c"])
    assert seen == [PREFLIGHT_PROMPT]
    assert "not on PATH" in str(e.value)


def test_an_expired_session_raises_unusable_with_the_remedy(monkeypatch):
    def handler(prompt, n):
        raise claudecall.AuthExpired(claudecall.REMEDY)

    stub_call(monkeypatch, handler)
    with pytest.raises(claudecall.Unusable) as e:
        claudecall.run(["a"])
    # Exact, not a substring: the callers print f"claude is not usable: {e}" where they used to
    # print the preflight message directly, so str(Unusable) has to BE that message for the
    # output to stay what it was.
    assert str(e.value) == claudecall.REMEDY


def test_auth_expiring_mid_flight_propagates(monkeypatch):
    # preflight passed, so the session died during the stage. Still abandon it: no later item
    # can succeed and waiting for them only burns the timeout again.
    def handler(prompt, n):
        if prompt == PREFLIGHT_PROMPT:
            return "ok"
        raise claudecall.AuthExpired(claudecall.REMEDY)

    stub_call(monkeypatch, handler)
    with pytest.raises(claudecall.AuthExpired):
        claudecall.run(["a", "b"])


def test_a_per_item_failure_does_not_sink_the_others(monkeypatch):
    # The other half of the policy, and the reason run() does not just raise on any error.
    def handler(prompt, n):
        if prompt == "b":
            raise RuntimeError("claude exited 1")
        return f"reply:{prompt}"

    stub_call(monkeypatch, handler)
    got = claudecall.run(["a", "b", "c"])
    assert got[0] == ("reply:a", None)
    assert got[1][0] is None and isinstance(got[1][1], RuntimeError)
    assert got[2] == ("reply:c", None)


def test_an_empty_prompt_list_still_preflights(monkeypatch):
    seen = stub_call(monkeypatch, lambda p, n: "ok")
    assert claudecall.run([]) == []
    assert seen == [PREFLIGHT_PROMPT]


def test_announce_prints_only_after_preflight_passes(monkeypatch, capsys):
    """
    Why `announce` is a parameter rather than a print in the caller. On the failing path the
    stage must not say "calling claude for 6 draft(s)" and then "claude is not usable" — that
    claims work which never started.
    """
    stub_call(monkeypatch, lambda p, n: "ok")
    claudecall.run(["a"], announce="calling claude for 1 batch(es), 4 at a time")
    assert capsys.readouterr().out == "calling claude for 1 batch(es), 4 at a time\n"


def test_announce_is_not_printed_when_the_cli_is_unusable(monkeypatch, capsys):
    def handler(prompt, n):
        raise FileNotFoundError("claude")

    stub_call(monkeypatch, handler)
    with pytest.raises(claudecall.Unusable):
        claudecall.run(["a"], announce="calling claude for 1 batch(es), 4 at a time")
    assert capsys.readouterr().out == ""


# --- gather, called directly

def test_gather_clamps_workers_to_the_prompt_count(monkeypatch):
    # max(1, min(workers, len(prompts))). A profile setting max_parallel_claude higher than
    # the batch count must not become a ThreadPoolExecutor sized for prompts that do not exist.
    stub_call(monkeypatch, lambda p, n: f"reply:{p}")
    assert claudecall.gather(["a", "b"], workers=99) == [("reply:a", None), ("reply:b", None)]


def test_gather_on_an_empty_list_short_circuits(monkeypatch):
    seen = stub_call(monkeypatch, lambda p, n: "ok")
    assert claudecall.gather([]) == []
    assert seen == []


def test_preflight_reports_the_reply_on_success(monkeypatch):
    stub_call(monkeypatch, lambda p, n: "  ok\n")
    assert claudecall.preflight() == (True, "ok")


def test_preflight_describes_an_empty_reply_rather_than_claiming_success(monkeypatch):
    stub_call(monkeypatch, lambda p, n: "   ")
    assert claudecall.preflight() == (True, "(empty reply)")
