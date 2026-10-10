#!/usr/bin/env python3
"""
The referral-ask template bank, and the slot filling that turns one into a draft.

`draft.VOICE` is a set of instructions to a model. For a template bank those instructions
become properties of the text, checked by tests over every template in the bank rather than
over one sampled output: no em dashes, no banned openers, no gendered pronouns, one specific
resume detail rather than a career summary, under the word budget, signed with the short name.

Eighteen templates across eight buckets, keyed on (kind, warmth, lane), at least two per
bucket. The failure mode to avoid is three asks in one week reading as one form letter, and
`draft.py --limit 6` bounds the weekly draw, so two or three variants per bucket means a
*visible* repeat (two identical messages to two people at the same company) is unlikely twice
over. Selection is deterministic on the job key, never random, so re-drafting the same req
yields the same message and a redraft is reviewable as a diff.

`string.Template` with `safe_substitute`, not `str.format`: scraped JDs are full of `{` and `}`
and a stray brace in a template would be a silent landmine, while an unknown `$placeholder`
survives as visible literal text the owner catches at the "read it aloud" checklist step.

Templates are tracked in git. The sensitive strings (`never_mention`, `reason_for_leaving`) stay
in the gitignored profile, which is the split draft.py already made deliberately.
"""

from __future__ import annotations

import hashlib
import re
from string import Template

# --------------------------------------------------------------------------- the bank

# referral, warm, lane A
T1 = """Hi [name],

$company has a $role open ($req). I work on $skills_2 at Amazon, and that is most of what the
req describes.$hook_clause

$warm_clause Would you be willing to refer me? $geo_clause

I can send the resume and the link, whichever is easier.

$short_name"""

T2 = """Hi [name],

I'm going after the $role req at $company ($req). My current work is $skills_2, so the overlap
is real rather than aspirational.$hook_clause

$warm_clause A referral from you would get me past the screen, which is the part I keep losing.
$geo_clause

$short_name"""

T3 = """Hi [name],

Quick ask. $company is hiring a $role ($req) and I'd like to put my name in. I've spent the
last couple of years on $skills_2.$hook_clause

$warm_clause Can you refer me, or point me at whoever owns the req? $geo_clause

$short_name"""

# referral, cold, lane A
T4 = """Hi [name],

We haven't spoken, so I'll be quick. $company has a $role open ($req) and I'm applying. I'm a
backend engineer at Amazon working on $skills_2.$hook_clause

$geo_clause If you're on or near that team, would you consider a referral? If not, no worries
at all.

$short_name"""

T5 = """Hi [name],

I'm applying to the $role req at $company ($req) and I'm trying to reach someone inside rather
than the form. My work is $skills_2.$hook_clause

$geo_clause Would a referral be something you'd be open to?

$short_name"""

T6 = """Hi [name],

Reaching out cold about $company's $role req ($req). Short version: I build $skills_2, and the
posting reads like the work I do now.$hook_clause

$geo_clause If you can refer me I'd appreciate it. If you'd rather not, that's completely fine.

$short_name"""

# referral, warm, lane B
T7 = """Hi [name],

$company has a $role open ($req). I built a multi-agent Gen-AI agent that writes post-incident
reports in production, so $skills_2 is current work and not a side interest.$hook_clause

$warm_clause Would you refer me? $geo_clause

$short_name"""

T8 = """Hi [name],

I'd like a shot at the $role req at $company ($req). The closest thing I've shipped is a
multi-agent writing agent that took incident reports from days to same-day, which is $skills_2
territory.$hook_clause

$warm_clause Any chance of a referral? $geo_clause

$short_name"""

# referral, cold, lane B
T9 = """Hi [name],

Cold message about $company's $role req ($req). I shipped a multi-agent Gen-AI agent into
production at Amazon, so $skills_2 is something I've actually run rather than read about.$hook_clause

$geo_clause Would you be open to referring me?

$short_name"""

T10 = """Hi [name],

I'm applying for the $role req at $company ($req) and I'd rather not go in through the form. My
nearest work is a production multi-agent agent for incident write-ups, so $skills_2.$hook_clause

$geo_clause A referral would help more than anything else at this stage.

$short_name"""

