#!/usr/bin/env python3
"""
The scripted engine. Scores, drafts and tailors with no subprocess, no network and no account.

One accumulator of points per req, starting at a base, with each dimension adding or
subtracting a signed weight, then clamped and rounded. Additive rather than multiplicative
because `score_reason` is the only audit surface this scorer has, and
`+1.3 in band, +2.1 overlap, -1.8 relocation` explains itself where a product does not.

The sign convention, stated once because getting it wrong inverts a penalty into a bonus:
**every weight in `scoring.weights` is signed and added as-is. Penalties are negative. A cap is
a negative floor, applied with `max`, never `min`.** A config validator rejects a positive value
on a penalty key at startup, and a test per penalty asserts it lowers the pre-clamp points.

The scorer holds no opinions. Every vocabulary, weight and threshold is profile data
(`scoring:` in config/profile.yaml) with the defaults below as the fallback, so a profile that
omits the block entirely still scores and the live gitignored profile needs no edit.

    python3 engine_heuristic.py        # score nothing, just validate the config and vocabularies
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import re
import sys
from pathlib import Path

import engine
import fetch            # geo_verdict, lane_of, title_verdict, _term_re, _terms
import jobprofile
import referral_templates
import resume_master

ROOT = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- defaults

# The calibrated values. Mirrored in config/profile.example.yaml, and a test asserts the two
# agree key for key, because the example file is what an owner copies to build a profile that
# then OVERRIDES these -- so a stale example is worse than no example.
DEFAULTS: dict = {
    "my_yoe": 2.4,
    "tolerance": 1.0,
    "below_at": 1.0,
    "base": 3.7,
    "base_no_jd": 4.2,
    "ceiling": 9,

    "weights": {
        "yoe_in_band": 1.3,
        "yoe_below_band": -1.0,
        "yoe_per_year_over": -0.55,
        "yoe_floor": -2.2,
        "overlap_max": 3.0,
        "overlap_saturate": 4.0,
        "overlap_zone_mult": 1.0,
        "overlap_preamble_mult": 0.6,
        "boilerplate_cap": 0.5,
        "off_lane_max": -2.0,
        "off_lane_saturate": 1.5,
        "never_claim_per": -0.6,
        "never_claim_cap": -1.8,
        "cs_degree_required": -0.8,
        "services_per": -0.8,
        "services_cap": -2.4,
        "title_in_band": 0.5,
        "title_above_band": -0.5,
        "title_below_band": -1.6,
        "title_excluded": -2.0,
        "target_employer": 0.8,
        "comp_risk_employer": 0.0,
        "no_jd_title_in_band": 2.6,
        "no_jd_target_employer": 2.6,
        "no_jd_title_excluded": -2.6,
    },

    "geo_points": {"home": 0.6, "remote": 0.5, "relocate": 0.0, "india": 0.0, "unclear": -0.4},
    "geo_points_no_jd": {"home": 0.8, "remote": 0.6, "relocate": 0.0, "india": 0.0,
                         "unclear": 0.3},

    "source_bonus": {
        "instahyre": -0.3, "seed": -0.1, "smartrecruiters": -1.2,
        "avature": -1.2, "radancy": -0.6, "oracle": 0.4, "greenhouse": 0.4,
    },
    "source_bonus_default": 0.2,

    "speed_points": {"services": 0.0, "comp_risk": 0.0, "target_bigco": 0.0,
                     "aggregator": 0.0, "startup": 0.0, "default": 0.0},

    "skills": {
        # Weight = how central this is to THIS resume, not how popular it is in the market. A
        # JD matching four core terms earns the full overlap bonus; one matching twenty
        # secondary terms does not. Every term in these three tiers is evidenced by
        # config/resume.md or the profile's resume_summary, and build_context warns if one
        # stops being -- see `unevidenced_terms`.
        "core": {
            "java": 1.0,
            "spring boot": 1.0,
            "distributed systems": 0.9,
            "aws": 0.8,
            "microservices": 0.7,
            "spring": 0.7,
            "kafka": 0.7,
            "dynamodb": 0.6,
            "cdk": 0.6,
            "rabbitmq": 0.5,
            "observability": 0.5,
            "infrastructure as code": 0.5,
            "multi-region": 0.5,
            "lambda": 0.5,
            "redis": 0.4,
            "cloudwatch": 0.4,
            "rest api": 0.4,
            "system design": 0.4,
            "design patterns": 0.3,
            "object-oriented": 0.3,
            "postgresql": 0.3,
            "mysql": 0.3,
            "mongodb": 0.3,
            "docker": 0.2,
            "kubernetes": 0.2,
        },
        "lane_b": {
            "agentic": 0.9,
            "multi-agent": 0.9,
            "llm": 0.8,
            "genai": 0.6,
            "agent": 0.3,
        },
        "secondary": {
            "python": 0.3,
            "graphql": 0.2,
            "data structures": 0.2,
            "algorithms": 0.2,
            "c++": 0.2,
            "javascript": 0.15,
            "node.js": 0.15,
            # 0.1 and deliberately NOT a never-claim term: the resume's own "Never claim"
            # bullet QUALIFIES React ("real but side-project level") rather than ruling it
            # out. So it is worth a tenth of a point when a JD mentions it, and a penalty
            # when the JD is about it (off_lane_dense, four mentions).
            "react": 0.1,
            "jenkins": 0.1,
        },
        # Terms a JD says constantly and this resume does not evidence at all. They were in
        # `core` and `secondary` until a review measured them: fourteen unevidenced terms
        # summing to 5.00 against overlap_saturate 4.0, so a JD naming only AWS service nouns
        # and "scalable"/"low latency" boilerplate earned +2.63 of the maximum +3.00 overlap
        # bonus with nothing behind it. They are kept rather than deleted because a JD naming
        # them really is weak evidence of a backend role, and dropped to a tier whose entire
        # summed contribution is capped at `boilerplate_cap` (0.5 of a 4.0 saturation, so at
        # most +0.375 of the +3.00). Do not promote a term into this tier to "boost" it.
        "boilerplate": {
            "sqs": 0.1, "s3": 0.1, "ecs": 0.1, "cloudformation": 0.1, "sns": 0.1,
            "high throughput": 0.1, "low latency": 0.1, "ci/cd": 0.1, "rag": 0.1,
            "langchain": 0.1, "bedrock": 0.1, "typescript": 0.1, "scalable": 0.1,
            "restful": 0.1,
        },
        "aliases": {
            # Spellings to search for one canonical term. A hit on any of them counts ONCE, at
            # the canonical weight, and the reason string prints the canonical spelling. The
            # canonical spelling is NOT implicit: list it first, as every entry below does.
            "spring boot": ["spring boot", "springboot", "spring-boot"],
            "node.js": ["node.js", "nodejs", "node js"],
            "object-oriented": ["object-oriented", "object oriented", "oop", "ood"],
            "infrastructure as code": ["infrastructure as code", "iac"],
            "ci/cd": ["ci/cd", "cicd", "ci cd"],
            "rest api": ["rest api", "rest apis", "restful api"],
            "postgresql": ["postgresql", "postgres"],
            "multi-agent": ["multi-agent", "multi agent", "multiagent"],
            "genai": ["genai", "gen ai", "generative ai"],
            "llm": ["llm", "llms", "large language model"],
        },
    },

    # Band markers in the title. Quoted numerals so they load as strings. MTS/SMTS are here
    # because they are this band at Salesforce-shaped companies and filters.include_titles
    # already admits them; without them a "Software Engineer - SMTS" cleared the fetch gate
    # and scored zero on this dimension.
    "in_band_titles": ["ii", "2", "sde 2", "sde ii", "mts", "smts",
                       "member of technical staff"],
    "above_band_titles": ["iii", "3", "4", "5"],
    "below_band_titles": ["jr", "jr.", "junior", "graduate", "fresher", "associate",
                          "trainee", "entry level", "entry-level", "apprentice", "l1",
                          "sde 1", "sde i"],

    # A term in the TITLE means the job is not this lane, whatever the JD says.
    "off_lane_titles": {
        "frontend": 1.0, "front end": 1.0, "front-end": 1.0, "ui developer": 1.0,
        "data engineer": 1.0, "data scientist": 1.0, "qa": 1.0, "sdet": 1.0,
        "test engineer": 1.0, "automation engineer": 0.8, "devops": 0.8,
        "site reliability": 0.6, "support engineer": 1.0, "salesforce": 1.0, "sap": 1.0,
        "android": 1.0, "ios": 1.0, "mobile": 0.8,
    },
    # Minimum occurrences in the JD before it counts. One mention of "front-end" is in every
    # full-stack backend JD; four mentions of React is a React job.
    "off_lane_dense": {
        "react": 4, "angular": 3, "vue": 3, "css": 3, "html": 3, "selenium": 2,
        "cypress": 2, "jest": 3, "figma": 2, "tableau": 2, "power bi": 2,
    },
    "off_lane_dense_weight": 0.6,

    # comp.dealbreakers, expressed as phrases a script can find.
    "services_signals": [
        "client requirements", "client projects", "staffing", "contract to hire", "c2h",
        "third party payroll", "deputed", "consulting services", "it services",
        "shift timings", "immediate joiner", "service bond",
    ],
    # Spellings the resume's "Never claim" list does not literally use. SPELL THE WHOLE TOKEN:
    # a term starting with punctuation will not match when that punctuation follows a letter,
    # so `.net` misses "asp.net" entirely.
    "never_claim_extra": ["golang", "dotnet", "asp.net", ".net core", "c sharp", "objective-c"],

    "speed_notes": {
        "services": "Fast loop, but the comp band is the problem.",
        "comp_risk": "6-10 weeks; confirm the India band early.",
        "target_bigco": "6-10 weeks; big-company loop, several rounds.",
        "aggregator": "Unknown; aggregator listing, recruiter sets the pace.",
        "startup": "3-5 weeks; small org, short loop.",
        "default": "4-8 weeks if a referral surfaces it.",
    },
}

# Must be negative. A positive value here would add points where the dimension name promises
# to remove them, which is the one config error that cannot be spotted by reading a score.
PENALTY_KEYS = frozenset({
    "yoe_below_band", "yoe_per_year_over", "yoe_floor", "off_lane_max", "never_claim_per",
    "never_claim_cap", "cs_degree_required", "services_per", "services_cap",
    "title_above_band", "title_below_band", "title_excluded", "no_jd_title_excluded",
})
# Divisors and caps on a positive sum. Zero is a ZeroDivisionError, negative inverts the sign.
SATURATE_KEYS = frozenset({"overlap_saturate", "off_lane_saturate", "boilerplate_cap"})
# Signed and unconstrained: shipping them at zero is the point, and the sign is the owner's.
FREE_KEYS = frozenset({"comp_risk_employer"})

SKILL_TIERS = ("core", "lane_b", "secondary", "boilerplate")
EVIDENCED_TIERS = ("core", "lane_b", "secondary")

CEILING_FALLBACK = 9

# --------------------------------------------------------------------------- text helpers

UNIT = r"(?:years?|yrs?)"
# "100-year company, we are proudly building..." appears in the live data and a naive pattern
# reads it as a 100-year experience requirement. Blanked before matching; the >25 discard below
# is the second net under this one.
YOE_NOISE = re.compile(r"\b\d{2,}\s*-?\s*year[\s-]+"
                       r"(?:company|old|history|legacy|anniversary|journey)")
YOE_RANGE = re.compile(rf"(?<![\d.])(\d{{1,2}}(?:\.\d)?)\s*\+?\s*(?:-|–|—|to)\s*"
                       rf"(\d{{1,2}}(?:\.\d)?)\s*\+?\s*{UNIT}")
YOE_PLUS = re.compile(rf"(?<![\d.])(\d{{1,2}}(?:\.\d)?)\s*\+\s*{UNIT}")
YOE_BARE = re.compile(rf"(?<![\d.])(\d{{1,2}}(?:\.\d)?)\s*{UNIT}\s+(?:of\s+)?"
                      rf"(?:\w+\s+){{0,3}}experience")
YOE_MAX_PLAUSIBLE = 25.0

# The slice of a JD that states what is actually required, as opposed to mentioned. Falls back
# to the whole JD when no such heading exists -- many aggregator JDs have no headings at all,
# and being silent there would exempt the noisiest source.
ZONE_HEAD = re.compile(r"(required|requirements|qualifications|must have|minimum qualif|"
                       r"what you'?ll need|basic qualif|skills required|mandatory)")
CS_DEGREE = re.compile(r"computer science degree|b\.?s\.?c?\.? in computer science|"
                       r"b\.?tech in computer science|degree in computer science|"
                       r"bachelor'?s? (?:degree )?in computer science")
STARTUP_SIGNS = ("funded", "series", "seed")

# Fragments of the resume's "### Never claim" section that are their own scoring dimension, or
# are a qualification rather than a prohibition.
NEVER_VERBISH = re.compile(r"\b(is|are|was|were|not|only|level|development|experience"
                           r"|figure|degree)\b")
NEVER_YOE_BULLET = re.compile(r"years?[-\s]of[-\s]experience|\byoe\b", re.I)
NEVER_DEG_BULLET = re.compile(r"\bdegree\b.*\bcomputer science\b", re.I)


def pad(s: str) -> str:
    """
    Lowercased and space-padded, once per field per row.

    The padding is what lets a term at the very start or end of the string still see a word
    boundary, the same trick fetch.title_verdict uses. The lowering is normative, not
    incidental: fetch._term_re builds lowercase-only character classes, so an uppercase term
    silently never matches.
    """
    return f" {(s or '').lower()} "


def never_terms(resume: str | None) -> set[str]:
    """
    The resume's own "### Never claim" list, as matchable terms.

    Four rules, each earning its place against the live file: drop any lead-in before `:`;
    split on comma, `or`, `and` and `/`; strip a trailing period; keep a fragment only if it is
    at most three words and carries no verb-ish token. The naive "split the bullet on commas"
    reading loses Go and Android, which are the two canonical cases -- `- Production experience
    in: Go, Rust, ...` yields `Production experience in: Go` as its first fragment, which
    matches nothing.

    The years-of-experience and CS-degree bullets are routed out to their own dimensions, and
    the React bullet yields nothing because it qualifies a skill rather than prohibiting it.
    """
    if not resume or "### Never claim" not in resume:
        return set()
    sec = re.split(r"\n#{1,3} ", resume.split("### Never claim", 1)[1])[0]
    out: set[str] = set()
    for line in sec.splitlines():
        if not line.strip().startswith("- "):
            continue
        b = line.strip()[2:].strip()
        if NEVER_YOE_BULLET.search(b) or NEVER_DEG_BULLET.search(b):
            continue
        body = b.split(":", 1)[1] if ":" in b else b
        for frag in re.split(r",|\bor\b|\band\b|/", body):
            frag = frag.strip().strip(".").strip()
            if not frag or len(frag.split()) > 3 or NEVER_VERBISH.search(frag.lower()):
                continue
            out.add(frag.lower())
    return out


def parse_yoe(low: str) -> float | None:
    """
    The minimum years figure the JD states, or None.

    `min` of every match, not max or mean: JDs state the hard minimum first and list preferred
    extras after it ("2+ years required, 5+ preferred"), and the minimum is the screen gate.
    Runs against the already-lowered text, so no re.I flag anywhere.
    """
    if not low:
        return None
    text = YOE_NOISE.sub(" ", low)
    found: list[float] = []
    for m in YOE_RANGE.finditer(text):
        found.append(float(m.group(1)))              # the low end of a range
    for rx in (YOE_PLUS, YOE_BARE):
        for m in rx.finditer(text):
            found.append(float(m.group(1)))
    found = [f for f in found if 0 < f <= YOE_MAX_PLAUSIBLE]
    return min(found) if found else None


def requirements_zone(low: str) -> str:
    m = ZONE_HEAD.search(low)
    return low[m.start():] if m else low


# --------------------------------------------------------------------------- context

# Re-exported from engine so a stage can catch it without importing this module, which is
# the lazy-import rule that keeps claudecall off the heuristic path applied the other way.
ConfigError = engine.ConfigError


@dataclasses.dataclass(frozen=True)
class Ctx:
    """
    Everything score_one reads, built once. No I/O happens below this line, which is what makes
    score_one pure and lets the calibration harness and the tests call it with a plain dict.
    """
    cfg: dict
    w: dict
    rx: dict                       # term -> compiled pattern, compiled once not 806 times
    skills: dict                   # canonical term -> weight, across all four tiers
    tier_of: dict                  # canonical term -> tier name
    spellings: dict                # canonical term -> spellings to search
    never: frozenset
    filters: dict
    geo_cfg: dict
    targets: tuple
    comp_risk: tuple
    short_name: str
    warnings: tuple = ()


@dataclasses.dataclass(frozen=True)
class Verdict:
    score: int                     # 1..ceiling, clamped
    points: float                  # pre-clamp accumulator, which is what the penalty tests read
    lane: str
    geo: str
    reason: str
    speed: str
    parts: dict
    clauses: tuple


def _merge(base: dict, over: dict | None) -> dict:
    """
    Dict-of-dict merge, which is all the scoring block is shaped like.

    A key present with nothing under it clears that block rather than being ignored. YAML parses
    `boilerplate:` with no body to None, and "empty this tier" is the only thing an owner could
    mean by writing it; without this rule it silently kept every default in the tier.
    """
    out = dict(base)
    for k, v in (over or {}).items():
        if v is None and isinstance(out.get(k), dict):
            out[k] = {}
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _check_terms_lowercase(cfg: dict) -> None:
    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}.{k}")
                else:
                    _one(k, f"{path}")
        elif isinstance(node, list):
            for t in node:
                _one(t, path)

    def _one(term, path):
        if not isinstance(term, str):
            raise ConfigError(f"config/profile.yaml: scoring.{path} term {term!r} is not a "
                              f"string. Quote numerals so they load as strings.")
        if term != term.lower():
            raise ConfigError(f"config/profile.yaml: scoring.{path} term {term!r} must be "
                              f"lowercase; an uppercase term never matches.")

    for key in ("skills", "in_band_titles", "above_band_titles", "below_band_titles",
                "off_lane_titles", "off_lane_dense", "services_signals", "never_claim_extra"):
        walk(cfg.get(key), key)


def _check_weights(w: dict) -> None:
    for k, v in sorted(w.items()):
        if not isinstance(v, (int, float)):
            raise ConfigError(f"config/profile.yaml: scoring.weights.{k} must be a number, "
                              f"got {v!r}")
        if k in FREE_KEYS:
            continue
        if k in SATURATE_KEYS:
            if v <= 0:
                raise ConfigError(f"config/profile.yaml: scoring.weights.{k} must be "
                                  f"positive, got {v}")
        elif k in PENALTY_KEYS:
            if v >= 0:
                raise ConfigError(f"config/profile.yaml: scoring.weights.{k} must be "
                                  f"negative, got {v}. Every weight is added as-is, so a "
                                  f"positive value here would raise the score.")
        elif v < 0:
            raise ConfigError(f"config/profile.yaml: scoring.weights.{k} is a bonus and must "
                              f"be non-negative, got {v}")


def _build_skills(cfg: dict, never: frozenset) -> tuple[dict, dict, dict]:
    skills: dict = {}
    tier_of: dict = {}
    sk = cfg.get("skills") or {}
    for tier in SKILL_TIERS:
        for term, weight in sorted((sk.get(tier) or {}).items()):
            if term in tier_of:
                raise ConfigError(
                    f"config/profile.yaml: {term!r} appears in both scoring.skills."
                    f"{tier_of[term]} and scoring.skills.{tier}; a term belongs to exactly "
                    f"one tier")
            if term in never:
                raise ConfigError(
                    f"config/profile.yaml: {term!r} is in scoring.skills.{tier} and is also "
                    f"on the resume's 'Never claim' list; scoring it would contradict the "
                    f"resume. Remove it from one of the two.")
            if not isinstance(weight, (int, float)) or weight < 0:
                raise ConfigError(f"config/profile.yaml: scoring.skills.{tier}.{term} must be "
                                  f"a non-negative number, got {weight!r}")
            skills[term] = float(weight)
            tier_of[term] = tier

    aliases = sk.get("aliases") or {}
    spellings = {t: [t] for t in skills}
    for canon, spells in sorted(aliases.items()):
        if canon not in skills:
            raise ConfigError(
                f"config/profile.yaml: scoring.skills.aliases has an entry for {canon!r}, "
                f"which is not in any skills tier. Remove the alias or add the term.")
        # The canonical spelling is not implicit, so an alias list that omits it would stop
        # matching the term's own name. Every default entry lists it first.
        spellings[canon] = sorted({str(s).lower() for s in spells} | {canon})
    return skills, tier_of, spellings


def unevidenced_terms(skills: dict, spellings: dict, evidence: str) -> list[str]:
    """
    Which scored terms the resume and profile do not back up, under the alias machinery.

    A term with no evidence anywhere is a term that adds points for nothing. Fourteen of them
    were measured in the shipped defaults and moved to the `boilerplate` tier, whose whole
    summed contribution is capped; this function is what stops the next fourteen from arriving
    unnoticed in `core`.
    """
    low = pad(evidence)
    out = []
    for term in sorted(skills):
        if not any(re.search(fetch._term_re(s), low) for s in spellings[term]):
            out.append(term)
    return out


def build_context(profile: dict, master: str | None = None) -> Ctx:
    """
    Validate the scoring config and compile every vocabulary into one immutable context.

    Raises ConfigError, naming the key, for anything that would otherwise score silently
    wrongly: a penalty with a positive sign, an uppercase term, a term in two tiers, an alias
    for a term that no longer exists.
    """
    cfg = _merge(DEFAULTS, profile.get("scoring") or {})
    _check_terms_lowercase(cfg)
    w = dict(cfg.get("weights") or {})
    _check_weights(w)

    never = frozenset(never_terms(master)
                      | {str(t).lower() for t in (cfg.get("never_claim_extra") or [])})
    skills, tier_of, spellings = _build_skills(cfg, never)

    # Every term that will be matched, compiled once. 806 reqs against ~200 patterns is 160k
    # re.compile calls if this is not cached.
    rx: dict = {}

    def compile_terms(terms):
        for t in terms:
            if t not in rx:
                rx[t] = re.compile(fetch._term_re(t))

    for spells in spellings.values():
        compile_terms(spells)
    compile_terms(never)
    for key in ("in_band_titles", "above_band_titles", "below_band_titles"):
        compile_terms(fetch._terms(cfg, key))
    compile_terms((cfg.get("off_lane_titles") or {}).keys())
    compile_terms((cfg.get("off_lane_dense") or {}).keys())
    compile_terms(str(s).lower() for s in (cfg.get("services_signals") or []))
    compile_terms(STARTUP_SIGNS)

    targets = tuple(sorted(str(c).lower() for c in
                           ((profile.get("targets") or {}).get("companies") or [])))
    comp_risk = tuple(sorted(str(c).lower() for c in
                             ((profile.get("targets") or {}).get("comp_risk") or [])))
    compile_terms(targets)
    compile_terms(comp_risk)

    # The evidence check. Terms in the `boilerplate` tier are exempt by construction -- that
    # tier exists precisely to hold unevidenced vocabulary at a capped weight -- so a warning
    # here means a term is being scored at full weight with nothing behind it.
    warnings: list[str] = []
    evidence = " ".join(filter(None, [master or "", profile.get("resume_summary") or "",
                                      str((profile.get("roles") or {}).get("lane_a") or ""),
                                      str((profile.get("roles") or {}).get("lane_b") or ""),
                                      profile.get("situation") or ""]))
    if evidence.strip():
        scored = {t: v for t, v in skills.items() if tier_of[t] in EVIDENCED_TIERS}
        missing = unevidenced_terms(scored, spellings, evidence)
        if missing:
            warnings.append(
                f"scoring.skills has {len(missing)} term(s) with no evidence in the master "
                f"resume or resume_summary: {', '.join(missing)}. They add points for nothing; "
                f"move them to scoring.skills.boilerplate or drop them.")

    return Ctx(
        cfg=cfg, w=w, rx=rx, skills=skills, tier_of=tier_of, spellings=spellings,
        never=never, filters=profile.get("filters") or {}, geo_cfg=profile.get("geo") or {},
        targets=targets, comp_risk=comp_risk,
        short_name=str((profile.get("candidate") or {}).get("short_name") or "").strip(),
        warnings=tuple(warnings),
    )


def _present(ctx: Ctx, low: str, term: str) -> bool:
    rx = ctx.rx.get(term) or re.compile(fetch._term_re(term))
    return rx.search(low) is not None


def _count(ctx: Ctx, low: str, term: str) -> int:
    rx = ctx.rx.get(term) or re.compile(fetch._term_re(term))
    return len(rx.findall(low))


def _hit_any(ctx: Ctx, low: str, terms) -> str | None:
    """The first matching term, in sorted order so the output does not depend on dict order."""
    return next((t for t in sorted(terms) if _present(ctx, low, t)), None)


# --------------------------------------------------------------------------- the formula

def matched_skills(ctx: Ctx, low: str) -> list[tuple[str, float, str]]:
    """
    (canonical term, weight, tier) for every skill the JD names, highest weight first.

    Sorted by (-weight, term) rather than by dict order so the reason string and the drafter's
    skill slots are reproducible across processes.
    """
    out = []
    for term in sorted(ctx.skills):
        if any(_present(ctx, low, s) for s in ctx.spellings[term]):
            out.append((term, ctx.skills[term], ctx.tier_of[term]))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def overlap_bonus(ctx: Ctx, low: str, zone: str) -> tuple[float, list[tuple[str, float, str]]]:
    """
    The overlap bonus and the matched terms at their effective weights, zone-weighted.

    `requirements_zone` already separates what a JD *requires* from what it merely *says*, and
    the never-claim dimension has used that split from the start. Overlap did not: a term named
    once in the marketing preamble ("we are a Java shop at heart") earned exactly what a term
    listed under `Requirements:` earned, so prose scored like a hard requirement. A term inside
    the zone is now worth `overlap_zone_mult` of its weight and a term only outside it
    `overlap_preamble_mult`, each canonical term counted once at the better of the two.

    The boilerplate tier is multiplied the same way and keeps its own `boilerplate_cap` sum, so
    the cap still bounds the whole unevidenced tier rather than being applied per term.

    When `ZONE_HEAD` finds no heading the zone IS the whole JD, so every term is a zone match
    and the number is unchanged. That is the common case for heading-less aggregator JDs, and
    it is deliberate: there is no preamble to discount when the JD has no structure to read.
    """
    w = ctx.w
    zmult, pmult = float(w["overlap_zone_mult"]), float(w["overlap_preamble_mult"])
    scored: list[tuple[str, float, str]] = []
    for term, weight, tier in matched_skills(ctx, low):
        in_zone = any(_present(ctx, zone, s) for s in ctx.spellings[term])
        # `max` rather than zmult: "the better of the two" is the owner's config, not this
        # function's assumption that the zone multiplier is the larger one.
        scored.append((term, weight * (max(zmult, pmult) if in_zone else pmult), tier))
    strong = sum(e for _, e, tier in scored if tier in EVIDENCED_TIERS)
    boiler = min(sum(e for _, e, tier in scored if tier == "boilerplate"),
                 float(w["boilerplate_cap"]))
    sat = float(w["overlap_saturate"])
    bonus = float(w["overlap_max"]) * min(strong + boiler, sat) / sat
    return bonus, sorted(scored, key=lambda x: (-x[1], x[0]))


def geo_bucket(ctx: Ctx, row) -> str:
    """
    The geo bucket, read and never written.

    Prefers the stored jobs.geo, which fetch already classified, and falls back to
    fetch.geo_verdict for a row that has none. geo_verdict's None means "drop" at fetch time;
    for a row already in the DB it means unclear, not delete.
    """
    stored = (_row(row, "geo") or "").strip().lower()
    if stored in ("home", "remote", "relocate", "india", "unclear"):
        return stored
    bucket, _ = fetch.geo_verdict(_row(row, "location") or "", ctx.geo_cfg)
    return bucket if bucket in ("home", "remote", "relocate", "india") else "unclear"


def _row(row, key, default=None):
    """Read a sqlite3.Row or a plain dict the same way, so tests can pass dicts."""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def speed_bucket(ctx: Ctx, clow: str, low: str, source: str, services_hit: bool) -> str:
    """
    A company-shape bucket, tested in a fixed order so the output is deterministic.

    comp_risk before target_bigco because the five comp-risk companies are all also targets and
    the band warning is the more useful of the two sentences.
    """
    if services_hit:
        return "services"
    if _hit_any(ctx, clow, ctx.comp_risk):
        return "comp_risk"
    if _hit_any(ctx, clow, ctx.targets):
        return "target_bigco"
    if source == "instahyre":
        return "aggregator"
    if low and _hit_any(ctx, low, STARTUP_SIGNS):
        return "startup"
    return "default"


def _clean(s: str) -> str:
    """
    One line, no pipes, no em dashes.

    score_reason is rendered in digest.py's bullets and speed_note lands in a table row, so a
    newline or a pipe from a JD-derived fragment would break the Markdown. Collapsed once here
    rather than trusted at every call site.
    """
    return " ".join(s.replace("|", " ").replace("\u2014", ",").replace("\u2013", ",").split())


def _assemble(clauses: list[tuple[int, str]], limit: int = 40) -> str:
    """
    The clauses, highest priority first, truncated to a word budget.

    Clauses are appended while the running count stays under the budget and the lowest-priority
    ones drop first, so an unusually long matched-skill list cannot blow the 40 words the
    Claude prompt asked for.
    """
    out, words = [], 0
    for _, text in sorted(clauses, key=lambda c: c[0]):
        n = len(text.split())
        if out and words + n > limit:
            continue
        out.append(text)
        words += n
    return _clean("; ".join(out))


def score_one(row, ctx: Ctx) -> Verdict:
    """
    One req, one verdict. Pure: no I/O, no clock, no randomness, no dict-order dependence.

    Two paths, because a req with no JD text is a different problem rather than this one with
    terms missing -- 31% of the stored corpus has no JD, and that slice is both harder and
    genuinely worse, so it gets its own base and its own weights.
    """
    cfg, w = ctx.cfg, ctx.w
    title = _row(row, "title", "") or ""
    company = _row(row, "company", "") or ""
    source = (_row(row, "source", "") or "").lower()
    desc = (_row(row, "description", "") or "").strip()

    tlow, clow, low = pad(title), pad(company), pad(desc)
    has_jd = bool(desc)
    parts: dict = {}
    clauses: list[tuple[int, str]] = []
    ceiling = int(cfg.get("ceiling", CEILING_FALLBACK))

    def add(name: str, value: float) -> None:
        if value:
            parts[name] = round(parts.get(name, 0.0) + value, 4)

    points = float(cfg["base" if has_jd else "base_no_jd"])
    parts["base"] = points

    # 1. Source prior, measured from the stored labels.
    src = float((cfg.get("source_bonus") or {}).get(source,
                                                    cfg.get("source_bonus_default", 0.2)))
    add("source", src)
    points += src

    in_band_hit = _hit_any(ctx, tlow, fetch._terms(cfg, "in_band_titles"))
    above_hit = _hit_any(ctx, tlow, fetch._terms(cfg, "above_band_titles"))
    below_hit = _hit_any(ctx, tlow, fetch._terms(cfg, "below_band_titles"))
    kept, why = fetch.title_verdict(title, ctx.filters)
    # `title_verdict` also returns "no include_titles match", which fires on seed placeholder
    # titles like "MongoDB req (title unknown)" and must NOT read as above-band.
    over_ceiling = (not kept) and why.startswith("excluded title term")
    is_target = _hit_any(ctx, clow, ctx.targets)
    is_comp_risk = _hit_any(ctx, clow, ctx.comp_risk)
    geo = geo_bucket(ctx, row)
    services_hits: list[str] = []

    if has_jd:
        zone = requirements_zone(low)

        # 2. YOE band fit.
        yoe = parse_yoe(low)
        if yoe is not None:
            over = yoe - (float(cfg["my_yoe"]) + float(cfg["tolerance"]))
            if over <= 0 and yoe > float(cfg["below_at"]):
                add("yoe", w["yoe_in_band"])
                points += w["yoe_in_band"]
                clauses.append((20, f"JD asks {_num(yoe)} yrs, in band"))
            elif over <= 0:
                add("yoe", w["yoe_below_band"])
                points += w["yoe_below_band"]
                clauses.append((20, f"JD asks only {_num(yoe)} yrs, reads as a new-grad req"))
            else:
                # Both negative, so `max` is the floor. Uncapped, this one dimension dragged
                # good reqs at good employers to 2 where Claude had given 5 and 6.
                pen = max(w["yoe_floor"], w["yoe_per_year_over"] * over)
                add("yoe", pen)
                points += pen
                clauses.append((20, f"JD asks {_num(yoe)}+ yrs vs {_num(cfg['my_yoe'])}"))

        # 3. Resume overlap, weighted, zone-weighted and saturating. A term under
        # `Requirements:` outweighs the same term in the preamble, and the boilerplate tier
        # sums separately and is capped, so a JD made of AWS service nouns cannot earn the
        # evidenced bonus.
        bonus, hits = overlap_bonus(ctx, low, zone)
        add("overlap", bonus)
        points += bonus
        named = [t for t, _, tier in hits if tier in EVIDENCED_TIERS][:4]
        if named:
            clauses.append((10, "matched " + ", ".join(named)))
        elif hits:
            clauses.append((10, "only generic cloud vocabulary matched, no core overlap"))
        else:
            clauses.append((10, "no resume overlap in the JD at all"))

        # 4. Off-lane work: an in-band title can sit on work that is not this lane.
        off = 0.0
        off_named = []
        for term, weight in sorted((cfg.get("off_lane_titles") or {}).items()):
            if _present(ctx, tlow, term):
                off += float(weight)
                off_named.append(term)
        dense_w = float(cfg.get("off_lane_dense_weight", 0.6))
        for term, need in sorted((cfg.get("off_lane_dense") or {}).items()):
            if _count(ctx, low, term) >= int(need):
                off += dense_w
                off_named.append(f"{term} x{int(need)}+")
        if off:
            osat = float(w["off_lane_saturate"])
            pen = float(w["off_lane_max"]) * min(off, osat) / osat
            add("off_lane", pen)
            points += pen
            clauses.append((30, "off lane: " + ", ".join(off_named[:3])))

        # 5. Never-claim terms demanded in the requirements zone. The resume's list outranks
        # the JD, so these can only ever subtract.
        nc = sorted(t for t in ctx.never if _present(ctx, zone, t))
        if nc:
            pen = max(w["never_claim_cap"], w["never_claim_per"] * len(nc))
            add("never_claim", pen)
            points += pen
            clauses.append((40, f"requires {', '.join(nc[:3])}, which the resume cannot claim"))

        # 6. CS degree demanded. The B.Tech is Electrical Engineering.
        if CS_DEGREE.search(zone):
            add("cs_degree", w["cs_degree_required"])
            points += w["cs_degree_required"]
            clauses.append((50, "demands a CS degree"))

        # 7. Services and contract signals: comp.dealbreakers, as phrases a script can find.
        services_hits = [t for t in sorted(str(s).lower()
                                           for s in (cfg.get("services_signals") or []))
                         if _present(ctx, low, t)]
        if services_hits:
            pen = max(w["services_cap"], w["services_per"] * len(services_hits))
            add("services", pen)
            points += pen
            clauses.append((60, f"services signals ({', '.join(services_hits[:2])})"))

        # 8. Title band. Added once per list, not once per matching marker.
        if in_band_hit:
            add("title_band", w["title_in_band"])
            points += w["title_in_band"]
            clauses.append((70, "title reads in band"))
        if above_hit:
            add("title_band", w["title_above_band"])
            points += w["title_above_band"]
            clauses.append((70, f"title reads a level up ({above_hit})"))
        if below_hit:
            add("title_band", w["title_below_band"])
            points += w["title_below_band"]
            clauses.append((70, f"title reads below band ({below_hit})"))

        # 9. Title above the ceiling. Separate from 8 and stacks with it: a numeral is a level,
        # an excluded word is a ceiling.
        if over_ceiling:
            add("ceiling", w["title_excluded"])
            points += w["title_excluded"]
            clauses.append((80, f"title reads above band ({why.split(': ', 1)[1]})"))

        # 11. Target employer, whole-word through the same helper as everything else.
        if is_target:
            add("target", w["target_employer"])
            points += w["target_employer"]
    else:
        clauses.append((10, "no JD text, so scored on title, company and location only"))
        if in_band_hit:
            add("title_band", w["no_jd_title_in_band"])
            points += w["no_jd_title_in_band"]
            clauses.append((70, "title reads in band"))
        if above_hit:
            add("title_band", w["title_above_band"])
            points += w["title_above_band"]
            clauses.append((70, f"title reads a level up ({above_hit})"))
        if below_hit:
            add("title_band", w["title_below_band"])
            points += w["title_below_band"]
            clauses.append((70, f"title reads below band ({below_hit})"))
        if over_ceiling:
            add("ceiling", w["no_jd_title_excluded"])
            points += w["no_jd_title_excluded"]
            clauses.append((80, f"title reads above band ({why.split(': ', 1)[1]})"))
        if is_target:
            add("target", w["no_jd_target_employer"])
            points += w["no_jd_target_employer"]

    # 10. Geography.
    gkey = "geo_points" if has_jd else "geo_points_no_jd"
    gpts = float((cfg.get(gkey) or {}).get(geo, 0.0))
    add("geo", gpts)
    points += gpts
    clauses.append((90, {
        "home": "Hyderabad, no relocation",
        "remote": "remote, no relocation",
        "relocate": "needs a relocation",
        "india": "in India, city unclear",
        "unclear": "location unclear",
    }[geo]))

    # 12. Comp-risk employer. Zero by default and that is measured, not an oversight: in the
    # stored labels the comp_risk companies scored HIGHER than the other targets. The clause
    # and the speed bucket still change at weight zero, so the config is genuinely consumed.
    if is_comp_risk:
        add("comp_risk", w["comp_risk_employer"])
        points += w["comp_risk_employer"]
        clauses.append((100, "on the comp_risk list, so confirm the band first"))
    elif is_target:
        clauses.append((100, "a target employer"))

    # 13. Speed. Wired at zero: time-to-offer is not derivable from a JD, so a weight here
    # would be a weight on a company-shape guess. Sweepable rather than written off.
    bucket = speed_bucket(ctx, clow, low, source, bool(services_hits))
    spts = float((cfg.get("speed_points") or {}).get(bucket, 0.0))
    add("speed", spts)
    points += spts

    # floor(x + 0.5), not round(): Python's round is banker's rounding, so round(6.5) == 6 but
    # round(5.5) == 6 -- asymmetric at exactly the boundary that decides digest promotion.
    score = max(1, min(ceiling, math.floor(points + 0.5)))
    speed = _clean((cfg.get("speed_notes") or {}).get(bucket, ""))
    return Verdict(
        score=score, points=round(points, 4),
        lane=fetch.lane_of(title, ctx.filters), geo=geo,
        reason=_assemble(clauses), speed=speed,
        parts=parts, clauses=tuple(sorted(clauses, key=lambda c: c[0])),
    )


def _num(x: float) -> str:
    """2.4 stays 2.4; 3.0 prints as 3, because "JD asks 3.0 yrs" reads like a bug."""
    return f"{x:g}"


# --------------------------------------------------------------------------- the engine

HEURISTIC_GROUP = 50


class HeuristicEngine:
    """
    The scripted engine. Every method is a loop over a pure per-row helper, so the list-shaped
    interface costs it nothing and a failure on one row cannot cost the others.
    """

    name = "heuristic"

    def preflight(self, profile: dict | None = None) -> tuple[bool, str]:
        # Nothing to check. There is no binary, no session and no network, which is the whole
        # point of this engine.
        return True, "scripted engine, nothing to check"

    def plan(self, rows: list, *, batch_size: int = 0) -> list[list]:
        # `batch_size` is ignored, which is what --batch's help text already says. The grouping
        # is the same 50 that on_batch fires on, so the stage's header count and its progress
        # lines agree rather than one counting reqs and the other groups.
        return [rows[i:i + HEURISTIC_GROUP] for i in range(0, len(rows), HEURISTIC_GROUP)]

    def dry_run_preview(self, profile: dict, unit: list, *,
                        master: str | None = None) -> str:
        ctx = build_context(profile, master)
        out = [f"engine: heuristic (no subprocess, no network)",
               f"base {ctx.cfg['base']} with JD / {ctx.cfg['base_no_jd']} without, "
               f"ceiling {ctx.cfg['ceiling']}", ""]
        for warn in ctx.warnings:
            out.append(f"WARNING: {warn}")
        if ctx.warnings:
            out.append("")
        for row in unit[:3]:
            v = score_one(row, ctx)
            out.append(f"### {_row(row, 'company')} — {_row(row, 'title')}")
            out.append(f"score {v.score}  (points {v.points:+.2f})  lane {v.lane}  geo {v.geo}")
            for k in sorted(v.parts):
                out.append(f"    {k:<12} {v.parts[k]:+.2f}")
            out.append(f"  reason: {v.reason}")
            out.append(f"  speed:  {v.speed}")
            out.append("")
        return "\n".join(out)

    def score_reqs(self, profile: dict, rows: list, *, master: str | None = None,
                   batch_size: int = 0, announce: str = "",
                   on_batch=None) -> list[dict]:
        ctx = build_context(profile, master)
        for warn in ctx.warnings:
            print(f"WARNING: {warn}", flush=True)
        groups = self.plan(rows)
        out: list[dict] = []
        offset = 0
        for i, group in enumerate(groups, 1):
            results, error = [], None
            for k, row in enumerate(group):
                try:
                    v = score_one(row, ctx)
                except Exception as e:  # noqa: BLE001
                    # One malformed row must not cost the other 805.
                    error = e
                    continue
                results.append({"n": offset + k + 1, "score": v.score, "lane": v.lane,
                                "reason": v.reason, "speed": v.speed})
            if on_batch is not None:
                on_batch(i, len(groups), len(group), results, error)
            out.extend(results)
            offset += len(group)
        return out

    def draft_referrals(self, profile: dict, rows: list, kind: str, *,
                        announce: str = "") -> list:
        ctx = build_context(profile, _read_master())
        out = []
        for row in rows:
            try:
                out.append(referral_templates.draft_one(row, profile, ctx, kind,
                                                        score_one=score_one))
            except Exception as e:  # noqa: BLE001
                out.append(e)
        return out

    def tailor_resumes(self, master: str, profile: dict, rows: list, *,
                       announce: str = "") -> list:
        ctx = build_context(profile, master)
        doc = resume_master.parse_master(master)
        out = []
        for row in rows:
            try:
                out.append(resume_master.tailor_one(row, doc, ctx))
            except Exception as e:  # noqa: BLE001
                out.append(e)
        return out


def _read_master() -> str | None:
    """
    The master resume, for the never-claim list the drafter's risk bullets quote.

    The drafter's stage does not resolve the resume today, so this is the one file read the
    heuristic engine does on its own. The scorer never does: score_one stays pure and
    score.py hands it `master`.
    """
    for p in (ROOT / "config" / "resume.md", ROOT / "config" / "resume.example.md"):
        if p.exists():
            return p.read_text(encoding="utf-8")
    return None


if __name__ == "__main__":
    if not jobprofile.PROFILE.exists():
        sys.exit(f"no profile at {jobprofile.PROFILE} — copy config/profile.example.yaml")
    prof = jobprofile.load()
    try:
        c = build_context(prof, _read_master())
    except ConfigError as e:
        sys.exit(str(e))
    print(f"scoring config ok: {len(c.skills)} skill terms across {len(SKILL_TIERS)} tiers, "
          f"{len(c.never)} never-claim terms, {len(c.rx)} compiled patterns")
    print(f"never-claim: {', '.join(sorted(c.never))}")
    for warn in c.warnings:
        print(f"WARNING: {warn}")
    if not c.warnings:
        print("every scored skill term is evidenced by the resume or the profile")
