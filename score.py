#!/usr/bin/env python3
"""
Score unscored jobs 1-10 by shelling out to the `claude` CLI.

Uses your Claude subscription through `claude -p`, so there is no API key anywhere in this
project. Jobs go out in batches (runtime.max_score_batch) because one call judging twelve
reqs against the same profile is both cheaper and more consistent than twelve calls that
cannot see each other.

The prompt is built from config/profile.yaml only — resume, band, geography, comp reality,
and the situation block. Editing the profile changes the scoring; this file holds no opinions
about the candidate at all.

    python3 score.py --dry-run     # print the prompt for the first batch, call nothing
    python3 score.py               # score everything unscored
    python3 score.py --limit 5
    python3 score.py --rescore     # re-score jobs that already have a score
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import claudecall
import jobprofile
import store

ROOT = Path(__file__).resolve().parent
JD_CHARS = 2500          # per req, inside the prompt
# Was 420s, chosen when batches ran one at a time. Batches now run concurrently, so a stuck
# call no longer blocks the others and there is no reason to wait this long for one of them.
CALL_TIMEOUT = 240

RUBRIC = """\
Score 1-10. The scale is calibrated so that 7 is the bar for spending a referral ask on it:

  9-10  Right band, right city or remote-India, strong resume overlap, and a realistic path
        to an offer in weeks rather than months.
  7-8   Worth a referral ask. Real fit with one soft spot (relocation, a stretch on years,
        or a slower loop).
  5-6   Plausible but flawed: adjacent stack, a band that may sit above SDE II, or a company
        whose India comp probably lands under the floor.
  3-4   Weak. Apply only if the pipeline is empty.
  1-2   Out of band, wrong geography, or excluded outright.

Weigh these, in order:
  1. Band fit. Above SDE II is a hard cap. A JD wanting 5+ years is a stretch that needs a
     referral to survive a screen; 8+ is effectively closed. Below-band (new grad, intern)
     is also wrong.
  2. Resume overlap. Java/Spring Boot/AWS/distributed systems is the core. Agentic-AI and
     LLM-platform work is a genuine second lane, evidenced by the multi-agent incident-writing
     agent, not a stretch.
  3. Speed. Time-to-offer is a scoring dimension, not a footnote. A 7 that can close in six
     weeks beats a 9 with a four-month committee loop.
  4. Geography. Hyderabad or remote-India needs no relocation and is worth a point over
     another Indian city.
  5. Comp plausibility at this band in India, against the floor in the profile. Unknown comp
     is never a discard; say it is unknown.
  6. Referral plausibility. The screen is the weak point in this search, so a role where a
     warm introduction is realistic is worth more than a marginally better cold one.