# email, warm, lane A
T11 = """Hi [name],

I'm writing about the $role req at $company ($req). I'm an SDE at Amazon, two and a bit years
in, and my day job is $skills_3.$hook_clause

$warm_clause I'm looking to move for $reason_for_leaving, and this req is the closest match
I've seen to what I already do. $geo_clause

If you're willing to refer me I'll send the resume. A pointer to whoever owns the req
helps too.

$url

$short_name"""

T12 = """Hi [name],

The $role req at $company ($req) landed in my search this morning and I'd like to go for it. I
work on $skills_3 at Amazon.$hook_clause

$warm_clause The move is about $reason_for_leaving. $geo_clause

A referral is the ask. The screen is where this search keeps stalling, and a name attached to
the application changes that.

$url

$short_name"""

# email, cold, lane A
T13 = """Hi [name],

We haven't met. I'm applying to the $role req at $company ($req) and I'd rather a person saw it
than a filter. I'm an SDE at Amazon working on $skills_3.$hook_clause

$geo_clause I'm moving for $reason_for_leaving.

If a referral is something you'd consider, I'll send the resume. If not, thanks for reading this
far.

$url

$short_name"""

T14 = """Hi [name],

I'm writing cold about $company's $role req ($req). I've spent the last two years on
$skills_3, which is most of what the posting asks for.$hook_clause

$geo_clause The reason I'm looking is $reason_for_leaving.

Would you refer me, or tell me who owns the req? Either one helps.

$url

$short_name"""

# email, warm, lane B
T15 = """Hi [name],

About the $role req at $company ($req). I built and shipped a multi-agent Gen-AI agent that
writes post-incident reports at Amazon, which puts me in $skills_3.$hook_clause

$warm_clause I'm looking to move for $reason_for_leaving, into work where the agent side is the
job rather than a side project. $geo_clause

If you'd refer me I'll send the resume and the agent write-up.

$url

$short_name"""

T17 = """Hi [name],

I'd like to apply for the $role req at $company ($req). The relevant thing I've shipped is a
Collator to Writer to Validation agent pipeline in production, so $skills_3 is current.$hook_clause

$warm_clause The move is about $reason_for_leaving. $geo_clause

A referral would get this past the screen, which is where it otherwise dies.

$url

$short_name"""

# email, cold, lane B
T16 = """Hi [name],

Cold note about the $role req at $company ($req). I shipped a production multi-agent agent for
incident write-ups at Amazon, so $skills_3 is work I've done rather than a course I took.$hook_clause

$geo_clause I'm moving for $reason_for_leaving.

If you're open to referring me, I'll send the resume. If not, no hard feelings.

$url

$short_name"""

T18 = """Hi [name],

I'm applying to $company's $role req ($req) and writing to a person instead of a form. My
nearest work is a multi-agent Gen-AI pipeline in production, which is $skills_3.$hook_clause

$geo_clause The reason I'm looking is $reason_for_leaving.

Would a referral be possible? A pointer to the req owner works too.

$url

$short_name"""

BANK: dict[tuple[str, str, str], list[str]] = {
    ("referral", "warm", "A"): [T1, T2, T3],
    ("referral", "cold", "A"): [T4, T5, T6],
    ("referral", "warm", "B"): [T7, T8],
    ("referral", "cold", "B"): [T9, T10],
    ("email", "warm", "A"): [T11, T12],
    ("email", "cold", "A"): [T13, T14],
    ("email", "warm", "B"): [T15, T17],
    ("email", "cold", "B"): [T16, T18],
}

FALLBACK_BUCKET = ("referral", "cold", "A")

SLOTS = ("company", "role", "req", "url", "skills_2", "skills_3", "geo_clause",
         "short_name", "reason_for_leaving", "hook_clause", "warm_clause", "warm_path")

WORD_BUDGET = {"referral": 90, "email": 130}

