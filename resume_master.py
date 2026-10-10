#!/usr/bin/env python3
"""
Parse config/resume.md, then reorder it against one JD. Nothing here invents anything.

**This stage reorders and annotates. It does not rephrase.** That is a deliberate narrowing
against the Claude engine, which was allowed to restate a bullet in the JD's vocabulary.
A script cannot do that safely: rephrasing requires knowing that two phrasings describe the
same fact, and a token-rewriting rule would eventually produce a bullet the owner cannot defend
in a room, which is the exact failure tailor.py exists to prevent.

So the output is the master resume's claims, rewrapped and reordered, plus three generated
sections that are plainly the tool's commentary rather than resume content.

The claim check, and its scope, because the scope is the whole difference between a tool that
works and one that refuses every row: **it applies to the TAILORED RESUME section only.**
GAPS, WHAT CHANGED and KEYWORDS ADDED are commentary and are exempt. That is not a loophole --
it is what makes GAPS possible at all. GAPS exists to name a JD term the resume cannot claim
("Go: required by the JD..."), so a check that refused any line containing a never-claim term
would refuse the one section whose job is to name them. Never-claim terms still cannot reach the
resume body, and structurally rather than by assertion: MASTER_CANON is built from the resume
body only, so a line quoting one is in neither the canon nor the literal set.
"""

from __future__ import annotations

import dataclasses
import re

import engine

SKILLS_LINE = re.compile(r"- \*\*(?P<cat>[^:]+):\*\*\s*(?P<items>.+)$")
HEADER_LINE = re.compile(r"- \*\*(?P<key>[^:]+):\*\*\s*(?P<val>.*)$")
GAP_TERM_CAP = 12

# Lines the assembler is allowed to emit that are not master claims: the structural scaffolding.
TEMPLATE_LITERALS = frozenset({""})


@dataclasses.dataclass(frozen=True)
class Job:
    employer: str
    title: str
    meta: str
    bullets: tuple


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    meta: str
    bullets: tuple


@dataclasses.dataclass(frozen=True)
class Master:
    header: tuple                 # verbatim lines of ## Header
    jobs: tuple                   # Job, in master order
    skills: tuple                 # (category, (items,)) in master order
    projects: tuple               # Project
    education: tuple              # verbatim lines
    notes: tuple                  # "Notes for the tailoring stage" bullets
    never_claim: tuple            # the ### Never claim bullets, verbatim
    canon: frozenset              # the claim set of the resume BODY only
    yoe_line: str                 # the Experience: line, quoted in WHAT CHANGED


class MasterShapeError(engine.ConfigError):
    """
    config/resume.md is not the shape the tailorer needs.

    Fatal for the stage rather than degraded, and an engine.ConfigError so tailor.py's existing
    handler prints it and exits 1. Degrading would produce a resume missing a section and
    write_tailored would happily write it; a malformed master is a configuration error to fix,
    not a condition to work around.
    """


def _canon(line: str) -> str:
    """
    A claim, independent of the three transformations this stage is allowed to make:
    whitespace rewrapping, skills-item permutation, and line reordering.

    Identity cannot be line identity. Every Amazon bullet in the live resume wraps across four
    or five source lines, so rejoining one changes the bytes even with zero reordering, and
    reordering items inside `- **Languages:** Java, JavaScript, C++` rewrites that line while
    adding no claim. A raw-line check fired on essentially every row and wrote nothing, which
    is worse than the bug it guards against.
    """
    s = " ".join(line.split())
    m = SKILLS_LINE.match(s)
    if m:
        items = sorted(i.strip() for i in m.group("items").split(","))
        return f"- **{m.group('cat')}:** " + ", ".join(items)
    return s


def _bullets(lines: list[str]) -> list[str]:
    """
    A bullet is `- ` plus every following line that is indented and does not start a new one.

    Not an edge case: the Amazon bullets wrap across four and five source lines each. Rejoined
    with a single space and deliberately not re-wrapped to the master's column breaks, because
    re-wrapping is a cosmetic rule with no correctness value and one more way to differ.
    """
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("- "):
            out.append(stripped[2:].strip())
        elif stripped and out and (raw.startswith(" ") or raw.startswith("\t")):
            out[-1] = f"{out[-1]} {stripped}"
    return out


