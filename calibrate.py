#!/usr/bin/env python3
"""
Measure the heuristic scorer against the stored Claude labels. Read-only, always.

The stored `score` column is 806 rows of Claude's judgement, which is the only ground truth
this project has. It is also destructible: `score.py --rescore` would overwrite it, and then
there would be nothing left to calibrate against. So this script opens the DB **read-only**
through a URI connection and refuses to run against the live file unless given `--db`.

What it reports, and why each number is here rather than an adjective:

  * Spearman rho, overall and on the with-JD slice. Rank correlation rather than Pearson,
    because the question is "does it order reqs the way Claude did", not "does it reproduce
    Claude's absolute numbers".
  * The 7+ boundary confusion. The threshold gates the digest AND drafting AND tailoring, so
    how many of Claude's 7+ survive (recall) and how many extra are admitted (precision) is
    the number that decides whether this branch is usable.
  * The score histogram both ways, because a pile-up at one value is invisible in a
    correlation.
  * The overlap contribution of rows whose only matched terms are unevidenced, which is the
    measurement that caught the fourteen free-points terms in the first place.

    python3 calibrate.py --db /tmp/copy.db
    python3 calibrate.py --db /tmp/copy.db --before-high2      # the pre-fix weights
    python3 calibrate.py --db /tmp/copy.db --threshold 6
"""

from __future__ import annotations

import argparse
import collections
import copy
import sqlite3
import sys
from pathlib import Path

import engine_heuristic as eh
import jobprofile

ROOT = Path(__file__).resolve().parent
LIVE_DB = ROOT / "jobhunt.db"

# The fourteen terms the round-3 review measured as having no resume evidence, in the tiers and
# at the weights they carried BEFORE the fix. Kept here so --before-high2 can reproduce the
# pre-fix behaviour exactly instead of the report quoting a number nobody can re-derive.
BEFORE_HIGH2 = {
    "core": {"sqs": 0.4, "s3": 0.4, "ecs": 0.4, "cloudformation": 0.4, "sns": 0.3,
             "high throughput": 0.4, "low latency": 0.4, "ci/cd": 0.2, "restful": 0.3},
    "lane_b": {"rag": 0.5, "langchain": 0.5, "bedrock": 0.5},
    "secondary": {"typescript": 0.15, "scalable": 0.15},
}


