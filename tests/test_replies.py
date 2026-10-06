"""
The three functions that parse a language model's reply.

Every one of them is handed text a model wrote, which means the input is never guaranteed to
match the format the prompt asked for. Several of the tests below assert failure modes rather
than successes. That is on purpose: the current fragility is documented so a future change is
a visible decision instead of an accident.

No subprocess, no `claude`. These are pure string functions.
"""

from __future__ import annotations

import pytest

import draft
import score
import tailor


# --- score.parse_scores

def test_plain_json_array():
    got = score.parse_scores(
        '[{"n":1,"score":7,"lane":"A","reason":"real overlap","speed":"6 weeks"}]', 1)
    assert got == [{"n": 1, "score": 7, "lane": "A", "reason": "real overlap",
                    "speed": "6 weeks"}]


def test_fenced_reply_and_lane_is_upper_cased():
    got = score.parse_scores('```json\n[{"n":1,"score":7,"lane":"a"}]\n```', 1)
    assert got[0]["lane"] == "A"


def test_prose_prefix_is_discarded():
    # "Here you go:" in front of the array is the single most common deviation.
    assert score.parse_scores('Here you go:\n[{"n":1,"score":7}]', 1)[0]["score"] == 7


@pytest.mark.parametrize("raw, expected", [
    ('[{"n":1,"score":15}]', 10),
    ('[{"n":1,"score":-3}]', 1),
])
def test_score_is_clamped_to_the_scale(raw, expected):
    assert score.parse_scores(raw, 1)[0]["score"] == expected


def test_out_of_range_indices_are_dropped():
    # `n` indexes back into the batch, so a value outside 1..n would be an IndexError or,
    # worse, would score the wrong req.
    assert score.parse_scores('[{"n":3,"score":7},{"n":0,"score":7}]', 2) == []


def test_missing_optional_fields_become_empty_or_none():
    got = score.parse_scores('[{"n":1,"score":7}]', 1)[0]
    assert got["lane"] is None
    assert got["reason"] == ""
    assert got["speed"] == ""


def test_no_array_at_all_raises():
    with pytest.raises(ValueError) as e:
        score.parse_scores("no array here", 1)
    assert str(e.value).startswith("no JSON array in reply")


def test_trailing_bracketed_prose_breaks_the_greedy_match():
    # CHARACTERISATION. re.search(r"\[.*\]", ..., re.S) is greedy, so it spans the first "[" to
    # the LAST "]" and json.loads then chokes on the extra data. The caller catches it per
    # batch and reports "unparseable reply", so the cost is one batch, not the run.
    with pytest.raises(ValueError):
        score.parse_scores('[{"n":1,"score":7}] and also [see above]', 1)


def test_a_malformed_item_costs_the_whole_batch():
    # CHARACTERISATION. `it["score"]` is unguarded, so one item missing `score` raises and the
    # other scores in the same reply are lost with it.
    with pytest.raises(KeyError):
        score.parse_scores('[{"n":1}]', 1)


def test_a_verbose_lane_is_truncated_to_one_character():
    # CHARACTERISATION, and an observation worth the owner's attention: a model replying
    # "Lane B" stores "L", which then shows up as a lane in the digest. Not fixed here —
    # `jobs.lane` is a stored value the digest groups on, so changing it is not a pure refactor.
    assert score.parse_scores('[{"n":1,"score":7,"lane":"Lane B"}]', 1)[0]["lane"] == "L"


# --- draft.split_sections

def test_draft_both_sections():
    raw = "MESSAGE:\nHi Priya, saw the req.\n\nRISKS:\n- may be filled\n- no JD"
    assert draft.split_sections(raw) == ("Hi Priya, saw the req.", "- may be filled\n- no JD")


def test_draft_message_only():
    assert draft.split_sections("MESSAGE:\nHi Priya.") == ("Hi Priya.", "")


def test_draft_unlabelled_reply_is_treated_as_the_whole_message():
    # Better than dropping it: the file still gets written and you can read it.
    assert draft.split_sections("Hi Priya, no labels here.") == (
        "Hi Priya, no labels here.", "")


def test_draft_preamble_is_discarded():
    raw = "Sure, here you go.\n\nMESSAGE:\nHi Priya.\n\nRISKS:\n- one"
    assert draft.split_sections(raw) == ("Hi Priya.", "- one")


def test_draft_risks_inside_the_body_does_not_split_early():
    # The label has to be at the start of a line for this to hold.
    raw = "MESSAGE:\nI know the RISKS: here.\n\nRISKS:\n- real"
    assert draft.split_sections(raw) == ("I know the RISKS: here.", "- real")


def test_draft_trailing_newline_is_trimmed():
    assert draft.split_sections("MESSAGE:\nHi.\n\nRISKS:\n- one\n") == ("Hi.", "- one")


# --- tailor.split_sections

FULL = ("TAILORED RESUME:\nName, 3 years\n\nWHAT CHANGED:\n- led with the CDK bullet\n\n"
        "KEYWORDS ADDED:\n- observability\n\nGAPS:\n- no Go")


def test_tailor_all_four_sections():
    got = tailor.split_sections(FULL)
    assert got == {
        "TAILORED RESUME": "Name, 3 years",
        "WHAT CHANGED": "- led with the CDK bullet",
        "KEYWORDS ADDED": "- observability",
        "GAPS": "- no Go",
    }


def test_tailor_always_returns_every_key():
    """
    The invariant write_tailored depends on: it indexes parts['GAPS'] and the other three
    unguarded, so a missing key would be a KeyError at file-write time, after the claude call
    has already been paid for.
    """
    assert set(tailor.split_sections("nothing here")) == set(tailor.SECTIONS)
    assert set(tailor.split_sections(FULL)) == set(tailor.SECTIONS)


def test_tailor_only_the_first_section_present():
    got = tailor.split_sections("TAILORED RESUME:\nonly this")
    assert got["TAILORED RESUME"] == "only this"
    assert got["WHAT CHANGED"] == got["KEYWORDS ADDED"] == got["GAPS"] == ""


def test_tailor_garbage_input_yields_all_empty():
    got = tailor.split_sections("nothing here")
    assert all(v == "" for v in got.values())
    # main() checks exactly this and reports "reply had no TAILORED RESUME section".
    assert got["TAILORED RESUME"] == ""


def test_tailor_closing_fence_lands_in_the_last_section():
    # CHARACTERISATION. The final section's pattern runs to \Z, so a wrapping code fence ends
    # up inside GAPS. Cosmetic — GAPS is prose a human reads — and not worth a regex change
    # that would touch the resume text itself.
    got = tailor.split_sections("```\n" + FULL + "\n```")
    assert got["TAILORED RESUME"] == "Name, 3 years"
    assert got["GAPS"] == "- no Go\n```"


def test_tailor_sections_out_of_order_overlap():
    # CHARACTERISATION. GAPS is matched last and greedily to \Z, so if a model emits it first
    # it swallows everything after it — including a later TAILORED RESUME label, which is
    # matched independently and so still comes out right.
    got = tailor.split_sections("GAPS:\n- g\n\nTAILORED RESUME:\nr")
    assert got["TAILORED RESUME"] == "r"
    assert "TAILORED RESUME:" in got["GAPS"]