# How a canonical skill term is spelled in a message a human reads. The vocabulary is lowercase
# because the matcher requires it, and "My work is java and spring boot" is the kind of detail
# that makes a referral ask read as generated. Only the drafter uses this; `score_reason` keeps
# the canonical lowercase spelling, because that is an audit string rather than prose.
DISPLAY = {
    "java": "Java", "spring boot": "Spring Boot", "spring": "Spring", "aws": "AWS",
    "kafka": "Kafka", "dynamodb": "DynamoDB", "cdk": "AWS CDK", "rabbitmq": "RabbitMQ",
    "lambda": "AWS Lambda", "redis": "Redis", "cloudwatch": "CloudWatch",
    "rest api": "REST APIs", "object-oriented": "object-oriented design",
    "postgresql": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB", "docker": "Docker",
    "kubernetes": "Kubernetes", "agentic": "agentic AI", "multi-agent": "multi-agent systems",
    "llm": "LLM", "genai": "GenAI", "python": "Python", "graphql": "GraphQL", "c++": "C++",
    "javascript": "JavaScript", "node.js": "Node.js", "react": "React", "jenkins": "Jenkins",
}

# (skills_2, skills_3) when the JD gave nothing to match, per lane. Both are true of the
# resume and neither names a specific technology, because with no JD there is no evidence that
# any particular one is the relevant thing to lead with.
FALLBACK_SKILLS = {
    "A": ("backend services", "backend and distributed systems work"),
    "B": ("agentic AI work", "agentic AI and LLM application work"),
}

GEO_CLAUSE = {
    "home": "I'm in Hyderabad already, so there's no relocation in this.",
    "remote": "The posting reads as remote, which works from where I am.",
    "relocate": "I'm in Hyderabad and would relocate for this.",
    "india": "I'm in Hyderabad and the role reads as India-based.",
    "unclear": "The posting doesn't state a location; I'm in Hyderabad.",
}

# A hook is one lifted sentence, and these are the three ways a lifted sentence could flatter
# or misrepresent, so a candidate carrying any of them is rejected rather than trimmed.
YEARS_IN_TEXT = re.compile(r"\d\s*\+?\s*(?:years?|yrs?)")
HOOK_MAX_WORDS = 12


def sanctioned_reason(raw: str) -> str:
    """
    The quotable part of `drafting.reason_for_leaving`: its first sentence, de-dashed.

    "Verbatim" cannot mean the whole string. The live value is `growth — a team reorganisation
    and a shift of investment toward AI narrowed the path. Never elaborate beyond this, and
    never volunteer the performance plan.` -- that is an em dash the voice rules forbid, 24 words
    out of a 90-word budget, and a second sentence that is an instruction to the drafter naming
    the one topic `never_mention` exists to suppress. So: first sentence only, em and en dashes
    become commas, trailing period dropped.
    """
    first = re.split(r"(?<=[.!?])\s", (raw or "").strip(), maxsplit=1)[0]
    out = first.replace("\u2014", ",").replace("\u2013", ",")
    # The live value spells it `growth — a team...`, with spaces around the dash, so a bare
    # replace leaves `growth , a team`. Collapse the space the dash was carrying.
    out = re.sub(r"\s+,", ",", out)
    return " ".join(out.split()).rstrip(".").strip()


def pick_variant(key: str, kind: str, warmth: str, lane: str) -> tuple[str, str]:
    """
    (template, note). Deterministic on the job key.

    hashlib.sha1, not hash(): hash() on a str is salted per process by PYTHONHASHSEED, so
    --redraft would produce a different message every run and a redraft would not be reviewable
    as a diff.
    """
    bucket = (kind, warmth, lane)
    variants = BANK.get(bucket) or []
    note = ""
    if not variants:
        variants = BANK[FALLBACK_BUCKET]
        note = f"no template for {bucket}, used the cold Lane A fallback"
    idx = int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(variants)
    return variants[idx], note


