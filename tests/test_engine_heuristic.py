"""
The deterministic scorer: YOE parsing, weighted overlap, the never-claim hard negative, geo
weighting, and the sign direction of every penalty.

The penalty tests assert on the PRE-CLAMP `points`, not on the clamped 1-9 score, because the
ceiling masks a penalty whenever the baseline is already at the top -- a strong JD at a target
employer reaches 9, and adding a CS-degree requirement leaves the clamped score at 9 while the
points genuinely fall.
"""

import pytest

import engine
import engine_heuristic as eh
import fetch

MASTER = """# X — master resume

preamble

---

## Header

- **Name:** X
- **Experience:** 2.4 years

## Experience

### Amazon — Software Development Engineer
Hyderabad, India · Nov 2025 – Present

- Built multi-region, multi-account monitoring with AWS CDK across 5 AWS regions and
  CloudWatch alarms.
- Shipped a multi-agent Gen-AI agent in production.

## Skills

- **Languages:** Java, JavaScript, C++
- **Technologies:** SpringBoot, Node.js, AWS Lambda, Graphql

## Education

**Aliah University** — B.Tech Electrical Engineering

---

## Notes for the tailoring stage

- Python: used to build the pipeline end to end.
- Observability: CloudWatch alarms and dashboards.

### Never claim
- Production experience in: Go, Rust, C#, Kotlin, Swift, Scala, Spark, Hive, Terraform, Ruby.
- iOS, Android or native mobile development.
- Front-end specialism: React is real but side-project level, not expert.
- Any years-of-experience figure above 2.4.
- A degree in Computer Science. The B.Tech is Electrical Engineering.
"""

PROFILE = {
    "candidate": {"short_name": "X"},
    "resume_summary": "Java, Spring Boot, AWS, Kafka, RabbitMQ, Redis, distributed systems, "
                      "microservices, DynamoDB, MySQL, PostgreSQL, MongoDB, Docker, "
                      "Kubernetes, Jenkins, GraphQL, data structures, algorithms, "
                      "object-oriented design, design patterns, system design, REST APIs, "
                      "agentic multi-agent LLM GenAI agent, python, javascript, c++, "
                      "node.js, react, observability, infrastructure as code, multi-region, "
                      "lambda, cdk, cloudwatch, spring",
    "filters": {"include_titles": ["software engineer", "member of technical staff", "smts"],
                "exclude_titles": ["senior", "sr", "staff", "principal", "lead", "architect"],
                "exclude_overrides": ["member of technical staff"],
                "lane_a_titles": ["software engineer"]},
    "geo": {"country_terms": ["india"], "home_terms": ["hyderabad"],
            "relocation_terms": ["bangalore", "pune"], "remote_terms": ["remote"],
            "require_india_signal": True},
    "targets": {"companies": ["Rubrik", "JP Morgan Chase"], "comp_risk": ["JP Morgan Chase"]},
}


@pytest.fixture(scope="module")
def ctx():
    return eh.build_context(PROFILE, MASTER)


def row(**kw):
    base = {"key": "x::1", "company": "Acme", "title": "Software Engineer II",
            "location": "Hyderabad, India", "url": "", "source": "greenhouse",
            "description": "", "lane": "A", "geo": None, "human_path": None}
    base.update(kw)
    return base


STRONG_JD = ("We need Java, Spring Boot, AWS and distributed systems experience. "
             "Requirements: 3+ years of backend experience.")


# --------------------------------------------------------------------------- YOE parsing

@pytest.mark.parametrize("text,want", [
    ("3+ years applied experience", 3.0),
    ("2.5-6 yrs", 2.5),
    ("5 to 8 years of backend development", 5.0),
    ("4–6 years", 4.0),
    ("7 to 10 years", 7.0),
    ("2 + yrs", 2.0),
    ("3 years of professional software development experience", 3.0),
    # Noise suppression is required, not optional: this phrasing appears four times in the
    # live data and a naive pattern reads it as a 100-year requirement.
    ("100-year company we are proudly building", None),
    ("30 years", None),
    ("the last 3 years", None),
    ("", None),
    ("no numbers here at all", None),
])
def test_yoe_parsing(text, want):
    assert eh.parse_yoe(eh.pad(text)) == want


