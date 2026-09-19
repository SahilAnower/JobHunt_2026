#!/usr/bin/env python3
"""
Write referral-ask and application-note drafts to outbox/, one Markdown file per job.

NOTHING IS SENT. This script only writes files. Every draft carries a checklist you tick
before copying it into LinkedIn or an email yourself. That is deliberate and not a limitation
to be engineered away: automated LinkedIn messaging gets accounts restricted, and a referral
ask that reads as generated costs you the contact, not just the referral.

Drafts are produced by `claude -p` from the profile, so they carry the actual resume facts
instead of adjectives. One file per job, named outbox/<date>-<company>-<slug>.md.

    python3 draft.py --dry-run            # show which jobs would get a draft
    python3 draft.py                      # draft everything at or above the threshold
    python3 draft.py --key cloudflare::123
    python3 draft.py --min-score 8
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

import store

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "config" / "profile.yaml"
OUTBOX = ROOT / "outbox"
CALL_TIMEOUT = 300

# The voice rules matter more than anything else in this file. A referral ask that pattern
# matches to generated text is worse than no message: it burns a contact you cannot re-ask.
VOICE = """\
Voice rules, all of them hard requirements:
- No em dashes anywhere. Use a comma, a period, or parentheses.
- No "Honestly", "To be honest", "I hope this finds you well", "I wanted to reach out".
- No "excited to connect", no "passionate about", no closing slogan. End on a plain line.
- Do not use three-item lists or balanced two-part sentences. Vary the sentence length and
  let one sentence run short.
- Contractions are fine and wanted. This should read like a competent engineer typing quickly,
  not like a cover letter.
- One concrete, checkable detail about the role or the team. If the JD gives you nothing
  specific, say something true about the work instead of inventing a detail.
- Exactly one specific thing from the resume, the one that actually maps to this req. Not a
  summary of the career.
- Never raise anything listed under "Do not raise" below, and never hint at it. If a reason
  for leaving is needed, use the wording given there and nothing else.
- Under 90 words for a LinkedIn referral ask. Under 130 for an email.
- Sign off with the short name only.
- The sender's pronouns are not recorded. The message is in first person so this rarely
  comes up, but never write a gendered pronoun about the sender in the RISKS section.
"""


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")[:48]


def build_prompt(profile: dict, job, kind: str) -> str:
    c = profile["candidate"]
    d = profile.get("drafting", {}) or {}
    channel = "LinkedIn message" if kind == "referral" else "short email"
    who = job["human_path"] or "a second-degree connection who works there, name unknown"
    return "\n".join(
        [
            f"Write a {channel} asking for a referral, as {c['name']} would type it.",
            "",
            "## The requisition",
            f"Company: {job['company']}",
            f"Role: {job['title']}",
            f"Location: {job['location'] or 'not stated'}",
            f"URL: {job['url'] or 'not stated'}",
            f"Who this goes to: {who}",
            "",
            "## Job description (may be partial or missing)",
            (job["description"] or "(no JD text available)")[:2500],
            "",
            "## The sender",
            profile["resume_summary"].strip(),
            "",
            f"Reachable at {c['email']}, {c['phone']}. LinkedIn: {c['links']['linkedin']}.",
            "",
            "## Why this req scored well",
            f"{job['score']}/10 — {job['score_reason'] or 'no reason recorded'}",
            "",
            VOICE,
            "",
            # Kept in the profile rather than here, because it is the most personal thing the
            # drafter knows and this file is version-controlled.
            "## Do not raise",
            *(f"- {x}" for x in d.get("never_mention") or ["(nothing specified)"]),
            f"Reason for leaving, the only version to use: "
            f"{d.get('reason_for_leaving') or 'growth'}",
            "",
            "## Output",
            "Return exactly two sections and nothing else:",
            "",
            "MESSAGE:",
            "<the message, ready to paste>",
            "",
            "RISKS:",
            "<2 or 3 bullets: what a reader might push back on, and anything in the message",
            "you could not verify from the JD and should check before sending>",
        ]
    )


def call_claude(prompt: str, claude_bin: str) -> str:
    proc = subprocess.run(
        [claude_bin, "-p", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=CALL_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def split_sections(raw: str) -> tuple[str, str]:
    m = re.search(r"MESSAGE:\s*(.*?)(?:\n\s*RISKS:\s*(.*))?$", raw, re.S)
    if not m:
        return raw, ""
    return (m.group(1) or "").strip(), (m.group(2) or "").strip()


def write_draft(job, message: str, risks: str, kind: str) -> Path:
    OUTBOX.mkdir(exist_ok=True)
    path = OUTBOX / f"{date.today().isoformat()}-{slug(job['company'])}-{slug(job['title'])}.md"
    channel = "LinkedIn" if kind == "referral" else "Email"
    path.write_text(
        f"""# {job['company']} — {job['title']}

