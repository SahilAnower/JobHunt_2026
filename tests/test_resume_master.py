"""
The deterministic tailorer: parse the master, reorder against one JD, emit four sections.

The central case here is HIGH-1. The claim check applies to the TAILORED RESUME section ONLY;
GAPS, WHAT CHANGED and KEYWORDS ADDED are the tool's commentary and are exempt. Without that
scope the check refuses every real row, because GAPS exists precisely to name a JD term the
resume cannot claim.
"""

import pytest

import engine
import engine_heuristic as eh
import resume_master as rm
from tests.test_engine_heuristic import MASTER, PROFILE, row


@pytest.fixture(scope="module")
def ctx():
    return eh.build_context(PROFILE, MASTER)


@pytest.fixture(scope="module")
def doc():
    return rm.parse_master(MASTER)


JD_GO = ("We build distributed systems. Requirements: Go, Java and 3+ years of experience, "
         "plus a degree in Computer Science.")
JD_JAVA = ("Java and Spring Boot services on AWS with CloudWatch observability and CDK. "
           "Requirements: 3+ years.")


# --------------------------------------------------------------------------- parsing

def test_the_master_parses_into_its_sections(doc):
    assert [j.employer for j in doc.jobs] == ["Amazon"]
    assert doc.jobs[0].title == "Software Development Engineer"
    assert len(doc.jobs[0].bullets) == 2
    assert [cat for cat, _ in doc.skills] == ["Languages", "Technologies"]
    assert doc.education
    assert doc.yoe_line == "- **Experience:** 2.4 years"


def test_wrapped_bullets_are_rejoined_into_one_line(doc):
    # Every Amazon bullet in the live resume wraps across four or five source lines, so this is
    # not an edge case -- and it is why the output check is over claims rather than raw lines.
    first = doc.jobs[0].bullets[0]
    assert "\n" not in first
    assert "multi-region" in first and "CloudWatch alarms" in first


def test_the_notes_and_never_claim_sections_are_parsed_but_kept_out_of_canon(doc):
    assert any("Python" in n for n in doc.notes)
    assert any("Go" in n for n in doc.never_claim)
    # Emitting either into a resume body would be a disclosure bug, and keeping them out of
    # canon is what makes "a never-claim term cannot be emitted" structural rather than asserted.
    assert not any("Never claim" in c for c in doc.canon)
    assert not any("Rust" in c for c in doc.canon)


def test_canon_is_order_independent_for_skills_items(doc):
    assert rm._canon("- **Languages:** Java, C++, JavaScript") in doc.canon
    assert rm._canon("- **Languages:** Java, JavaScript, C++") in doc.canon


def test_canon_rejects_an_added_or_edited_item(doc):
    assert rm._canon("- **Languages:** Java, JavaScript, C++, Go") not in doc.canon
    assert rm._canon("- **Languages:** Java, JavaScript, Rust") not in doc.canon


def test_canon_accepts_a_rewrapped_bullet(doc):
    original = doc.jobs[0].bullets[0]
    rewrapped = original.replace(" ", "\n   ", 1)
    assert rm._canon("- " + " ".join(rewrapped.split())) in doc.canon


def test_a_master_with_no_experience_block_is_fatal():
    bad = "# X\n\n---\n\n## Header\n\n- **Name:** X\n\n---\n"
    with pytest.raises(rm.MasterShapeError) as e:
        rm.parse_master(bad)
    assert "Experience" in str(e.value)
    # tailor.py's existing handler catches engine.ConfigError, so this exits 1 with a sentence
    # rather than a traceback.
    assert isinstance(e.value, engine.ConfigError)


def test_a_master_with_no_separator_is_fatal():
    with pytest.raises(rm.MasterShapeError):
        rm.parse_master("# X\n\n## Header\n- **Name:** X\n")


# --------------------------------------------------------------------------- HIGH-1

def test_a_gaps_entry_naming_go_is_written_not_refused(ctx, doc):
    """
    HIGH-1, the case that decides whether this stage writes anything at all.

    GAPS has to be able to name a never-claim term the JD demands. Under a claim check applied
    to all four sections, this row is refused and the tailorer writes nothing on every real
    posting that asks for Go.
    """
    parts = rm.tailor_one(row(description=JD_GO), doc, ctx)
    assert "go" in parts["GAPS"].lower()
    assert "Never claim" in parts["GAPS"]
    assert parts["TAILORED RESUME"]


def test_a_never_claim_term_still_cannot_reach_the_resume_body(ctx, doc):
    # The exemption is scoped: GAPS may name Go, the resume body may not contain it. Structural,
    # because canon is built from the body only.
    parts = rm.tailor_one(row(description=JD_GO), doc, ctx)
    body = parts["TAILORED RESUME"].lower()
    for term in ("go,", " go ", "rust", "terraform", "kotlin"):
        assert term not in body, term


def test_the_claim_check_fires_when_the_reorderer_invents_a_line(ctx, doc):
    # A runtime assertion of the invariant, kept in production rather than test-only, because
    # the thing it guards against is a resume claiming something the owner cannot defend.
    import dataclasses
    tampered = dataclasses.replace(
        doc, jobs=(dataclasses.replace(doc.jobs[0],
                                       bullets=doc.jobs[0].bullets + ("Expert in Go.",)),))
    with pytest.raises(rm.ClaimLeak) as e:
        rm.tailor_one(row(description=JD_JAVA), tampered, ctx)
    assert "not present in the master" in str(e.value)
    assert "Expert in Go" in str(e.value)


# --------------------------------------------------------------------------- reordering