def jd_hook(desc: str, ctx, engine_heuristic) -> str:
    """
    One concrete, checkable detail about the work, lifted from the JD and never generated.

    The first sentence carrying a core-vocabulary term, cut at the first comma and capped at 12
    words. Rejected if it names a never-claim term, an off-lane term, or a years figure. When
    nothing qualifies the slot resolves to "" and the template's hook clause disappears, which
    is the only honest scripted reading of VOICE's "if the JD gives you nothing specific, say
    something true about the work instead of inventing a detail".
    """
    if not desc:
        return ""
    core = {t for t, tier in ctx.tier_of.items() if tier == "core"}
    off = set((ctx.cfg.get("off_lane_titles") or {}).keys())
    for raw in re.split(r"(?<=[.!?])\s+|\n", desc):
        clause = raw.split(",")[0].strip(" -*\u2022\t")
        clause = " ".join(clause.split())
        if not clause or len(clause.split()) > HOOK_MAX_WORDS or len(clause.split()) < 4:
            continue
        low = engine_heuristic.pad(clause)
        if not any(any(engine_heuristic._present(ctx, low, s) for s in ctx.spellings[t])
                   for t in sorted(core)):
            continue
        if YEARS_IN_TEXT.search(low):
            continue
        if any(engine_heuristic._present(ctx, low, t) for t in sorted(ctx.never)):
            continue
        if any(engine_heuristic._present(ctx, low, t) for t in sorted(off)):
            continue
        if "\u2014" in clause or "\u2013" in clause or "|" in clause:
            continue
        return clause.rstrip(".:;")
    return ""


def risk_bullets(row, verdict, hook: str, never_named: list[str], comp_risk: bool) -> str:
    """
    The risks section, generated from the facts the scorer already computed rather than
    templated, so the risks cannot disagree with the score in the same file.
    """
    out = []
    parts = verdict.parts
    if parts.get("yoe", 0) < 0:
        out.append("- The JD asks for more years than the resume shows. Expect that in a "
                   "screen.")
    if never_named:
        out.append(f"- The JD lists {', '.join(never_named[:3])} as a requirement and the "
                   f"resume cannot claim it. Decide before you send whether to raise it.")
    if parts.get("ceiling", 0) < 0:
        out.append("- The title reads above band. Confirm the level before you spend the ask.")
    if verdict.geo == "relocate":
        loc = (row["location"] if _has(row, "location") else "") or "another city"
        out.append(f"- This is in {loc}, so the ask carries a relocation. Confirm you want "
                   f"that.")
    if comp_risk:
        company = (row["company"] if _has(row, "company") else "") or "This company"
        out.append(f"- {company} is on your comp_risk list. Confirm the India band before you "
                   f"invest in the loop.")
    if not ((row["description"] if _has(row, "description") else "") or "").strip():
        out.append("- No JD text was available, so the detail above comes from the title and "
                   "the company only. Open the posting before you send.")
    if hook:
        out.append("- The specific detail in the message was lifted from the JD. Read the "
                   "posting and confirm it is still accurate.")
    return "\n".join(out[:4])


def _has(row, key) -> bool:
    try:
        row[key]
        return True
    except (KeyError, IndexError):
        return False


def _get(row, key, default=""):
    if not _has(row, key):
        return default
    v = row[key]
    return default if v is None else v


class Blocked(RuntimeError):
    """A rendered draft tripped the never_mention backstop. That row is not written."""


