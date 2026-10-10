#!/usr/bin/env python3
"""
Write a per-req tailored resume to tailored/, one Markdown file per job.

    python3 tailor.py --dry-run          # show which reqs would get one
    python3 tailor.py                    # tailor everything above the threshold
    python3 tailor.py --key mongodb::123
    python3 tailor.py --min-score 8

The hard rule, enforced in the prompt and worth stating plainly: this reorders, reweights and
rephrases what is already in config/resume.md. It does not invent. A resume that claims Go
because the JD asked for Go gets you into a room where you cannot answer a single question, and
the recruiter who put you there stops taking your calls. So the model is given the master resume,
a list of things that are true but under-stated, and an explicit "never claim" list — and every
output carries a GAPS section naming what the JD wanted that you genuinely do not have.

Instahyre reqs are skipped by default (`--include-instahyre` overrides). Two reasons: most of
them arrive with no JD text, so there is nothing to tailor against and the result would be
guesswork dressed up as targeting; and applications there go through the Instahyre profile
rather than a per-role resume upload, so a tailored PDF has nowhere to go.

Nothing is sent. This writes files you review, export and attach yourself.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import engine
import jobprofile
import store

ROOT = Path(__file__).resolve().parent
RESUME = ROOT / "config" / "resume.md"
OUT = ROOT / "tailored"
CALL_TIMEOUT = 240
JD_CHARS = 6000          # JDs run long; the tailoring needs more of one than scoring does

RULES = """\
Hard rules. These are not style preferences:

- Use ONLY facts present in the master resume, including its "Notes for the tailoring stage"
  section. You may reorder, reweight, merge, split and rephrase. You may surface a detail the
  master states but the current resume buries.
- NEVER add a language, framework, tool, cloud service, database or methodology the master does
  not evidence. If the JD asks for Go and the master does not have Go, the answer is a GAPS
  entry, not a skills-line addition.
- NEVER change the years of experience, job titles, employers, dates, degree, institution or
  GPA. NEVER invent a metric, and never alter an existing one.
- Reproduce the header exactly as the master states it, including the years-of-experience
  figure, and keep every contact field. Omitting it is not a permitted alternative to changing
  it: whether to show a years figure against a JD asking for more is the candidate's decision,
  not yours, and a tailored resume that silently drops it hands them two documents that differ
  in substance rather than emphasis.
- Honour the master's "Never claim" list absolutely, even if the JD demands the item.
- Keyword alignment means using the JD's vocabulary for work that genuinely happened. If the
  master says "multi-region, multi-account monitoring" and the JD says "observability at scale",
  you may write "observability across 5 AWS regions and multiple accounts". That is the same
  fact in their words. Writing "SRE on-call rotation" would not be.
- Keep every bullet checkable. A reader with the master resume in hand must be able to trace
  each claim back to it.
- Preserve the one-page shape: 4 employers, same order, Amazon keeps the most bullets. Drop the
  weakest bullet rather than let the page overflow if you add emphasis elsewhere.
- Plain prose. No em dashes. No "passionate about", no "proven track record", no three-item
  lists of adjectives.