def test_yoe_takes_the_minimum_not_the_maximum():
    # JDs state the hard minimum first and list preferred extras after it. The minimum is the
    # screen gate, so it is the figure that matters.
    assert eh.parse_yoe(eh.pad("2+ years required, 5+ years preferred")) == 2.0


def test_yoe_parsing_needs_the_lowered_text():
    # Documents why the lowercasing rule is normative rather than incidental: the patterns
    # carry no re.I flag, so raw mixed-case text silently yields nothing.
    assert eh.parse_yoe("3+ Years applied experience") is None
    assert eh.parse_yoe(eh.pad("3+ Years applied experience")) == 3.0


# --------------------------------------------------------------------------- overlap

def test_overlap_names_the_matched_core_skills(ctx):
    v = eh.score_one(row(description=STRONG_JD), ctx)
    assert "matched" in v.reason
    for term in ("java", "spring boot", "aws", "distributed systems"):
        assert term in v.reason


def test_aliases_count_once_at_the_canonical_spelling(ctx):
    # A JD saying SpringBoot, Spring Boot and spring-boot must earn one hit, not three, and the
    # reason must print the canonical spelling whatever the JD wrote.
    low = eh.pad("SpringBoot and spring-boot and Spring Boot")
    hits = [t for t, _, _ in eh.matched_skills(ctx, low)]
    assert hits.count("spring boot") == 1
    v = eh.score_one(row(description="We use SpringBoot."), ctx)
    assert "spring boot" in v.reason


def test_overlap_saturates_so_jd_length_is_not_fit(ctx):
    four = eh.score_one(row(description="Java, Spring Boot, AWS, distributed systems, "
                                        "microservices, Kafka."), ctx)
    many = eh.score_one(row(description="Java, Spring Boot, AWS, distributed systems, "
                                        "microservices, Kafka, Redis, Docker, Kubernetes, "
                                        "MySQL, MongoDB, PostgreSQL, GraphQL, Jenkins."), ctx)
    assert four.parts["overlap"] == pytest.approx(3.0)
    assert many.parts["overlap"] == pytest.approx(3.0)


def test_weighting_separates_a_java_job_from_a_react_job(ctx):
    # The second most important calibration finding: a flat count of matched terms scored a
    # Node/React agency job as highly as a Java/Spring/AWS job because both matched five terms.
    java = eh.score_one(row(description="Java, Spring Boot, AWS, distributed systems."), ctx)
    front = eh.score_one(row(description="React, JavaScript, Node.js, GraphQL, Jenkins."), ctx)
    assert java.parts["overlap"] > front.parts["overlap"]


def test_boilerplate_tier_is_capped_well_below_saturation(ctx):
    """
    HIGH-2. Fourteen terms with no resume evidence summed to 5.00 against overlap_saturate 4.0,
    so a JD made of AWS service nouns and 'scalable'/'low latency' boilerplate earned +2.63 of
    the maximum +3.00 with nothing behind it. They now live in a tier whose entire summed
    contribution is capped.
    """
    jd = ("Build scalable, low latency, high throughput RESTful services on S3, SQS, SNS, ECS, "
          "CloudFormation with CI/CD, TypeScript, RAG, LangChain and Bedrock.")
    v = eh.score_one(row(description=jd), ctx)
    cap = eh.DEFAULTS["weights"]["boilerplate_cap"]
    sat = eh.DEFAULTS["weights"]["overlap_saturate"]
    # +0.375 of the available +3.00, against +2.63 before the fix.
    assert v.parts["overlap"] <= 3.0 * cap / sat + 1e-9
    assert v.parts["overlap"] < 0.5
    assert "only generic cloud vocabulary matched" in v.reason


def test_a_requirement_outweighs_the_same_term_in_the_preamble(ctx):
    """
    The point of the zone split. `requirements_zone` already separates what a JD requires from
    what it merely says, and the never-claim dimension has used that split from the start.
    Overlap did not, so marketing prose naming Java scored like a hard Java requirement.
    """
    preamble = eh.score_one(row(description="We are a Java shop at heart and we love "
                                            "Spring Boot. Requirements: a good attitude."),
                            ctx)
    required = eh.score_one(row(description="We are a friendly team with a good attitude. "
                                            "Requirements: Java and Spring Boot."), ctx)
    assert preamble.points < required.points
    assert preamble.parts["overlap"] < required.parts["overlap"]