- **Score:** {job['score']}/10 — {job['score_reason'] or ''}
- **Speed:** {job['speed_note'] or 'not assessed'}
- **Location:** {job['location'] or 'not stated'} ({job['geo'] or 'unknown'})
- **Posting:** {job['url'] or 'not stated'}
- **Warm path:** {job['human_path'] or 'cold, no human attached yet'}
- **Channel:** {channel}
- **Job key:** `{job['key']}`

## Before you send
- [ ] Open the posting and confirm the req is still live and in band
- [ ] Confirm the years-of-experience ask is not 8+
- [ ] Put the recipient's actual name in the greeting
- [ ] Read it aloud once. If a line does not sound like you, rewrite that line
- [ ] After sending, record it: `python3 mark.py {job['key']} referral_ask`

## Message

{message}

## Risks and things to verify

{risks or '_none flagged_'}
"""
    )
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-score", type=int)
    ap.add_argument("--key", action="append", help="draft these job keys regardless of score")
    ap.add_argument("--kind", default="referral", choices=["referral", "email"])
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--redraft", action="store_true", help="draft again even if one exists")
    args = ap.parse_args()

    profile = yaml.safe_load(PROFILE.read_text())
    rt = profile.get("runtime", {})
    threshold = args.min_score or rt.get("score_threshold", 7)
    claude_bin = rt.get("claude_bin", "claude")

    with store.connect() as conn:
        if args.key:
            marks = ",".join("?" * len(args.key))
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE key IN ({marks})", args.key
            ).fetchall()
        else:
            rows = store.promoted(conn, threshold)

        # One referral ask per job, ever. Re-asking the same contact about the same req is
        # the fastest way to become someone's ignored message.
        todo = [
            r for r in rows
            if args.redraft or not store.outreach_exists(conn, r["key"], args.kind)
        ][: args.limit]

        if not todo:
            print(f"nothing to draft (threshold {threshold}, all already drafted)")
            return 0

        print(f"{len(todo)} draft(s) to write:")
        for r in todo:
            print(f"  {r['score']:>2}  {r['company']:<14} {r['title'][:46]}")

        if args.dry_run:
            print("\n(dry run — nothing written)")
            return 0

        for r in todo:
            print(f"\ndrafting {r['company']} — {r['title'][:44]} ...", flush=True)
            try:
                raw = call_claude(build_prompt(profile, r, args.kind), claude_bin)
            except Exception as e:  # noqa: BLE001
                print(f"  failed: {e}")
                continue
            message, risks = split_sections(raw)
            path = write_draft(r, message, risks, args.kind)
            store.add_outreach(
                conn, job_key=r["key"], company=r["company"], kind=args.kind,
                contact=r["human_path"], channel="linkedin" if args.kind == "referral" else "email",
                draft_path=str(path.relative_to(ROOT)),
            )
            print(f"  -> {path.relative_to(ROOT)}")

        store.log_run(conn, "draft", detail={"drafted": len(todo)})

    print("\nDrafts are drafts. Nothing was sent; read each one and send it yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