"""


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:46]


def build_prompt(master: str, profile: dict, job) -> str:
    sit = (profile.get("situation") or "").strip()
    return "\n".join([
        "You are tailoring one engineer's resume to one specific job requisition.",
        "",
        "## The requisition",
        f"Company: {job['company']}",
        f"Role: {job['title']}",
        f"Location: {job['location'] or 'not stated'}",
        f"Scored {job['score']}/10 by an earlier pass — {job['score_reason'] or 'no reason recorded'}",
        "",
        "## Job description",
        (job["description"] or "(no JD text available)")[:JD_CHARS],
        "",
        "## Master resume — the only permitted source of facts",
        master.strip(),
        "",
        "## The candidate's situation, for tone and emphasis only",
        sit or "(none recorded)",
        "",
        RULES,
        "",
        "## Output",
        "Return exactly these four sections, in this order, and nothing else.",
        "",
        "TAILORED RESUME:",
        "<the full resume in the same Markdown shape as the master: header line, Experience with",
        "the same four employers in the same order, Skills, Projects, Education. This is the",
        "artefact that gets exported, so it must be complete rather than a diff.>",
        "",
        "WHAT CHANGED:",
        "<4 to 7 bullets. Each names the change and the JD line that motivated it. Be specific:",
        "'led the Amazon section with the CDK pipeline bullet because the JD opens on IaC' beats",
        "'reordered for relevance'.>",
        "",
        "KEYWORDS ADDED:",
        "<the JD vocabulary now present, and for each one the master-resume fact it describes.",
        "If a keyword could not be used honestly, it belongs in GAPS instead.>",
        "",
        "GAPS:",
        "<what this JD asks for that the master genuinely does not evidence. Be blunt; this is",
        "the section that stops the resume from lying and tells the candidate what to expect to",
        "be asked. If a gap is likely to fail a screen outright, say so.>",
    ])


SECTIONS = ("TAILORED RESUME", "WHAT CHANGED", "KEYWORDS ADDED", "GAPS")

FENCE = re.compile(r"^\s*```[\w-]*\s*$", re.M)


def _unfence(s: str) -> str:
    """
    Drop markdown fence lines from a section body.

    GAPS is matched to end of input, so a reply wrapped in one code fence put its closing fence
    inside GAPS. Stripping fence-only lines from every section fixes that and the mirror case,
    an opening ```markdown at the head of TAILORED RESUME, which would otherwise be the first
    line of the artefact that gets exported.

    Only lines that are nothing but a fence are removed, and from anywhere in the section
    rather than just the ends, because a reply that fences each section individually produces
    them in the middle. A bullet that mentions a fenced snippet inline survives.
    """
    return FENCE.sub("", s).strip()


def split_sections(raw: str) -> dict:
    """Pull the four labelled blocks out, tolerating a stray preamble or markdown fencing."""
    out, order = {}, list(SECTIONS)
    for i, name in enumerate(order):
        nxt = order[i + 1] if i + 1 < len(order) else None
        pat = rf"{name}:\s*(.*?)(?=\n\s*{nxt}:|\Z)" if nxt else rf"{name}:\s*(.*)\Z"
        m = re.search(pat, raw, re.S)
        out[name] = _unfence(m.group(1)) if m else ""
    return out


def write_tailored(job, parts: dict) -> Path:
    OUT.mkdir(exist_ok=True)
    ident = job["key"].split("::")[-1] or job["key"]
    path = OUT / (f"{date.today().isoformat()}-{slug(job['company'])}-"
                  f"{slug(job['title'])}-{slug(ident)}.md")
    path.write_text(f"""# {job['company']} — {job['title']}

- **Score:** {job['score']}/10 — {job['score_reason'] or ''}
- **Location:** {job['location'] or 'not stated'} ({job['geo'] or 'unknown'})
- **Posting:** {job['url'] or 'not stated'}
- **Job key:** `{job['key']}`
- **Source:** {job['source']}

## Before you send
- [ ] Read GAPS first. If a gap would fail the screen outright, skip the application
- [ ] Check every bullet against `config/resume.md`. Anything you cannot defend, delete
- [ ] Export to your LaTeX template; this file is content, not formatting
- [ ] After applying: `python3 mark.py {job['key']} applied`

## Gaps — read this first

{parts['GAPS'] or '_none flagged_'}

## What changed

{parts['WHAT CHANGED'] or '_not reported_'}

## Keywords added

{parts['KEYWORDS ADDED'] or '_none_'}

---

## Tailored resume

{parts['TAILORED RESUME'] or '_generation failed_'}
""")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-score", type=int)
    ap.add_argument("--key", action="append", help="tailor these job keys regardless of score")
    ap.add_argument("--limit", type=int, default=6,
                    help="how many resumes to tailor (default: %(default)s)")
    ap.add_argument("--retailor", action="store_true", help="overwrite an existing file")
    ap.add_argument("--include-instahyre", action="store_true",
                    help="tailor aggregator reqs too, which mostly have no JD text")
    ap.add_argument("--engine", choices=engine.VALID,
                    help="override runtime.engine for this run")
    args = ap.parse_args()

    if not RESUME.exists():
        print(f"no master resume at {RESUME.relative_to(ROOT)} — copy config/resume.example.md "
              "and fill it in")
        return 1
    master = RESUME.read_text()
    profile = jobprofile.load()
    rt = jobprofile.runtime(profile)
    threshold = args.min_score or rt.get("score_threshold", 7)
    try:
        engine_name = engine.resolve(rt, args.engine)
        eng = engine.get(engine_name)
    except engine.BadEngine as e:
        print(e)
        return 1

    with store.connect() as conn:
        if args.key:
            marks = ",".join("?" * len(args.key))
            rows = conn.execute(f"SELECT * FROM jobs WHERE key IN ({marks})", args.key).fetchall()
        else:
            rows = store.promoted(conn, threshold)

        todo, skipped = [], []
        for r in rows:
            if not args.include_instahyre and r["source"] == "instahyre":
                skipped.append((r, "aggregator"))
                continue
            # Tailoring against a title alone produces confident-looking fiction, which is worse
            # than no tailored resume at all.
            if not (r["description"] or "").strip():
                skipped.append((r, "no JD text"))
                continue
            todo.append(r)

        # One tailored resume per req, ever, unless asked again. Matched on the req id in the
        # filename rather than the whole name, so re-running on a later date does not produce a
        # second copy of the same job.
        if not args.retailor and OUT.exists():
            have = [p.name for p in OUT.glob("*.md")]
            todo = [r for r in todo
                    if not any(f"-{slug(r['key'].split('::')[-1])}.md" in n for n in have)]
        todo = todo[: args.limit]

        if skipped:
            print(f"skipping {len(skipped)}:")
            for r, why in skipped[:12]:
                print(f"  {r['score']:>2}  {r['company']:<22} {r['title'][:36]:<36} ({why})")
            print()
        if not todo:
            print(f"nothing to tailor (threshold {threshold})")
            return 0

        print(f"{len(todo)} resume(s) to tailor:")
        for r in todo:
            print(f"  {r['score']:>2}  {r['company']:<22} {r['title'][:40]}")
        if args.dry_run:
            print("\n(dry run — nothing written)")
            return 0

        workers = rt.get("max_parallel_claude", 4)
        try:
            replies = eng.tailor_resumes(
                master, profile, todo,
                announce=f"\ncalling claude for {len(todo)} resume(s), "
                         f"{workers} at a time",
            )
        except engine.EngineUnusable as e:
            print(f"aborted: {e}" if e.mid_stage else f"{engine_name} is not usable: {e}")
            return 1
        except engine.ConfigError as e:
            print(e)
            return 1

        written = 0
        for r, parts in zip(todo, replies):
            print(f"\n{r['company']} — {r['title'][:44]}")
            if isinstance(parts, Exception):
                print(f"  failed: {parts}")
                continue
            if not parts["TAILORED RESUME"]:
                print("  failed: reply had no TAILORED RESUME section")
                continue
            path = write_tailored(r, parts)
            written += 1
            print(f"  -> {path.relative_to(ROOT)}")

        store.log_run(conn, "tailor", detail={"tailored": written})

    print(f"\n{written} tailored resume(s) in tailored/. Read the GAPS section of each before "
          f"you send it;\nnothing here is checked against reality except by you.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