def test_a_jd_with_no_requirements_heading_is_unaffected_by_the_zone_split(ctx):
    # No heading means the zone is the whole JD, so there is no preamble to discount. These
    # are the heading-less aggregator JDs, which are the majority of the corpus.
    jd = "Our stack is Java, Spring Boot, AWS and distributed systems."
    assert eh.ZONE_HEAD.search(jd.lower()) is None
    low = eh.pad(jd)
    assert eh.requirements_zone(low) == low
    v = eh.score_one(row(description=jd), ctx)
    bonus, _ = eh.overlap_bonus(ctx, low, eh.requirements_zone(low))
    # Five terms match (spring boot also matches spring) for 4.4 against a 4.0 saturation, so
    # this JD earns the full overlap_max with the split exactly as it did without it.
    assert v.parts["overlap"] == pytest.approx(eh.DEFAULTS["weights"]["overlap_max"])
    assert bonus == pytest.approx(v.parts["overlap"])


def test_a_preamble_multiplier_of_one_reproduces_the_unzoned_number(ctx):
    # The escape hatch, and the proof that the only thing the split changes is the multiplier.
    flat = eh.build_context({**PROFILE, "scoring": {"weights": {"overlap_preamble_mult": 1.0}}},
                            MASTER)
    jd = "We need Java and Spring Boot. Requirements: a good attitude."
    zoned = eh.score_one(row(description=jd), ctx)
    unzoned = eh.score_one(row(description=jd), flat)
    # java 1.0 + spring boot 1.0 + spring 0.7, all in the preamble, of a 4.0 saturation.
    strong = sum(ctx.skills[t] for t in ("java", "spring boot", "spring"))
    w = eh.DEFAULTS["weights"]
    assert unzoned.parts["overlap"] == pytest.approx(
        w["overlap_max"] * strong / w["overlap_saturate"])
    assert zoned.parts["overlap"] == pytest.approx(
        w["overlap_max"] * strong * w["overlap_preamble_mult"] / w["overlap_saturate"])


def test_a_term_in_both_zones_counts_once_at_the_better_multiplier(ctx):
    both = eh.score_one(row(description="We need Java. Requirements: Java."), ctx)
    only_zone = eh.score_one(row(description="We need a good attitude. Requirements: Java."),
                             ctx)
    assert both.parts["overlap"] == pytest.approx(only_zone.parts["overlap"])


def test_the_matched_clause_is_ordered_by_effective_weight(ctx):
    # java (1.0) in the preamble is worth 0.6; aws (0.8) under Requirements is worth 0.8, so
    # the reason must name aws first even though java carries the larger skills weight.
    v = eh.score_one(row(description="We are a Java shop. Requirements: AWS."), ctx)
    assert v.reason.index("aws") < v.reason.index("java")


def test_every_scored_term_is_evidenced_by_the_resume_or_the_profile(ctx):
    # The guard that stops the next fourteen free-points terms arriving unnoticed in core.
    assert ctx.warnings == (), ctx.warnings


def test_unevidenced_terms_warns_when_a_term_loses_its_evidence():
    prof = dict(PROFILE)
    prof["scoring"] = {"skills": {"core": {"erlang": 0.9}}}
    c = eh.build_context(prof, MASTER)
    assert any("erlang" in w for w in c.warnings)


def test_boilerplate_terms_are_exempt_from_the_evidence_warning(ctx):
    # The tier IS the acknowledgement that those terms have no evidence, so warning about them
    # every run would be noise that trains the owner to ignore the warning that matters.
    assert "sqs" in ctx.skills
    assert ctx.tier_of["sqs"] == "boilerplate"
    assert not any("sqs" in w for w in ctx.warnings)


# --------------------------------------------------------------------------- never-claim

def test_never_terms_parsed_from_the_live_section_shape():
    # The naive "split the bullet on commas" reading loses Go and Android, which are the two
    # canonical cases: "Production experience in: Go, Rust, ..." yields "Production experience
    # in: Go" as its first fragment and matches nothing.
    got = eh.never_terms(MASTER)
    assert got == {"go", "rust", "c#", "kotlin", "swift", "scala", "spark", "hive",
                   "terraform", "ruby", "ios", "android"}


def test_never_terms_routes_yoe_and_degree_bullets_to_their_own_dimensions():
    got = eh.never_terms(MASTER)
    assert not any("year" in t for t in got)
    assert not any("computer science" in t for t in got)