def _split_sections(block: str, level: str) -> list[tuple[str, str]]:
    """[(heading text, body)] for every `## ` or `### ` heading in a block."""
    pat = re.compile(rf"(?m)^{level} (.+)$")
    hits = list(pat.finditer(block))
    out = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(block)
        out.append((m.group(1).strip(), block[m.end():end]))
    return out


def parse_master(text: str) -> Master:
    """
    The master resume as data.

    Everything after the second `---` (the notes and the never-claim list) is parsed but never
    emitted into a resume body. Emitting either would be a disclosure bug, and keeping them out
    of `canon` is what makes "a never-claim term cannot be emitted into the resume" structural.
    """
    chunks = re.split(r"(?m)^---\s*$", text)
    if len(chunks) < 2:
        raise MasterShapeError(
            "config/resume.md does not have the expected shape: no `---` separator. The "
            "tailorer needs a Header block and an Experience block with ### Employer sections.")
    body = chunks[1]
    tail = "\n".join(chunks[2:]) if len(chunks) > 2 else ""

    sections = dict(_split_sections(body, "##"))
    missing = [k for k in ("Header", "Experience") if k not in sections]
    if missing:
        raise MasterShapeError(
            f"config/resume.md does not have the expected shape: no "
            f"{' and no '.join('## ' + m for m in missing)}. The tailorer needs a Header block "
            f"and an Experience block with ### Employer sections.")

    header = tuple(l.rstrip() for l in sections["Header"].splitlines() if l.strip())
    if not any(HEADER_LINE.match(l.strip()) for l in header):
        raise MasterShapeError("config/resume.md does not have the expected shape: "
                               "## Header has no `- **Key:** value` lines.")
    yoe_line = next((l.strip() for l in header
                     if l.strip().lower().startswith("- **experience:")), "")

    jobs = []
    for heading, blk in _split_sections(sections["Experience"], "###"):
        employer, _, title = heading.partition("—")
        lines = blk.splitlines()
        meta = next((l.strip() for l in lines if l.strip() and not l.strip().startswith("- ")),
                    "")
        jobs.append(Job(employer.strip(), title.strip(), meta, tuple(_bullets(lines))))
    if not jobs:
        raise MasterShapeError("config/resume.md does not have the expected shape: "
                               "## Experience has no ### Employer sections.")

    skills = []
    for line in sections.get("Skills", "").splitlines():
        m = SKILLS_LINE.match(" ".join(line.split()))
        if m:
            skills.append((m.group("cat").strip(),
                           tuple(i.strip() for i in m.group("items").split(",") if i.strip())))

    projects = []
    for heading, blk in _split_sections(sections.get("Projects", ""), "###"):
        name, _, meta = heading.partition("—")
        projects.append(Project(name.strip(), meta.strip(), tuple(_bullets(blk.splitlines()))))

    education = tuple(l.rstrip() for l in sections.get("Education", "").splitlines()
                      if l.strip())

    notes, never = [], []
    if tail:
        for heading, blk in _split_sections(tail, "###"):
            if heading.strip().lower() == "never claim":
                never = [b for b in _bullets(blk.splitlines())]
        before = re.split(r"(?m)^### ", tail)[0]
        notes = _bullets(before.splitlines())

    canon = set()
    for line in _emit_lines(header, jobs, skills, projects, education):
        if line.strip():
            canon.add(_canon(line))
    return Master(header=header, jobs=tuple(jobs), skills=tuple(skills),
                  projects=tuple(projects), education=education, notes=tuple(notes),
                  never_claim=tuple(never), canon=frozenset(canon), yoe_line=yoe_line)


def _emit_lines(header, jobs, skills, projects, education) -> list[str]:
    """The resume body, assembled. One function so `canon` and the output cannot diverge."""
    out: list[str] = ["## Header", ""]
    out += list(header)
    out += ["", "## Experience", ""]
    for j in jobs:
        out.append(f"### {j.employer} — {j.title}" if j.title else f"### {j.employer}")
        if j.meta:
            out.append(j.meta)
        out.append("")
        out += [f"- {b}" for b in j.bullets]
        out.append("")
    if skills:
        out += ["## Skills", ""]
        out += [f"- **{cat}:** " + ", ".join(items) for cat, items in skills]
        out.append("")
    if projects:
        out += ["## Projects", ""]
        for p in projects:
            out.append(f"### {p.name} — {p.meta}" if p.meta else f"### {p.name}")
            out += [f"- {b}" for b in p.bullets]
            out.append("")
    if education:
        out += ["## Education", ""]
        out += list(education)
    return out


