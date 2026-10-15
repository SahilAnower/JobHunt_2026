#!/usr/bin/env python3
"""
Score unscored jobs 1-10, through whichever engine the profile selects.

Two engines. `heuristic` (the default) scores with a deterministic, weighted rule set in this
repo: no subprocess, no network, no account. `claude` shells out to `claude -p` on your
subscription, so there is no API key anywhere in this project, and sends jobs in batches
(runtime.max_score_batch) because one call judging twelve reqs against the same profile is both
cheaper and more consistent than twelve calls that cannot see each other.

    runtime:
      engine: heuristic          # or claude. Absent means heuristic.

The prompt and the rule set are both built from config/profile.yaml only — resume, band,
geography, comp reality, and the situation block — plus config/resume.md for the never-claim
list. Editing the profile changes the scoring; this file holds no opinions about the candidate
at all.

    python3 score.py --dry-run     # print what the engine would do, call nothing
    python3 score.py               # score everything unscored
    python3 score.py --limit 5
    python3 score.py --engine claude

Re-scoring is the one destructive thing in this project, and it is gated. `jobs.score` holds
918 rows of claude's judgement that cannot be re-derived without spending the owner's
subscription on the whole corpus again, and a rescore overwrites exactly those cells. So
`--rescore` refuses to start unless the labels are safe twice over: the export from
`calibration_labels.py` must exist and cover every row at risk, and either `--backup PATH`
is given (a consistent SQLite backup, taken before the first write) or `--i-have-a-backup`
is passed to say so out loud.

    python3 calibration_labels.py --export              # once, before any of this
    python3 score.py --rescore --backup ~/jobhunt-pre-rescore.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import calibration_labels
import engine
import jobprofile
import store

ROOT = Path(__file__).resolve().parent
RESUME = ROOT / "config" / "resume.md"
RESUME_EXAMPLE = ROOT / "config" / "resume.example.md"
LABELS = calibration_labels.LABELS
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


def load_master() -> str | None:
    """
    The master resume, for the never-claim list the heuristic scorer treats as a hard negative.

    Resolved here rather than inside the engine for three reasons: it keeps `score_one` pure and
    free of I/O, it lets a test point the scorer at a fixture resume without monkeypatching a
    module constant, and the resume.example.md rung is load-bearing in a worktree that has no
    gitignored resume.md — without it a calibration run would score with the never-claim
    dimension silently off and the resulting numbers would not describe the shipped scorer.
    """
    path = RESUME if RESUME.exists() else (RESUME_EXAMPLE if RESUME_EXAMPLE.exists() else None)
    if path is None:
        print("no master resume; never-claim penalties are off for this run")
        return None
    if path is RESUME_EXAMPLE:
        print("no config/resume.md; using config/resume.example.md for the never-claim list")
    return path.read_text(encoding="utf-8")


def loud(lines: list[str]) -> str:
    """A refusal nobody can scroll past. Returned, so the caller decides how it exits."""
    rule = "=" * 78
    return "\n".join(["", rule, *lines, rule, ""])


def backup_db(conn: sqlite3.Connection, dest: Path) -> None:
    """
    A consistent copy of the open database, taken before anything is overwritten.

    `sqlite3.Connection.backup` rather than `shutil.copy` because the connection is open and
    may hold uncommitted work: a file copy of a live SQLite database can land mid-transaction.
    Refuses a destination inside the repo — `config/profile.yaml.bak` once reached a remote
    carrying a name, email, phone and the whole situation block, which is why `.gitignore`
    carries `config/*.bak` as a backstop. A database backup is strictly worse: it is the whole
    job hunt. Refuses an existing file too, rather than overwriting someone's earlier backup.
    """
    if dest.resolve().is_relative_to(ROOT):
        sys.exit(loud([
            f"REFUSING to back up inside the repository: {dest}",
            "",
            "A copy of the database here is one `git add -A` away from a remote, and it holds",
            "every company, score and note in the job hunt. config/*.bak is in .gitignore for",
            "exactly this reason, after a profile backup reached a remote carrying personal",
            "details. Pick a path outside the repo:",
            "",
            "    --backup ~/jobhunt-pre-rescore.db",
        ]))
    if dest.exists():
        sys.exit(loud([
            f"REFUSING to overwrite an existing backup: {dest}",
            "",
            "That file may be the only copy of an earlier state. Choose another path.",
        ]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(dest) as out:
        conn.backup(out)
    print(f"backed up to {dest} ({dest.stat().st_size} bytes)")


def gate_rescore(conn, db_path: Path, labels_path: Path, backup: Path | None,
                 confirmed: bool, overwriting: int) -> None:
    """
    Refuse to rescore unless the labels survive it. Exits on refusal; returns on approval.

    Two conditions, and both are about the same single fact: the `score` column is the only
    ground truth this project has.

    1. The label export must exist and cover every row at risk. "At risk" is every row with a
       score and no `label_score` behind it — those are the cells a rescore destroys with no
       second copy anywhere. An export smaller than that number is a stale export, taken
       before the corpus grew, and running on it would lose the difference silently.
    2. There must be a backup: `--backup PATH`, which this takes itself and verifies, or
       `--i-have-a-backup`, which puts the claim on the record. The flag is deliberately a
       sentence rather than `--force`, because "force" invites being typed without reading.
    """
    at_risk = store.unprotected_scores(conn)
    if not labels_path.exists():
        sys.exit(loud([
            "REFUSING to rescore: the Claude score labels have not been exported.",
            "",
            f"  database        : {db_path}",
            f"  rows at risk    : {at_risk} (a score with no label_score behind it)",
            f"  expected export : {labels_path}",
            "",
            "Those scores are the only ground truth this project has, and a rescore",
            "overwrites exactly those cells. Export them first — it is read-only and takes",
            "a second:",
            "",
            "    python3 calibration_labels.py --export",
        ]))

    try:
        exported = len(calibration_labels.read_labels(labels_path)["rows"])
    except (ValueError, json.JSONDecodeError) as e:
        sys.exit(loud([
            f"REFUSING to rescore: {labels_path} is not readable as a label export.",
            f"  {e}",
            "",
            "Re-export it before overwriting anything:",
            "",
            "    python3 calibration_labels.py --export --force",
        ]))

    if exported < at_risk:
        sys.exit(loud([
            "REFUSING to rescore: the label export is smaller than the set it has to protect.",
            "",
            f"  rows at risk in {db_path.name:<12} : {at_risk}",
            f"  rows in the export{'':<14} : {exported}",
            f"  unprotected if this ran{'':<9} : {at_risk - exported}",
            "",
            "The export was almost certainly taken before the corpus grew. Re-export, then",
            "run this again:",
            "",
            "    python3 calibration_labels.py --export --force",
        ]))

    print(f"labels: {exported} exported in {labels_path.name}, {at_risk} row(s) at risk")

    if backup is not None:
        backup_db(conn, backup)
    elif not confirmed:
        sys.exit(loud([
            "REFUSING to rescore without a backup.",
            "",
            f"This run would overwrite {overwriting} score(s) in {db_path}, and {at_risk} "
            f"row(s) there",
            "have no label_score behind them.",
            "",
            "Either let this take one:",
            "",
            "    python3 score.py --rescore --backup ~/jobhunt-pre-rescore.db",
            "",
            "or say you already have one:",
            "",
            "    python3 score.py --rescore --i-have-a-backup",
        ]))
    else:
        print("proceeding on --i-have-a-backup; no backup was taken by this run")


def rescore_rows(conn, limit: int | None) -> list:
    """
    Every still-open row, worst-ranked last, or the first `limit` of them.

    `--rescore` used to inherit `--limit`'s default of 100 and silently rescore the top 100 by
    score, which is the worst possible subset: it leaves the database holding two scales at
    once, the top of it on the new one and the rest on the old. No limit unless one is asked
    for.
    """
    sql = "SELECT * FROM jobs WHERE still_open = 1 ORDER BY score DESC"
    if limit is not None:
        return conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print what the engine would do, call nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="how many reqs to score (default: 100, or every still-open req "
                         "with --rescore)")
    ap.add_argument("--rescore", action="store_true",
                    help="re-judge reqs that already have a score. Overwrites them, so it "
                         "needs the label export and a backup; see --backup")
    ap.add_argument("--backup", type=Path, default=None, metavar="PATH",
                    help="with --rescore: take a consistent backup here first. Must be "
                         "outside the repository")
    ap.add_argument("--i-have-a-backup", action="store_true",
                    help="with --rescore: proceed without taking a backup, because one "
                         "already exists")
    ap.add_argument("--labels", type=Path, default=LABELS, metavar="FILE",
                    help="the label export --rescore checks for (default: %(default)s)")
    ap.add_argument("--db", type=Path, default=None,
                    help="score this database instead of jobhunt.db")
    ap.add_argument("--batch", type=int,
                    help="reqs per scoring call; defaults to runtime.max_score_batch (12). "
                         "Ignored by the heuristic engine, which groups by 50.")
    ap.add_argument("--engine", choices=engine.VALID,
                    help="override runtime.engine for this run")
    args = ap.parse_args(argv)

    profile = jobprofile.load()
    rt = jobprofile.runtime(profile)
    batch_size = args.batch or rt.get("max_score_batch", 12)
    try:
        engine_name = engine.resolve(rt, args.engine)
        eng = engine.get(engine_name)
    except engine.BadEngine as e:
        print(e)
        return 1
    master = load_master()

    db_path = args.db or store.DB_PATH
    with store.connect(db_path) as conn:
        if args.rescore:
            rows = rescore_rows(conn, args.limit)
            scored_now = sum(1 for r in rows if r["score"] is not None)
            print(f"--rescore will OVERWRITE {scored_now} existing score(s) "
                  f"of {len(rows)} still-open req(s) in {db_path}")
            if not args.dry_run:
                gate_rescore(conn, db_path, args.labels, args.backup,
                             args.i_have_a_backup, scored_now)
                protected = store.protect_labels(conn)
                print(f"copied {protected} score(s) into label_score/label_reason "
                      f"before overwriting anything")
        else:
            rows = store.unscored(conn, args.limit if args.limit is not None else 100)

        if not rows:
            print("nothing to score")
            return 0

        # The engine owns the grouping, because the claude engine batches and the heuristic one
        # does not. `plan` hands the units back so this stage keeps owning both strings that
        # depend on them: the header below and the --dry-run preview.
        batches = eng.plan(rows, batch_size=batch_size)
        print(f"{len(rows)} job(s) to score in {len(batches)} batch(es) of up to "
              f"{batch_size if engine_name != 'heuristic' else len(batches[0])}")

        if args.dry_run:
            print("\n" + "=" * 78)
            print(eng.dry_run_preview(profile, batches[0], master=master))
            print("=" * 78)
            print(f"\n(dry run — {engine_name} not called)")
            return 0

        total = 0
        threshold = rt.get("score_threshold", 7)

        def on_batch(bi, nbatches, nreqs, results, error):
            # This callback owns every print and every DB write. The stage deliberately does
            # not also iterate score_reqs' return value: doing both writes every row twice and
            # doubles the count `scored N job(s)` reports.
            nonlocal total
            print(f"\nbatch {bi}/{nbatches} ({nreqs} reqs) ...")
            if error is not None and not results:
                if isinstance(error, subprocess.TimeoutExpired):
                    print(f"  timed out after {CALL_TIMEOUT}s; leaving this batch unscored")
                elif isinstance(error, engine.UnparseableReply):
                    print(f"  unparseable reply: {error}")
                else:
                    print(f"  failed: {error}")
                return
            if error is not None:
                print(f"  partial failure: {error}")
            for r in results or []:
                j = rows[r["n"] - 1]
                store.set_score(conn, j["key"], r["score"], r["reason"],
                                lane=r["lane"], speed_note=r["speed"],
                                scored_by=engine_name)
                total += 1
                flag = "*" if r["score"] >= threshold else " "
                print(f" {flag}{r['score']:>2}  {j['company']:<14} {j['title'][:40]:<40} "
                      f"{textwrap.shorten(r['reason'], 70)}")

        # The claude engine makes one cheap call first, then runs the batches together. The
        # cheap call matters: an expired Midway session used to cost a full CALL_TIMEOUT per
        # batch before anyone found out, which is most of how a score stage reached 420s having
        # scored nothing. Batches are independent so they parallelise; the DB writes stay in
        # the callback, on this thread, because a sqlite3 connection is not thread-safe.
        workers = rt.get("max_parallel_claude", 4)
        try:
            eng.score_reqs(
                profile, rows, master=master, batch_size=batch_size,
                announce=f"calling claude for {len(batches)} batch(es), "
                         f"{workers} at a time",
                on_batch=on_batch,
            )
        except engine.EngineUnusable as e:
            print(f"aborted: {e}" if e.mid_stage else f"{engine_name} is not usable: {e}")
            return 1
        except engine.ConfigError as e:
            print(e)
            return 1

        store.log_run(conn, "score", scored=total)

    print(f"\nscored {total} job(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