def draft_one(row, profile: dict, ctx, kind: str, *, score_one) -> tuple[str, str]:
    """
    (message, risks) for one req. Raises Blocked if the backstop fires.

    The real control on `never_mention` is slot provenance, not the post-render check: the only
    slots carrying text not authored in this tracked file are $hook_clause (from the JD),
    $reason_for_leaving (from the profile, narrowed to its first sentence) and $warm_path (the
    owner's own note on the req). No slot sources the `situation` block, the `never_mention`
    list, or any free text about the owner's circumstances, so none of the topics is reachable.
    The literal check below is a backstop under that, and it is documented as matching
    literally: write the phrase you must never see, not a description of the topic.
    """
    import engine_heuristic as eh

    cand = profile.get("candidate") or {}
    short_name = str(cand.get("short_name") or "").strip()
    if not short_name:
        raise RuntimeError("config/profile.yaml: candidate.short_name is required for drafting")

    d = profile.get("drafting") or {}
    verdict = score_one(row, ctx)
    desc = str(_get(row, "description") or "")
    low = eh.pad(desc)

    lane = (str(_get(row, "lane") or "A").strip().upper() or "A")[:1]
    if lane not in ("A", "B"):
        lane = "A"

    hits = eh.matched_skills(ctx, low) if desc else []
    strong = [t for t, _, tier in hits if tier in eh.EVIDENCED_TIERS]
    # The fallback has to match the lane of the template that will consume it. A Lane B
    # template says "I shipped a multi-agent agent, so $skills_2 is work I've done", and the
    # Lane A fallback turned that into "...so backend services is work I've done", which reads
    # as a non-sequitur. It fires on every no-JD row, which is 31% of the corpus.
    shown = [DISPLAY.get(t, t) for t in strong]
    skills_2 = _join(shown[:2]) or FALLBACK_SKILLS[lane][0]
    skills_3 = _join(shown[:3]) or FALLBACK_SKILLS[lane][1]

    hook = jd_hook(desc, ctx, eh)
    warm_path = str(_get(row, "human_path") or "").strip()
    warmth = "warm" if warm_path else "cold"

    template, note = pick_variant(str(_get(row, "key") or _get(row, "company")),
                                 kind, warmth, lane)
    req = str(_get(row, "key") or "").split("::")[-1] or str(_get(row, "key") or "")

    slots = {
        "company": str(_get(row, "company") or "the company"),
        "role": str(_get(row, "title") or "the role"),
        "req": req or "req id unknown",
        "url": str(_get(row, "url") or ""),
        "skills_2": skills_2,
        "skills_3": skills_3,
        "geo_clause": GEO_CLAUSE.get(verdict.geo, GEO_CLAUSE["unclear"]),
        "short_name": short_name,
        "reason_for_leaving": sanctioned_reason(str(d.get("reason_for_leaving") or "growth")),
        "hook_clause": f" The posting mentions {hook}, which is the part I'd want to work on."
                       if hook else "",
        "warm_clause": f"Context on my side: {warm_path}" if warm_path else "",
        "warm_path": warm_path,
    }

    message = _render(template, slots)
    budget = WORD_BUDGET.get(kind, 90)
    if len(message.split()) > budget and hook:
        # Drop the one optional clause and re-check. Every template fits the budget without it,
        # so this is the mechanism that keeps the budget rather than a hope.
        slots["hook_clause"] = ""
        hook = ""
        message = _render(template, slots)
    if len(message.split()) > budget:
        # A warning, not a block, unlike the never_mention backstop: a 95-word LinkedIn message
        # is a style miss the owner trims while reading it aloud, where a leaked phrase is
        # unrecoverable once sent. Different severity, different handling.
        print(f"WARNING: draft for {slots['company']} is {len(message.split())} words, over "
              f"the {budget}-word budget", flush=True)

    zone = sorted(str(p).strip().lower() for p in (d.get("never_mention") or []) if str(p).strip())
    never_named = sorted(t for t in ctx.never
                         if eh._present(ctx, eh.requirements_zone(low), t)) if desc else []
    risks = risk_bullets(row, verdict, hook,
                         never_named,
                         bool(eh._hit_any(ctx, eh.pad(str(_get(row, "company"))),
                                          ctx.comp_risk)))
    if note:
        risks = (risks + f"\n- Template note: {note}").strip()

    flat = " ".join(f"{message}\n{risks}".lower().split())
    for phrase in zone:
        if " ".join(phrase.split()) in flat:
            raise Blocked(f"draft for {slots['company']} would mention a never_mention phrase; "
                          f"not written")
    return message, risks


def _render(template: str, slots: dict) -> str:
    out = Template(template).safe_substitute(slots)
    # Dropping an optional clause leaves a double space or a dangling blank line behind, and
    # those are the tell that a message was assembled rather than typed.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n[ \t]+", "\n", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _join(items: list[str]) -> str:
    """"A", "A and B", "A, B and C" -- never an Oxford comma, never a bare three-item list."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