# --------------------------------------------------------------------------- reordering

def relevance(text: str, jd_low: str, ctx) -> tuple[float, list[str]]:
    """
    How strongly one bullet answers this JD, and which terms did it.

    Only terms present on BOTH sides count, so a bullet is promoted for matching *this* JD
    rather than for being generally keyword-dense. Both sides go through the same lowercasing
    and the same whole-word matcher, so `go` in a bullet cannot match `mongo` in a JD.
    """
    import engine_heuristic as eh

    low = eh.pad(text)
    total, named = 0.0, []
    for term in sorted(ctx.skills):
        spells = ctx.spellings[term]
        if any(eh._present(ctx, low, s) for s in spells) and \
           any(eh._present(ctx, jd_low, s) for s in spells):
            total += ctx.skills[term]
            named.append(term)
    return total, named


def tailor_one(row, doc: Master, ctx) -> dict:
    """
    The four sections for one req.

    Sorting is stable, so a JD that matches nothing leaves every claim in master order, and
    WHAT CHANGED says so rather than inventing a change list.
    """
    import engine_heuristic as eh

    jd = str(_get(row, "description") or "")
    jd_low = eh.pad(jd)
    zone = eh.requirements_zone(jd_low)
    changed: list[str] = []

    # 1. Bullets within each employer. Employers stay in master order: chronological order on a
    #    resume is not a stylistic choice.
    new_jobs = []
    for job in doc.jobs:
        scored = [(relevance(b, jd_low, ctx), b) for b in job.bullets]
        order = sorted(range(len(scored)), key=lambda i: -scored[i][0][0])
        if order and order[0] != 0 and scored[order[0]][0][0] > 0:
            terms = scored[order[0]][0][1][:2]
            changed.append(f"Led the {job.employer} section with the bullet matching "
                           f"{', '.join(terms)}; the JD names them.")
        new_jobs.append(dataclasses.replace(
            job, bullets=tuple(scored[i][1] for i in order)))

    # 3. Skill items within a category. Categories themselves stay in master order: a Skills
    #    block that starts with Databases because the JD mentioned Postgres reads as generated.
    new_skills = []
    for cat, items in doc.skills:
        scored = [(relevance(it, jd_low, ctx)[0], it) for it in items]
        order = sorted(range(len(scored)), key=lambda i: -scored[i][0])
        if order and order[0] != 0 and scored[order[0]][0] > 0:
            changed.append(f"Promoted {scored[order[0]][1]} to the front of the {cat} line.")
        new_skills.append((cat, tuple(scored[i][1] for i in order)))

    # 4. Projects.
    proj_scored = [(relevance(" ".join([p.name, p.meta, *p.bullets]), jd_low, ctx)[0], p)
                   for p in doc.projects]
    proj_order = sorted(range(len(proj_scored)), key=lambda i: -proj_scored[i][0])
    if proj_order and proj_order[0] != 0 and proj_scored[proj_order[0]][0] > 0:
        changed.append(f"Led Projects with {proj_scored[proj_order[0]][1].name}, which the JD "
                       f"matches more closely.")
    new_projects = tuple(proj_scored[i][1] for i in proj_order)

    lines = _emit_lines(doc.header, new_jobs, new_skills, new_projects, doc.education)
    illegal = next((l for l in lines
                    if l.strip() and _canon(l) not in doc.canon
                    and l not in TEMPLATE_LITERALS), None)
    if illegal:
        # A runtime assertion of the invariant, kept in production rather than test-only. It is
        # one _canon call and one set lookup per line, and the thing it guards against is a
        # resume claiming something the owner cannot defend. Firing means a bug in the
        # reorderer, not bad input, so the message quotes the line.
        raise ClaimLeak(f"tailored output for {_get(row, 'company')} contains a claim not "
                        f"present in the master: \"{illegal.strip()[:80]}\"; not written")

    if not changed:
        changed.append("No reordering was warranted; the JD did not match more strongly "
                       "against any one bullet than the master order already reflects.")
    changed = changed[:6]
    if doc.yoe_line:
        figure = doc.yoe_line.split("**", 2)[-1].lstrip(":* ").strip()
        changed.append(f"Left the employer order, the dates and the {figure} figure exactly as "
                       f"the master states them.")
    else:
        changed.append("Left the employer order and the dates exactly as the master states "
                       "them.")

    return {
        "TAILORED RESUME": "\n".join(lines).strip(),
        "WHAT CHANGED": "\n".join(f"- {c}" for c in changed),
        "KEYWORDS ADDED": _keywords(doc, jd_low, ctx),
        "GAPS": _gaps(row, doc, jd_low, zone, ctx),
    }


