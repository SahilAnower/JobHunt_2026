#!/usr/bin/env python3
"""
Export the stored Claude score labels to a file, and put them back into a copy of the DB.

The `jobs.score` column is the only ground truth this project has: every scored row in it was
judged by `engine: claude`, on the owner's subscription, one req at a time. Re-deriving it
means spending that access on the whole corpus again. And it sits one flag away from
destruction — `score.py --rescore` overwrites exactly those cells.

So this script lifts the labels out of the database and into a file that nothing in the
pipeline writes to. Two subcommands, deliberately separate:

    --export   read the live DB **read-only** and write calibration/claude-labels.json
    --apply    write label_score / label_reason into a COPY of a DB from that file

`--export` is the only thing in this repo that opens the live database on purpose, which is
why it opens it through `file:...?mode=ro` and never through `store.connect` (that would run
`executescript(SCHEMA)` and `_migrate` against the live file). `--apply` refuses any path named
`jobhunt.db`, for the same reason `calibrate.py` does.

Why a file and not just a column: a column lives in the file that `--rescore` rewrites, and an
in-DB copy of the labels made *after* a half-finished rescore would copy the heuristic's own
output and call it ground truth. The export happens once, before any rescore, and the
provenance guard below is what keeps a later re-export honest.

    python3 calibration_labels.py --export
    python3 calibration_labels.py --export --db /path/to/jobhunt.db --force
    python3 calibration_labels.py --apply /tmp/cal-2.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import engine_heuristic as eh
import fetch
import jobprofile

ROOT = Path(__file__).resolve().parent
LABELS = ROOT / "calibration" / "claude-labels.json"
RESUME = ROOT / "config" / "resume.md"
RESUME_EXAMPLE = ROOT / "config" / "resume.example.md"

# The name that means "this is somebody's real job hunt", wherever it turns up. `--apply`
# refuses it by filename rather than by resolved path, because the live DB is reachable under
# several paths (a symlink from a worktree, a relative path, ../jobhunt.db) and the filename is
# the one thing all of them share.
LIVE_DB_NAME = "jobhunt.db"


def default_source_db() -> Path:
    """
    Where the live database is, from wherever this script was run.

    Normally it sits next to this file. In a `git worktree` it does not exist at all — the
    worktree has no database — so walk up to the main checkout and use the one there. Without
    this, running `--export` from a worktree would quietly export an empty DB that
    `store.connect` had just created.
    """
    here = ROOT / LIVE_DB_NAME
    if here.exists():
        return here
    for parent in ROOT.parents:
        cand = parent / LIVE_DB_NAME
        if cand.exists():
            return cand
    return here


def open_ro(path: Path) -> sqlite3.Connection:
    """Read-only, and it fails rather than creating a file if the path is wrong."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_master() -> str | None:
    """
    The master resume, for the never-claim list the heuristic treats as a hard negative.

    The provenance guard compares the heuristic's reason string against the stored one, and the
    never-claim clause is part of that string. Scoring with the list off would change the
    reasons and let the guard pass rows it should have caught, so the example resume is used as
    a fallback rather than scoring with no resume at all.
    """
    for path in (RESUME, RESUME_EXAMPLE):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


