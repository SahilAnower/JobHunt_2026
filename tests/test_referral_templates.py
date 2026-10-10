"""
The template bank. draft.VOICE is a set of instructions to a model; for a template bank those
instructions have to become properties of the text, so these tests run over EVERY template in
the bank rather than over one sampled output.
"""

import re

import pytest

import engine_heuristic as eh
import referral_templates as rt
from tests.test_engine_heuristic import MASTER, PROFILE, STRONG_JD, row

BANNED_OPENERS = ("honestly", "to be honest", "i hope this finds you well",
                  "i wanted to reach out", "i am excited to")
PRONOUNS = re.compile(r"\b(he|him|his|she|her|hers)\b")

ALL_TEMPLATES = [(bucket, i, t)
                 for bucket, variants in sorted(rt.BANK.items())
                 for i, t in enumerate(variants)]

# The longest plausible slot values, so the word budget holds at the worst case rather than on
# a lucky example.
WORST = {
    "company": "Wissen Infotech Technology Services",
    "role": "Software Engineer II, Distributed Systems and Platform Infrastructure",
    "req": "JR0000000338173",
    "url": "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/x",
    "skills_2": "infrastructure as code and distributed systems",
    "skills_3": "infrastructure as code, distributed systems and object-oriented design",
    "geo_clause": rt.GEO_CLAUSE["unclear"],
    "short_name": "Sahil",
    "reason_for_leaving": ("growth, a team reorganisation and a shift of investment toward AI "
                           "narrowed the path"),
    "hook_clause": " The posting mentions building distributed backend services, which is the "
                   "part I'd want to work on.",
    "warm_clause": "Context on my side: A recruiter opened a LinkedIn message thread about "
                   "this req.",
    "warm_path": "A recruiter opened a LinkedIn message thread about this req.",
}


@pytest.fixture(scope="module")
def ctx():
    return eh.build_context(PROFILE, MASTER)


# --------------------------------------------------------------------------- the bank

def test_every_bucket_has_at_least_two_variants():
    # An invariant with a documented exception is an invariant nobody can check at a glance.
    thin = {b: len(v) for b, v in rt.BANK.items() if len(v) < 2}
    assert not thin, thin


def test_the_bank_covers_every_kind_warmth_lane_combination():
    want = {(k, w, l) for k in ("referral", "email")
            for w in ("warm", "cold") for l in ("A", "B")}
    assert set(rt.BANK) == want


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_no_template_contains_an_em_dash(bucket, i, template):
    assert "\u2014" not in template and "\u2013" not in template


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_no_template_opens_with_a_banned_phrase(bucket, i, template):
    body = template.lower()
    for phrase in BANNED_OPENERS:
        assert phrase not in body, phrase


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_no_template_contains_a_gendered_pronoun(bucket, i, template):
    assert not PRONOUNS.search(template.lower())


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_every_template_signs_off_with_the_short_name(bucket, i, template):
    last = [l for l in template.strip().splitlines() if l.strip()][-1]
    assert last.strip() == "$short_name"


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_every_template_uses_exactly_one_skills_slot(bucket, i, template):
    # One specific thing from the resume, the one that maps to this req. Not a career summary.
    n = template.count("$skills_2") + template.count("$skills_3")
    assert n == 1, n


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_every_placeholder_is_a_known_slot(bucket, i, template):
    # safe_substitute leaves an unknown placeholder as literal text rather than raising, so
    # this is the test-time check that catches a typo before it reaches a draft.
    found = set(re.findall(r"\$(\w+)", template))
    assert found <= set(rt.SLOTS), found - set(rt.SLOTS)


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_every_template_fits_its_word_budget_at_the_worst_case(bucket, i, template):
    """
    The budget holds at the worst case WITHOUT the optional hook clause, which is exactly the
    guarantee draft_one relies on: it renders with the hook, and if that overflows it drops the
    hook and re-renders. So the invariant that has to be true of every template is the one
    measured here. With the hook clause present at its longest, six of the eighteen do overflow
    by a handful of words, and the retry below is what catches that.
    """
    rendered = rt._render(template, {**WORST, "hook_clause": ""})
    n = len(rendered.split())
    assert n <= rt.WORD_BUDGET[bucket[0]], f"{bucket}[{i}] is {n} words"


def test_an_overflowing_draft_drops_the_hook_clause_rather_than_the_budget(ctx):
    long_jd = ("You will design and operate distributed systems in Java and Spring Boot on "
               "AWS. Requirements: 3+ years of experience.")
    r = row(company="Wissen Infotech Technology Services",
            title="Software Engineer II, Distributed Systems and Platform Infrastructure",
            description=long_jd)
    msg, _ = rt.draft_one(r, PROFILE, ctx, "referral", score_one=eh.score_one)
    assert len(msg.split()) <= rt.WORD_BUDGET["referral"]