class ClaimLeak(RuntimeError):
    """The assembled resume body carries a claim the master does not. Fatal for that row."""


def _keywords(doc: Master, jd_low: str, ctx) -> str:
    """
    A misnomer inherited from the section contract, and the content says so. Nothing is added.

    Each line pairs a JD term with the master fact that already evidences it, which is what the
    section is actually for in a screen.
    """
    import engine_heuristic as eh

    evidence = [("header", l) for l in doc.header]
    for j in doc.jobs:
        evidence += [(j.employer, b) for b in j.bullets]
    for cat, items in doc.skills:
        evidence.append((cat, ", ".join(items)))
    for p in doc.projects:
        evidence += [(p.name, " ".join([p.meta, *p.bullets]))]
    evidence += [("notes", n) for n in doc.notes]

    out = []
    for term, _, tier in eh.matched_skills(ctx, jd_low):
        if tier not in eh.EVIDENCED_TIERS:
            continue
        spells = ctx.spellings[term]
        src = next((text for _, text in evidence
                    if any(eh._present(ctx, eh.pad(text), s) for s in spells)), None)
        if src:
            out.append(f'- {term}: "{_trim(src, 90)}", already on the resume.')
        if len(out) >= GAP_TERM_CAP:
            break
    if not out:
        return ("No JD vocabulary matched the master resume. That is itself the finding; "
                "read GAPS.")
    return ("Nothing was added. These JD terms are already evidenced by the master resume:\n"
            + "\n".join(out))


def _gaps(row, doc: Master, jd_low: str, zone: str, ctx) -> str:
    """
    What this JD asks for that the master does not evidence. The section that stops the
    document lying, and the one the owner is told to read first.
    """
    import engine_heuristic as eh

    out: list[str] = []

    # 1. Never-claim demands. This is the section that is allowed to name them; the resume body
    #    structurally cannot.
    for term in sorted(t for t in ctx.never if eh._present(ctx, zone, t)):
        out.append(f'- {term}: required by the JD, and the master resume\'s "Never claim" list '
                   f'rules it out. Expect this to fail a screen that filters on it.')

    # 2. Years of experience.
    yoe = eh.parse_yoe(jd_low)
    my = float(ctx.cfg["my_yoe"])
    if yoe is not None and yoe > my:
        out.append(f"- The JD asks for {yoe:g}+ years against {my:g} on the resume. The figure "
                   f"is unchanged, by design.")

    # 3. Vocabulary named in the JD with no master evidence.
    body = " ".join([*doc.header, *(b for j in doc.jobs for b in j.bullets),
                     *(", ".join(i) for _, i in doc.skills),
                     *(" ".join([p.name, p.meta, *p.bullets]) for p in doc.projects),
                     *doc.notes])
    body_low = eh.pad(body)
    unmatched = []
    for term, weight, tier in eh.matched_skills(ctx, jd_low):
        if any(eh._present(ctx, body_low, s) for s in ctx.spellings[term]):
            continue
        mark = " (required)" if eh._present(ctx, zone, term) else ""
        unmatched.append((weight, f"{term}{mark}"))
    for term, weight in sorted((ctx.cfg.get("off_lane_titles") or {}).items()):
        if eh._present(ctx, jd_low, term) and not eh._present(ctx, body_low, term):
            unmatched.append((float(weight) * 0.5, f"{term} (off lane)"))
    if unmatched:
        named = [t for _, t in sorted(unmatched, key=lambda x: (-x[0], x[1]))][:GAP_TERM_CAP]
        out.append("- Named in the JD, not evidenced on the resume: " + ", ".join(named) + ".")

    if not out:
        return "Nothing in the JD is unevidenced by the master resume."
    return "\n".join(out)


def _trim(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _get(row, key, default=""):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v