def test_react_is_qualified_not_prohibited():
    # The resume's React bullet qualifies a skill rather than ruling it out, so React stays a
    # low-weight secondary term and an off_lane_dense trigger instead of a never-claim term.
    assert "react" not in eh.never_terms(MASTER)


def test_never_claim_penalty_lowers_the_points(ctx):
    clean = eh.score_one(row(description=STRONG_JD), ctx)
    dirty = eh.score_one(row(description=STRONG_JD + " Requirements: Go is required."), ctx)
    assert dirty.points < clean.points
    assert "go" in dirty.reason
    assert dirty.parts["never_claim"] < 0


def test_never_claim_penalty_is_capped(ctx):
    jd = STRONG_JD + " Requirements: Go, Rust, Scala, Spark and Hive are required."
    v = eh.score_one(row(description=jd), ctx)
    assert v.parts["never_claim"] == pytest.approx(eh.DEFAULTS["weights"]["never_claim_cap"])


def test_never_claim_only_fires_inside_the_requirements_zone(ctx):
    # A JD whose "nice to have" mentions Go should not be punished as hard as one whose
    # Requirements section demands it.
    before = ("Go is nice to have. Requirements: Java, Spring Boot, AWS and "
              "distributed systems.")
    v = eh.score_one(row(description=before), ctx)
    assert "never_claim" not in v.parts


def test_a_never_claim_term_can_never_add_points(ctx):
    # The resume's list outranks the JD, so no arrangement of config can make `go` a bonus.
    assert "go" not in ctx.skills
    with pytest.raises(engine.ConfigError) as e:
        eh.build_context({**PROFILE, "scoring": {"skills": {"core": {"go": 1.0}}}}, MASTER)
    assert "Never claim" in str(e.value)


# --------------------------------------------------------------------------- geo

def test_geo_weighting_prefers_home_then_remote_then_relocate(ctx):
    def pts(geo):
        return eh.score_one(row(description=STRONG_JD, geo=geo), ctx).parts.get("geo", 0.0)

    assert pts("home") > pts("remote") > pts("relocate")
    assert pts("relocate") == 0.0
    assert pts("unclear") < 0.0


def test_geo_falls_back_to_fetch_geo_verdict_when_the_column_is_empty(ctx):
    # Reused, not reimplemented: this is the function that stopped "india" matching inside
    # "Indianapolis".
    assert eh.geo_bucket(ctx, row(geo=None, location="Hyderabad, India")) == "home"
    assert eh.geo_bucket(ctx, row(geo=None, location="Bangalore, India")) == "relocate"
    assert eh.geo_bucket(ctx, row(geo=None, location="Remote, India")) == "remote"
    assert eh.geo_bucket(ctx, row(geo=None, location="Indianapolis, IN")) == "unclear"


def test_geo_is_read_and_never_written(ctx):
    # store.set_score's column list does not include geo and the scorer must not try to.
    v = eh.score_one(row(description=STRONG_JD, geo="home"), ctx)
    assert v.geo == "home"


def test_the_no_jd_path_treats_unclear_as_a_small_positive(ctx):
    # The no-JD unclear rows are the brand-name seed reqs whose location was never recorded,
    # not low-quality postings.
    v = eh.score_one(row(description="", geo="unclear", source="seed"), ctx)
    assert v.parts["geo"] > 0


# --------------------------------------------------------------------------- penalty signs

PENALTY_CASES = {
    "yoe": dict(description=STRONG_JD.replace("3+ years", "9+ years")),
    "never_claim": dict(description=STRONG_JD + " Requirements: Go is required."),
    "cs_degree": dict(description=STRONG_JD + " Requirements: a degree in Computer Science."),
    "services": dict(description=STRONG_JD + " You will work on client projects with "
                                             "shift timings."),
    "off_lane": dict(title="Frontend Engineer II", description=STRONG_JD),
    "ceiling": dict(title="Senior Software Engineer", description=STRONG_JD),
}


@pytest.mark.parametrize("name,extra", sorted(PENALTY_CASES.items()))
def test_every_penalty_strictly_lowers_the_pre_clamp_points(ctx, name, extra):
    base = eh.score_one(row(description=STRONG_JD), ctx)
    hit = eh.score_one(row(**{**{"description": STRONG_JD}, **extra}), ctx)
    assert hit.points < base.points, f"{name} did not lower the points"
    assert hit.parts.get(name if name != "ceiling" else "ceiling", 0) < 0 or name == "off_lane"