def test_bullets_are_reordered_by_jd_relevance(ctx, doc):
    agentic = rm.tailor_one(row(description="We need multi-agent Gen-AI and LLM work. "
                                            "Requirements: 3+ years."), doc, ctx)
    body = agentic["TAILORED RESUME"]
    assert body.index("multi-agent Gen-AI") < body.index("multi-region")


def test_a_jd_matching_nothing_leaves_master_order_untouched(ctx, doc):
    parts = rm.tailor_one(row(description="We sell artisanal candles. Apply within."),
                          doc, ctx)
    assert "No reordering was warranted" in parts["WHAT CHANGED"]
    master_body = "\n".join(rm._emit_lines(doc.header, doc.jobs, doc.skills, doc.projects,
                                           doc.education)).strip()
    assert parts["TAILORED RESUME"] == master_body


def test_employers_and_skill_categories_stay_in_master_order(ctx, doc):
    parts = rm.tailor_one(row(description=JD_JAVA), doc, ctx)
    body = parts["TAILORED RESUME"]
    assert body.index("Languages") < body.index("Technologies")


def test_relevance_needs_the_term_on_both_sides(ctx):
    # Only terms present on BOTH sides count, so a bullet is promoted for matching THIS JD
    # rather than for being generally keyword-dense.
    total, named = rm.relevance("Built services in Java", eh.pad("We need Java"), ctx)
    assert "java" in named and total > 0
    total, named = rm.relevance("Built services in Java", eh.pad("We need Python"), ctx)
    assert named == [] and total == 0


def test_relevance_uses_whole_word_matching(ctx):
    # `go` in a bullet must not match `mongo` in a JD.
    total, named = rm.relevance("We used MongoDB", eh.pad("experience with go"), ctx)
    assert "go" not in named


def test_the_years_figure_and_the_header_are_emitted_verbatim(ctx, doc):
    parts = rm.tailor_one(row(description=JD_GO), doc, ctx)
    assert "- **Experience:** 2.4 years" in parts["TAILORED RESUME"]
    assert "2.4 years figure" in parts["WHAT CHANGED"]


def test_no_bullet_is_dropped_or_added(ctx, doc):
    parts = rm.tailor_one(row(description=JD_JAVA), doc, ctx)
    body = parts["TAILORED RESUME"]
    for job in doc.jobs:
        for b in job.bullets:
            assert b in body


# --------------------------------------------------------------------------- the sections

def test_what_changed_always_states_what_was_left_alone(ctx, doc):
    for jd in (JD_JAVA, JD_GO, "nothing relevant"):
        parts = rm.tailor_one(row(description=jd), doc, ctx)
        assert "Left the employer order" in parts["WHAT CHANGED"]


def test_keywords_added_says_nothing_was_added(ctx, doc):
    parts = rm.tailor_one(row(description=JD_JAVA), doc, ctx)
    assert "Nothing was added" in parts["KEYWORDS ADDED"]
    assert "java" in parts["KEYWORDS ADDED"].lower()


def test_keywords_added_reports_an_empty_intersection_as_the_finding(ctx, doc):
    parts = rm.tailor_one(row(description="We sell artisanal candles."), doc, ctx)
    assert "read GAPS" in parts["KEYWORDS ADDED"]


def test_gaps_reports_the_years_ask_without_changing_the_figure(ctx, doc):
    parts = rm.tailor_one(row(description="Requirements: 8+ years of Java experience."),
                          doc, ctx)
    assert "8+ years against 2.4" in parts["GAPS"]
    assert "unchanged" in parts["GAPS"]


def test_gaps_marks_a_requirements_zone_term_as_required(ctx, doc):
    parts = rm.tailor_one(row(description=JD_GO), doc, ctx)
    assert "required by the JD" in parts["GAPS"]


def test_gaps_is_capped_so_it_stays_readable(ctx, doc):
    jd = ("Requirements: " + ", ".join(sorted(ctx.skills)) + ". 3+ years.")
    parts = rm.tailor_one(row(description=jd), doc, ctx)
    line = next((l for l in parts["GAPS"].splitlines() if "not evidenced" in l), "")
    assert line
    assert line.count(",") <= rm.GAP_TERM_CAP


def test_gaps_says_so_when_there_is_nothing_to_flag(ctx, doc):
    parts = rm.tailor_one(row(description="Java. Requirements: 2+ years."), doc, ctx)
    assert "Nothing in the JD is unevidenced" in parts["GAPS"] or "not evidenced" in parts["GAPS"]


def test_all_four_sections_are_non_empty(ctx, doc):
    # write_tailored indexes all four unguarded, so a missing key is a KeyError at write time.
    parts = rm.tailor_one(row(description=JD_JAVA), doc, ctx)
    assert set(parts) == set(("TAILORED RESUME", "WHAT CHANGED", "KEYWORDS ADDED", "GAPS"))
    assert all(v.strip() for v in parts.values())


# --------------------------------------------------------------------------- the engine

def test_tailor_resumes_is_positionally_aligned_and_contains_errors(ctx):
    rows = [row(key="a::1", description=JD_JAVA), row(key="b::1", description=JD_GO)]
    got = engine.get("heuristic").tailor_resumes(MASTER, PROFILE, rows)
    assert len(got) == 2
    assert all(isinstance(g, dict) for g in got)


def test_tailoring_is_deterministic(ctx):
    rows = [row(description=JD_JAVA)]
    a = engine.get("heuristic").tailor_resumes(MASTER, PROFILE, rows)
    b = engine.get("heuristic").tailor_resumes(MASTER, PROFILE, rows)
    assert a == b