"""


def build_prompt(profile: dict, jobs: list) -> str:
    c = profile["candidate"]
    comp = profile["comp"]
    lines = [
        "You are screening job requisitions for one candidate. Be blunt and specific; a",
        "generous score wastes their time, and they have very little of it.",
        "HARD RULE ON PRONOUNS: the candidate's pronouns are not recorded. Write \"they/them\"",
        "or use the first name, in every single note. Do not write \"he\", \"him\", \"his\",",
        "\"she\" or \"her\" anywhere, not even once, and do not infer a gender from the name.",
        "A wrong guess misgenders a real person; \"they\" never does.",
        "",
        "## Candidate",
        f"{c['name']}, based in {c['base_city']}, {c['country']}.",
        "",
        profile["resume_summary"].strip(),
        "",
        "## Target band and lanes",
        f"Band: {profile['roles']['band']}",
        f"Lane A: {profile['roles']['lane_a']}",
        f"Lane B: {profile['roles']['lane_b']}",
        "",
        "## Compensation reality",
        f"Target {comp['target_lpa']} LPA, hard floor {comp['floor_lpa']} LPA, "
        f"minimum fixed {comp['min_fixed_lpa']} LPA ({comp['currency']}).",
        comp["note"].strip(),
        "Dealbreakers: " + "; ".join(comp["dealbreakers"]),
        "",
        "## Situation",
        profile["situation"].strip(),
        "",
        "## Scoring rubric",
        RUBRIC,
        "## Requisitions",
    ]

    for i, j in enumerate(jobs, 1):
        lines.append(f"\n### [{i}] {j['company']} — {j['title']}")
        lines.append(f"Location field: {j['location'] or '(not given)'}  "
                     f"(classified: {j['geo'] or 'unknown'})")
        if j["url"]:
            lines.append(f"URL: {j['url']}")
        if j["human_path"]:
            lines.append(f"Warm path: {j['human_path']}")
        if j["notes"]:
            lines.append(f"Candidate's own note: {j['notes']}")
        desc = (j["description"] or "").strip()
        lines.append("Description: " + (desc[:JD_CHARS] if desc
                     else "(not available — the board gave no JD text. Score on title, "
                          "company, location and what you know of the company's India org, "
                          "and say the JD was missing.)"))

    lines += [
        "",
        "## Output",
        "Return ONLY a JSON array, no prose before or after, one object per requisition in",
        "the same order, shaped exactly like this:",
        '[{"n": 1, "score": 7, "lane": "A", "reason": "<=40 words, concrete: name the'
        ' specific fit and the specific doubt>", "speed": "<=15 words on realistic'
        ' time-to-offer>"}]',
    ]
    return "\n".join(lines)


def call_claude(prompt: str, claude_bin: str) -> str:
    """Kept as a thin alias so --key style one-offs and any caller still work."""
    return claudecall.call(prompt, claude_bin, CALL_TIMEOUT)


def parse_scores(raw: str, n: int) -> list[dict]:
    """
    Pull the JSON array out of the reply. Models sometimes wrap it in a fence or add a
    sentence, so take the outermost bracket pair rather than trusting the whole of stdout.
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError(f"no JSON array in reply: {raw.strip()[:300]}")
    items = json.loads(m.group(0))
    out = []
    for it in items:
        idx = int(it.get("n", 0))
        if not 1 <= idx <= n:
            continue
        score = max(1, min(10, int(it["score"])))
        out.append(
            {
                "n": idx,
                "score": score,
                "lane": (it.get("lane") or "").strip()[:1].upper() or None,
                "reason": (it.get("reason") or "").strip(),
                "speed": (it.get("speed") or "").strip(),
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the first prompt only")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--batch", type=int)
    args = ap.parse_args()

    profile = jobprofile.load()
    rt = jobprofile.runtime(profile)
    claude_bin = rt.get("claude_bin", "claude")
    batch_size = args.batch or rt.get("max_score_batch", 12)

    with store.connect() as conn:
        if args.rescore:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE still_open = 1 ORDER BY score DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
        else:
            rows = store.unscored(conn, args.limit)

        if not rows:
            print("nothing to score")
            return 0

        batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
        print(f"{len(rows)} job(s) to score in {len(batches)} batch(es) of up to {batch_size}")

        if args.dry_run:
            print("\n" + "=" * 78)
            print(build_prompt(profile, batches[0]))
            print("=" * 78)
            print("\n(dry run — claude not called)")
            return 0

        # One cheap call first. An expired Midway session used to cost a full CALL_TIMEOUT per
        # batch before anyone found out, which is most of how a score stage reached 420s
        # having scored nothing.
        ok, msg = claudecall.preflight(claude_bin)
        if not ok:
            print(f"claude is not usable: {msg}")
            return 1

        # Batches are independent, so run them together rather than one after another. The DB
        # writes stay below, on this thread, because a sqlite3 connection is not thread-safe.
        workers = rt.get("max_parallel_claude", claudecall.DEFAULT_WORKERS)
        print(f"calling claude for {len(batches)} batch(es), {workers} at a time", flush=True)
        try:
            replies = claudecall.gather(
                [build_prompt(profile, b) for b in batches],
                claude_bin, CALL_TIMEOUT, workers,
            )
        except claudecall.AuthExpired as e:
            print(f"aborted: {e}")
            return 1

        total = 0
        for bi, (batch, (raw, err)) in enumerate(zip(batches, replies), 1):
            print(f"\nbatch {bi}/{len(batches)} ({len(batch)} reqs) ...")
            if err is not None:
                if isinstance(err, subprocess.TimeoutExpired):
                    print(f"  timed out after {CALL_TIMEOUT}s; leaving this batch unscored")
                else:
                    print(f"  failed: {err}")
                continue
            try:
                results = parse_scores(raw, len(batch))
            except Exception as e:  # noqa: BLE001
                print(f"  unparseable reply: {e}")
                continue

            for r in results:
                j = batch[r["n"] - 1]
                store.set_score(conn, j["key"], r["score"], r["reason"],
                                lane=r["lane"], speed_note=r["speed"])
                total += 1
                flag = "*" if r["score"] >= rt.get("score_threshold", 7) else " "
                print(f" {flag}{r['score']:>2}  {j['company']:<14} {j['title'][:40]:<40} "
                      f"{textwrap.shorten(r['reason'], 70)}")

        store.log_run(conn, "score", scored=total)

    print(f"\nscored {total} job(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