@pytest.mark.parametrize("bucket,i,template", ALL_TEMPLATES)
def test_no_template_contains_a_bare_three_item_list(bucket, i, template):
    # "A, B, and C." reads as generated. _join never emits an Oxford comma, and no template
    # hardcodes the shape either.
    assert not re.search(r"\w+, \w+,? and \w+\.", rt._render(template, WORST))


# --------------------------------------------------------------------------- reason_for_leaving

def test_sanctioned_reason_takes_the_first_sentence_and_drops_the_dash():
    live = ("growth \u2014 a team reorganisation and a shift of investment toward AI narrowed "
            "the path. Never elaborate beyond this, and never volunteer the performance plan.")
    got = rt.sanctioned_reason(live)
    assert got == ("growth, a team reorganisation and a shift of investment toward AI narrowed "
                   "the path")
    # The tell that an instruction tail leaked.
    assert "never" not in got.lower()
    assert "performance" not in got.lower()
    assert "\u2014" not in got


def test_sanctioned_reason_handles_a_one_sentence_value():
    assert rt.sanctioned_reason("growth.") == "growth"
    assert rt.sanctioned_reason("") == ""


# --------------------------------------------------------------------------- variants

def test_variant_selection_is_deterministic_on_the_job_key():
    a = rt.pick_variant("mongodb::123", "referral", "warm", "A")
    b = rt.pick_variant("mongodb::123", "referral", "warm", "A")
    assert a == b


def test_variant_selection_is_stable_across_processes():
    # hashlib.sha1, not hash(): hash() on a str is salted per process by PYTHONHASHSEED, so
    # --redraft would otherwise produce a different message every run.
    import subprocess
    import sys
    from pathlib import Path
    code = ("import referral_templates as rt\n"
            "print(rt.pick_variant('mongodb::123', 'referral', 'warm', 'A')[0][:40])\n")
    outs = []
    for seed in ("0", "99"):
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                                "PYTHONPATH": str(Path(rt.__file__).parent)})
        assert p.returncode == 0, p.stderr
        outs.append(p.stdout)
    assert outs[0] == outs[1]


def test_an_empty_bucket_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setitem(rt.BANK, ("referral", "warm", "B"), [])
    template, note = rt.pick_variant("x::1", "referral", "warm", "B")
    assert template
    assert "fallback" in note


# --------------------------------------------------------------------------- rendering

def test_a_draft_carries_a_matched_skill_the_scorer_actually_matched(ctx):
    msg, risks = rt.draft_one(row(company="Rubrik", description=STRONG_JD), PROFILE, ctx,
                              "referral", score_one=eh.score_one)
    assert "java" in msg.lower() or "spring boot" in msg.lower()
    assert msg.strip().endswith(PROFILE["candidate"]["short_name"])


def test_a_rendered_draft_has_no_unresolved_placeholder(ctx):
    msg, risks = rt.draft_one(row(description=STRONG_JD), PROFILE, ctx, "referral",
                              score_one=eh.score_one)
    assert "$" not in msg


def test_a_rendered_draft_has_no_em_dash_or_gendered_pronoun(ctx):
    for kind in ("referral", "email"):
        msg, risks = rt.draft_one(row(description=STRONG_JD), PROFILE, ctx, kind,
                                  score_one=eh.score_one)
        both = f"{msg}\n{risks}".lower()
        assert "\u2014" not in both and "\u2013" not in both
        assert not PRONOUNS.search(both)


def test_a_warm_row_gets_a_warm_template_and_a_cold_row_does_not(ctx):
    warm, _ = rt.draft_one(row(description=STRONG_JD, human_path="We worked together at X."),
                           PROFILE, ctx, "referral", score_one=eh.score_one)
    cold, _ = rt.draft_one(row(description=STRONG_JD), PROFILE, ctx, "referral",
                           score_one=eh.score_one)
    assert "We worked together at X." in warm
    assert "Context on my side" not in cold


def test_the_hook_is_lifted_from_the_jd_and_never_generated(ctx):
    jd = "You will build distributed systems in Java. Requirements: 3+ years."
    hook = rt.jd_hook(jd, ctx, eh)
    assert hook
    assert hook.lower() in jd.lower()


def test_a_hook_naming_a_never_claim_term_is_rejected(ctx):
    # One of the three ways a lifted sentence could misrepresent: it must not quote the JD
    # asking for Go into a message that then reads as a claim.
    assert rt.jd_hook("You will build services in Go and Java.", ctx, eh) == ""