def labels_digest(rows: list[dict]) -> str:
    """
    sha256 over the labels themselves, stable across re-exports.

    Separate from the file's own sha256 because the file carries an `exported_at` timestamp:
    two exports of an unchanged corpus produce different file hashes but the same digest here,
    which is the one worth recording as "these are the same 918 labels".
    """
    payload = json.dumps(
        [[r["key"], r["score"], r["score_reason"]] for r in sorted(rows, key=lambda r: r["key"])],
        ensure_ascii=True, sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(conn: sqlite3.Connection, ctx: eh.Ctx | None) -> tuple[list[dict], list[str]]:
    """
    Every scored row, as a label, plus the keys the provenance guard rejected.

    The guard: a row is a Claude label only if its stored `score_reason` is not byte-identical
    to what the shipped heuristic produces for that same row. Today that rejects nothing — all
    918 stored reasons are Claude's. It earns its place after item 2: once a rescore has run,
    a re-export of a half-rescored database would otherwise hoover up the heuristic's own
    output and present it as ground truth. `ctx=None` skips the guard, which only a caller that
    cannot build a scoring context should do.
    """
    out, rejected = [], []
    for r in conn.execute("SELECT * FROM jobs WHERE score IS NOT NULL ORDER BY key"):
        reason = (r["score_reason"] or "").strip()
        if ctx is not None and reason and eh.score_one(r, ctx).reason.strip() == reason:
            rejected.append(r["key"])
            continue
        chars = len(r["description"] or "")
        out.append({
            "key": r["key"],
            "score": int(r["score"]),
            "score_reason": reason,
            "has_jd": chars >= fetch.JD_MIN_CHARS,
            "desc_chars": chars,
        })
    return out, rejected


def do_export(args) -> int:
    src, dest = args.db, args.out
    if dest.exists() and not args.force:
        sys.exit(f"{dest} already exists with "
                 f"{len(read_labels(dest)['rows'])} label(s). This file is the ground truth; "
                 f"overwriting it with a re-export of an already-rescored database would "
                 f"replace Claude's labels with the heuristic's own output. Pass --force only "
                 f"if you are certain the source DB still holds the original scores.")
    if not src.exists():
        sys.exit(f"no database at {src}. Pass --db PATH.")

    profile = jobprofile.load(args.profile)
    try:
        ctx = eh.build_context(profile, load_master())
    except eh.ConfigError as e:
        # Without a context there is no provenance guard, and an unguarded export is exactly
        # the failure mode this file exists to prevent. Stop rather than export blind.
        sys.exit(f"cannot build a scoring context, so the provenance guard cannot run: {e}")

    conn = open_ro(src)
    rows, rejected = collect(conn, ctx)
    conn.close()
    if not rows:
        sys.exit(f"no scored rows in {src}; nothing to export")

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_db": str(src.resolve()),
        "labels_digest": labels_digest(rows),
        "rows": rows,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with_jd = sum(1 for r in rows if r["has_jd"])
    print(f"exported {len(rows)} label(s) from {src} to {dest}")
    print(f"  with a JD at export ({fetch.JD_MIN_CHARS}+ chars): {with_jd}  "
          f"blind: {len(rows) - with_jd}")
    print(f"  labels digest : {payload['labels_digest']}")
    print(f"  file sha256   : {file_sha256(dest)}")
    if rejected:
        print(f"  provenance guard rejected {len(rejected)} row(s) whose stored reason is "
              f"byte-identical to the heuristic's: {', '.join(rejected[:5])}"
              + (" ..." if len(rejected) > 5 else ""))
    else:
        print("  provenance guard rejected 0 rows (every stored reason differs from the "
              "heuristic's own output)")
    return 0


def read_labels(path: Path) -> dict:
    """The export file, validated enough that a caller can trust `rows`."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError(f"{path} is not a label export (no 'rows' list)")
    for r in data["rows"]:
        if not isinstance(r, dict) or "key" not in r or "score" not in r:
            raise ValueError(f"{path} has a row with no key or no score: {r!r}")
    return data


def label_map(path: Path) -> dict[str, int]:
    return {r["key"]: int(r["score"]) for r in read_labels(path)["rows"]}


def do_apply(args) -> int:
    dest = args.apply
    if dest.name == LIVE_DB_NAME:
        sys.exit(f"refusing to write to {dest}: that is the live database. Copy it first "
                 f"(cp {dest} /tmp/cal.db) and apply to the copy. The stored scores are the "
                 f"only ground truth this project has.")
    if not dest.exists():
        sys.exit(f"no database at {dest}")
    if not args.out.exists():
        sys.exit(f"no label export at {args.out}. Run:\n"
                 f"    python3 calibration_labels.py --export")

    rows = read_labels(args.out)["rows"]
    import store  # local: only --apply writes, and only ever to a copy

    with store.connect(dest) as conn:
        have = {r["key"] for r in conn.execute("SELECT key FROM jobs")}
        hit = [r for r in rows if r["key"] in have]
        conn.executemany(
            "UPDATE jobs SET label_score = ?, label_reason = ? WHERE key = ?",
            [(r["score"], r["score_reason"], r["key"]) for r in hit],
        )
    print(f"applied {len(hit)} label(s) to {dest}")
    if len(hit) != len(rows):
        print(f"  {len(rows) - len(hit)} label(s) in the export have no row in that DB")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", action="store_true",
                    help="read the source DB read-only and write the label file")
    ap.add_argument("--apply", type=Path, metavar="DB",
                    help="write label_score/label_reason into a COPY of a DB")
    ap.add_argument("--db", type=Path, default=default_source_db(),
                    help="source DB for --export, opened read-only (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=LABELS,
                    help="the label file (default: %(default)s)")
    ap.add_argument("--profile", type=Path, default=jobprofile.PROFILE)
    ap.add_argument("--force", action="store_true",
                    help="allow --export to overwrite an existing label file")
    args = ap.parse_args(argv)

    if args.export and args.apply:
        sys.exit("--export and --apply do different things to different files; run them "
                 "one at a time")
    if args.export:
        return do_export(args)
    if args.apply:
        return do_apply(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