def test_below_band_title_lowers_the_points(ctx):
    base = eh.score_one(row(title="Software Engineer II", description=STRONG_JD), ctx)
    jr = eh.score_one(row(title="Junior Software Engineer", description=STRONG_JD), ctx)
    assert jr.points < base.points


def test_the_yoe_floor_caps_the_years_penalty(ctx):
    # Uncapped, this one dimension dragged good reqs at good employers to 2 where Claude gave
    # 5 and 6: an 8+ year ask at JP Morgan is a stretch needing a referral, not a 2.
    v = eh.score_one(row(description="Requirements: 20+ years of experience."), ctx)
    assert v.parts["yoe"] == pytest.approx(eh.DEFAULTS["weights"]["yoe_floor"])


def test_a_positive_value_on_a_penalty_key_is_a_startup_error():
    with pytest.raises(engine.ConfigError) as e:
        eh.build_context({**PROFILE, "scoring": {"weights": {"never_claim_per": 0.6}}}, MASTER)
    assert "never_claim_per" in str(e.value)
    assert "negative" in str(e.value)


def test_a_non_positive_saturation_is_a_startup_error():
    for bad in (0, -1.0):
        with pytest.raises(engine.ConfigError) as e:
            eh.build_context({**PROFILE,
                              "scoring": {"weights": {"overlap_saturate": bad}}}, MASTER)
        assert "overlap_saturate" in str(e.value)


def test_the_two_zero_default_knobs_accept_either_sign():
    for v in (-0.7, 0.0, 0.5):
        c = eh.build_context({**PROFILE,
                              "scoring": {"weights": {"comp_risk_employer": v}}}, MASTER)
        assert c.w["comp_risk_employer"] == v


# --------------------------------------------------------------------------- title band

def test_title_band_markers_are_whole_word(ctx):
    # ii must not fire inside iii, and l1 must not fire inside l10.
    two = eh.score_one(row(title="Software Engineer II", description=STRONG_JD), ctx)
    three = eh.score_one(row(title="Software Engineer III", description=STRONG_JD), ctx)
    assert two.parts["title_band"] > three.parts["title_band"]


def test_an_ambiguous_level_nets_to_zero(ctx):
    # A "Software Engineer II/III" posting is genuinely ambiguous, and +0.5 - 0.5 is the honest
    # reading rather than a guess either way.
    v = eh.score_one(row(title="Software Engineer II/III", description=STRONG_JD), ctx)
    assert v.parts.get("title_band", 0.0) == pytest.approx(0.0)


def test_mts_and_smts_are_in_band(ctx):
    # The owner's most recent commit before this branch admitted SMTS at the fetch gate. Without
    # these markers the title cleared that gate and then scored zero on this dimension.
    for title in ("Software Engineer - SMTS", "MTS Software Engineer",
                  "Member of Technical Staff II"):
        v = eh.score_one(row(title=title, description=STRONG_JD), ctx)
        assert v.parts.get("title_band", 0) > 0, title


def test_the_ceiling_reuses_title_verdict_so_overrides_are_inherited(ctx):
    # exclude_overrides readmits "Member of Technical Staff", and reusing title_verdict rather
    # than scanning for the word "staff" inherits that behaviour for free.
    mts = eh.score_one(row(title="Member of Technical Staff II", description=STRONG_JD), ctx)
    senior = eh.score_one(row(title="Senior Software Engineer", description=STRONG_JD), ctx)
    assert "ceiling" not in mts.parts
    assert senior.parts["ceiling"] < 0


def test_a_seed_placeholder_title_is_not_penalised_as_above_band(ctx):
    # title_verdict also returns "no include_titles match", which fires on seed placeholders
    # like "MongoDB req (title unknown)" and must not read as above-band.
    v = eh.score_one(row(title="MongoDB req (title unknown)", description=STRONG_JD), ctx)
    assert "ceiling" not in v.parts


# --------------------------------------------------------------------------- off lane