def test_a_hook_quoting_a_years_figure_is_rejected(ctx):
    assert rt.jd_hook("We want 8+ years of Java experience.", ctx, eh) == ""


def test_no_hook_drops_the_clause_rather_than_inventing_a_detail(ctx):
    msg, _ = rt.draft_one(row(description="Great culture. Apply now."), PROFILE, ctx,
                          "referral", score_one=eh.score_one)
    assert "The posting mentions" not in msg
    assert "  " not in msg


def test_the_risks_section_is_generated_from_the_scorer_not_templated(ctx):
    jd = STRONG_JD.replace("3+ years", "9+ years") + " Requirements: Go is required."
    _, risks = rt.draft_one(row(description=jd, geo="relocate"), PROFILE, ctx, "referral",
                            score_one=eh.score_one)
    assert "years" in risks
    assert "go" in risks.lower()
    assert "relocation" in risks


def test_nothing_to_flag_yields_an_empty_risks_section(ctx):
    # write_draft already renders "_none flagged_". With the Claude engine a risks section was
    # always generated and sometimes padded; here nothing to flag genuinely means nothing.
    _, risks = rt.draft_one(row(description="Java and Spring Boot. Requirements: 2+ years.",
                                geo="home"), PROFILE, ctx, "referral", score_one=eh.score_one)
    assert risks == "" or "lifted from the JD" in risks


def test_the_never_mention_backstop_refuses_to_write(ctx):
    # A leak is unrecoverable once sent, so the row is not written at all: no file, no outreach
    # row. Documented as matching literally, which is why this fixture uses a phrase a template
    # can actually emit.
    profile = {**PROFILE, "drafting": {"never_mention": ["would you be willing to refer me"]}}
    with pytest.raises(rt.Blocked):
        rt.draft_one(row(description=STRONG_JD, human_path="met at a meetup"), profile, ctx,
                     "referral", score_one=eh.score_one)


def test_no_slot_sources_the_situation_block_or_the_never_mention_list(ctx):
    # The real control on never_mention is slot provenance, not the post-render check. This is
    # the test that fails if a new slot appears without being classified.
    assert set(rt.SLOTS) == {
        "company", "role", "req", "url", "skills_2", "skills_3", "geo_clause", "short_name",
        "reason_for_leaving", "hook_clause", "warm_clause", "warm_path",
    }


def test_a_missing_short_name_is_a_clear_error(ctx):
    with pytest.raises(RuntimeError) as e:
        rt.draft_one(row(description=STRONG_JD), {**PROFILE, "candidate": {}}, ctx,
                     "referral", score_one=eh.score_one)
    assert "short_name" in str(e.value)


def test_drafting_a_no_jd_row_says_so_in_the_risks(ctx):
    _, risks = rt.draft_one(row(description="", company="Rubrik"), PROFILE, ctx, "referral",
                            score_one=eh.score_one)
    assert "No JD text" in risks


def test_join_never_emits_an_oxford_comma():
    assert rt._join(["a"]) == "a"
    assert rt._join(["a", "b"]) == "a and b"
    assert rt._join(["a", "b", "c"]) == "a, b and c"


def test_the_no_jd_skills_fallback_matches_the_lane_of_the_template(ctx):
    # A Lane B template reads "I shipped a multi-agent agent, so $skills_2 is work I've done".
    # Filling that with the Lane A fallback produced a non-sequitur on every no-JD row, which
    # is 31% of the corpus.
    b, _ = rt.draft_one(row(description="", lane="B", company="Adobe"), PROFILE, ctx,
                        "referral", score_one=eh.score_one)
    assert rt.FALLBACK_SKILLS["B"][0] in b
    assert rt.FALLBACK_SKILLS["A"][0] not in b
    a, _ = rt.draft_one(row(description="", lane="A", company="Adobe"), PROFILE, ctx,
                        "referral", score_one=eh.score_one)
    assert rt.FALLBACK_SKILLS["A"][0] in a


def test_skills_are_spelled_for_a_human_in_the_message(ctx):
    msg, _ = rt.draft_one(row(description="Java and Spring Boot on AWS. Requirements: 3+ "
                                          "years."), PROFILE, ctx, "referral",
                          score_one=eh.score_one)
    assert "Java" in msg
    assert "java and" not in msg


def test_every_display_name_is_a_real_vocabulary_term(ctx):
    # A DISPLAY key that no longer exists in the vocabulary is dead config that silently stops
    # applying, so the message quietly regresses to lowercase.
    unknown = sorted(set(rt.DISPLAY) - set(ctx.skills))
    assert not unknown, unknown