def ranks(xs: list[float]) -> list[float]:
    """Average ranks, so ties do not fabricate an ordering the data does not have."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(a: list[float], b: list[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ra, rb = ranks(a), ranks(b)
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * db) if da and db else float("nan")


def confusion(claude: list[int], heur: list[int], t: int) -> dict:
    tp = sum(1 for c, h in zip(claude, heur) if c >= t and h >= t)
    fp = sum(1 for c, h in zip(claude, heur) if c < t and h >= t)
    fn = sum(1 for c, h in zip(claude, heur) if c >= t and h < t)
    tn = sum(1 for c, h in zip(claude, heur) if c < t and h < t)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "claude_at": tp + fn, "heur_at": tp + fp}


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, required=True,
                    help="a COPY of the scored DB. Opened read-only.")
    ap.add_argument("--profile", type=Path, default=jobprofile.PROFILE)
    ap.add_argument("--resume", type=Path, default=ROOT / "config" / "resume.md")
    ap.add_argument("--threshold", type=int, default=7)
    ap.add_argument("--before-high2", action="store_true",
                    help="restore the fourteen unevidenced terms to their pre-fix tiers")
    args = ap.parse_args()

    if args.db.resolve() == LIVE_DB.resolve():
        # 806 rows of labels that cannot be regenerated without spending the owner's Claude
        # access on all of them again.
        sys.exit(f"refusing to read {LIVE_DB}: copy it first. The stored scores are the only "
                 f"ground truth this project has and --rescore would destroy them.")

    profile = jobprofile.load(args.profile)
    master = args.resume.read_text(encoding="utf-8") if args.resume.exists() else None
    if args.before_high2:
        prof = copy.deepcopy(profile)
        sc = prof.setdefault("scoring", {}).setdefault("skills", {})
        for tier, terms in BEFORE_HIGH2.items():
            merged = dict(eh.DEFAULTS["skills"][tier])
            merged.update(terms)
            sc[tier] = merged
        sc["boilerplate"] = None
        profile = prof

    ctx = eh.build_context(profile, master)
    for w in ctx.warnings:
        print(f"WARNING: {w}\n")

    conn = open_ro(args.db)
    rows = conn.execute("SELECT * FROM jobs WHERE score IS NOT NULL").fetchall()
    if not rows:
        sys.exit("no scored rows in that DB")

    claude, heur, verdicts = [], [], []
    with_jd = []
    hist_h, hist_c = collections.Counter(), collections.Counter()
    free_points = []
    for r in rows:
        v = eh.score_one(r, ctx)
        claude.append(int(r["score"]))
        heur.append(v.score)
        verdicts.append((r, v))
        hist_h[v.score] += 1
        hist_c[int(r["score"])] += 1
        if (r["description"] or "").strip():
            with_jd.append((int(r["score"]), v.score))
            low = eh.pad(r["description"])
            hits = eh.matched_skills(ctx, low)
            if hits and not any(t in eh.EVIDENCED_TIERS for _, _, t in hits):
                free_points.append(v.parts.get("overlap", 0.0))

    label = "BEFORE the HIGH-2 fix" if args.before_high2 else "as shipped"
    print(f"=== heuristic vs stored Claude labels, {label} ===")
    print(f"rows: {len(rows)}   with JD: {len(with_jd)}   "
          f"Claude at {args.threshold}+: {sum(1 for c in claude if c >= args.threshold)}")
    print()
    print(f"Spearman rho, all rows   : {spearman(claude, heur):+.3f}")
    if with_jd:
        print(f"Spearman rho, with JD    : "
              f"{spearman([c for c, _ in with_jd], [h for _, h in with_jd]):+.3f}")
    print()

    print("score histogram")
    print("  score   claude   heuristic")
    for s in range(1, 11):
        if hist_c[s] or hist_h[s]:
            print(f"  {s:>5}   {hist_c[s]:>6}   {hist_h[s]:>9}")
    print()

    for t in sorted({args.threshold, 6, 7, 8}):
        c = confusion(claude, heur, t)
        print(f"at {t}+ : claude {c['claude_at']:>3}  heuristic {c['heur_at']:>3}   "
              f"kept {c['tp']:>3}  missed {c['fn']:>3}  extra {c['fp']:>3}   "
              f"precision {c['precision']:.2f}  recall {c['recall']:.2f}")
    print()

    # The decision this branch actually forces on the owner. `score_threshold` gates the digest
    # AND drafting AND tailoring, so the question is not "which cut correlates best" but "which
    # cut promotes about as many reqs as Claude's 7+ did", because that is the volume of
    # generated drafts per run they are used to. Measured against Claude's 7+ as the target set.
    print(f"heuristic cut vs Claude {args.threshold}+ (the set the owner is used to)")
    print("  cut   promotes   of claude's   missed   extra   precision   recall")
    target = [1 if c >= args.threshold else 0 for c in claude]
    for cut in range(5, 10):
        tp = sum(1 for t, h in zip(target, heur) if t and h >= cut)
        fp = sum(1 for t, h in zip(target, heur) if not t and h >= cut)
        fn = sum(1 for t, h in zip(target, heur) if t and h < cut)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        print(f"  {cut:>3}   {tp + fp:>8}   {tp:>11}   {fn:>6}   {fp:>5}   "
              f"{prec:>9.2f}   {rec:>6.2f}")
    print()

    if free_points:
        print(f"rows whose ONLY matched terms are unevidenced: {len(free_points)}   "
              f"mean overlap bonus {sum(free_points) / len(free_points):+.2f}   "
              f"max {max(free_points):+.2f}")
    else:
        print("no row matched unevidenced terms exclusively")

    parts_fired = collections.Counter()
    for _, v in verdicts:
        for k in v.parts:
            parts_fired[k] += 1
    print()
    print("dimensions, rows fired on: " + ", ".join(
        f"{k} {n}" for k, n in sorted(parts_fired.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