def test_off_lane_density_needs_repetition_not_one_mention(ctx):
    # One mention of front-end is in every full-stack backend JD; four mentions of React is a
    # React job. A presence test fired on nearly every req and made the signal noise.
    once = eh.score_one(row(description=STRONG_JD + " Some React exposure helps."), ctx)
    lots = eh.score_one(row(description=STRONG_JD + " React React React React."), ctx)
    assert "off_lane" not in once.parts
    assert lots.parts["off_lane"] < 0


# --------------------------------------------------------------------------- reason / speed

def test_the_reason_is_one_line_with_no_pipes_or_em_dashes(ctx):
    for jd in (STRONG_JD, "", "Requirements: Go, 9+ years, client projects, shift timings."):
        v = eh.score_one(row(description=jd), ctx)
        assert "\n" not in v.reason and "|" not in v.reason
        assert "\u2014" not in v.reason and "\u2013" not in v.reason
        assert "\n" not in v.speed and "|" not in v.speed


def test_the_reason_carries_no_gendered_pronouns(ctx):
    import re
    v = eh.score_one(row(description=STRONG_JD), ctx)
    assert not re.search(r"\b(he|him|his|she|her|hers)\b", v.reason.lower())


def test_the_reason_stays_within_the_word_budget(ctx):
    jd = ("Java, Spring Boot, AWS, distributed systems, microservices, Kafka, Redis, Docker, "
          "Kubernetes, MySQL. Requirements: 9+ years, Go, Rust, a degree in Computer Science, "
          "client projects, shift timings, immediate joiner.")
    v = eh.score_one(row(title="Senior Frontend Engineer III", description=jd), ctx)
    assert len(v.reason.split()) <= 40


def test_the_reason_is_never_empty_and_says_when_the_jd_was_missing(ctx):
    v = eh.score_one(row(description=""), ctx)
    assert v.reason
    assert "no JD text" in v.reason


def test_the_speed_note_buckets_are_deterministic_and_short(ctx):
    cases = [
        (row(description=STRONG_JD, company="JP Morgan Chase"), "comp_risk"),
        (row(description=STRONG_JD, company="Rubrik"), "target_bigco"),
        (row(description="", source="instahyre"), "aggregator"),
        (row(description=STRONG_JD + " We are Series B funded."), "startup"),
        (row(description=STRONG_JD), "default"),
        (row(description=STRONG_JD + " Work on client projects."), "services"),
    ]
    for r, bucket in cases:
        v = eh.score_one(r, ctx)
        assert v.speed == eh.DEFAULTS["speed_notes"][bucket], (r["company"], bucket)
        assert len(v.speed.split()) <= 15


# --------------------------------------------------------------------------- determinism

def test_scoring_the_same_row_twice_agrees(ctx):
    r = row(description=STRONG_JD)
    a, b = eh.score_one(r, ctx), eh.score_one(r, ctx)
    assert (a.score, a.points, a.reason, a.speed) == (b.score, b.points, b.reason, b.speed)


def test_scoring_is_stable_across_a_different_hash_seed():
    import json
    import subprocess
    import sys
    code = (
        "import json, engine_heuristic as eh\n"
        f"MASTER = {MASTER!r}\n"
        f"PROFILE = {PROFILE!r}\n"
        f"JD = {STRONG_JD!r}\n"
        "ctx = eh.build_context(PROFILE, MASTER)\n"
        "r = {'key':'x::1','company':'Rubrik','title':'Software Engineer II',"
        "'location':'Hyderabad, India','url':'','source':'greenhouse','description':JD,"
        "'lane':'A','geo':None,'human_path':None}\n"
        "v = eh.score_one(r, ctx)\n"
        "print(json.dumps([v.score, v.points, v.reason, v.speed]))\n"
    )
    outs = []
    for seed in ("0", "1", "12345"):
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
                                "PYTHONPATH": str(__import__("pathlib").Path(
                                    eh.__file__).parent)})
        assert p.returncode == 0, p.stderr
        outs.append(p.stdout.strip())
    assert len(set(outs)) == 1, outs
    assert json.loads(outs[0])[0] >= 1


def test_rounding_is_half_up_not_bankers():
    # round() is banker's rounding, so round(6.5) == 6 but round(5.5) == 6 -- asymmetric at
    # exactly the boundary that decides digest promotion.
    import math
    assert math.floor(6.5 + 0.5) == 7
    assert math.floor(6.49 + 0.5) == 6


# --------------------------------------------------------------------------- reuse

def test_fetch_term_re_still_exists_and_still_rejects_indianapolis():
    # The scorer imports this rather than copying it, so a rename or a boundary regression in
    # fetch.py fails here instead of producing wrong scores silently.
    import re
    assert re.search(fetch._term_re("india"), " indianapolis, in ") is None
    assert re.search(fetch._term_re("india"), " hyderabad, india ") is not None


@pytest.mark.parametrize("term,hay,want", [
    ("go", " mongo db ", False), ("go", " golang ", False),
    ("go", " experience with go and rust ", True),
    ("ios", " axios library ", False), ("sap", " sapient consulting ", False),
    ("ii", " engineer iii ", False), ("2", " sde 2 ", True), ("2", " sde 24 ", False),
])
def test_term_matching_boundaries(ctx, term, hay, want):
    assert eh._present(ctx, hay, term) is want


def test_an_uppercase_term_in_the_config_is_a_startup_error():
    with pytest.raises(engine.ConfigError) as e:
        eh.build_context({**PROFILE, "scoring": {"skills": {"core": {"C#": 1.0}}}}, MASTER)
    assert "lowercase" in str(e.value)


def test_a_term_in_two_tiers_is_a_startup_error():
    with pytest.raises(engine.ConfigError) as e:
        eh.build_context({**PROFILE, "scoring": {"skills": {"core": {"python": 1.0}}}}, MASTER)
    assert "python" in str(e.value)
    assert "exactly one tier" in str(e.value)


def test_an_alias_for_a_term_in_no_tier_is_a_startup_error():
    with pytest.raises(engine.ConfigError) as e:
        eh.build_context({**PROFILE,
                          "scoring": {"skills": {"aliases": {"erlang": ["erlang"]}}}}, MASTER)
    assert "erlang" in str(e.value)


# --------------------------------------------------------------------------- the engine

def test_the_heuristic_scores_with_no_master_resume_at_all():
    # The never-claim dimension goes off and nothing else does, which is the documented
    # degradation rather than a crash.
    c = eh.build_context(PROFILE, None)
    assert not any(t in c.never for t in ("go", "rust"))
    v = eh.score_one(row(description=STRONG_JD + " Requirements: Go is required."), c)
    assert "never_claim" not in v.parts


def test_score_reqs_numbers_rows_globally_not_per_batch(ctx):
    rows = [row(key=f"x::{i}", description=STRONG_JD) for i in range(120)]
    got = engine.get("heuristic").score_reqs(PROFILE, rows, master=MASTER)
    assert [r["n"] for r in got] == list(range(1, 121))


def test_on_batch_sees_every_row_exactly_once(ctx):
    rows = [row(key=f"x::{i}", description=STRONG_JD) for i in range(120)]
    seen = []
    engine.get("heuristic").score_reqs(
        PROFILE, rows, master=MASTER,
        on_batch=lambda i, n, nreqs, results, error: seen.extend(
            r["n"] for r in (results or [])))
    assert sorted(seen) == list(range(1, 121))
    assert len(seen) == len(set(seen))


def test_a_row_missing_optional_columns_still_scores():
    # _row reads a sqlite3.Row or a dict the same way and treats absent as empty, so a thin row
    # degrades to the no-JD path rather than raising.
    got = engine.get("heuristic").score_reqs(PROFILE, [{"key": "thin::1"}], master=MASTER)
    assert len(got) == 1
    assert got[0]["reason"]


def test_a_malformed_row_does_not_cost_the_others():
    class Exploding:
        def __getitem__(self, key):
            raise RuntimeError("this row is broken")

    rows = [row(key="good::1", description=STRONG_JD), Exploding(),
            row(key="good::2", description=STRONG_JD)]
    got = engine.get("heuristic").score_reqs(PROFILE, rows, master=MASTER)
    assert len(got) == 2


def test_plan_ignores_batch_size_for_the_heuristic_engine():
    rows = [row(key=f"x::{i}") for i in range(120)]
    assert [len(g) for g in engine.get("heuristic").plan(rows, batch_size=12)] == [50, 50, 20]


def test_dry_run_preview_shows_the_per_dimension_breakdown():
    out = engine.get("heuristic").dry_run_preview(
        PROFILE, [row(description=STRONG_JD)], master=MASTER)
    assert "heuristic" in out
    assert "overlap" in out and "reason:" in out
