#!/usr/bin/env python3
"""
The daily run. Fetch, enrich, score, draft, digest — in that order, each stage tolerating the
previous one having a bad day.

    python3 run.py                # the full daily pass
    python3 run.py --no-draft     # score and digest only, write no outreach
    python3 run.py --dry-run      # show the stages and exit

Cron, once a morning (note the absolute paths; cron has almost no environment):

    0 8 * * *  cd /path/to/jobhunt && /usr/bin/python3 run.py >> logs/run.log 2>&1

Nothing here sends anything. The last thing it does is write a Markdown file.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable or "python3"

STAGES = [
    ("fetch",  [PY, "fetch.py", "--seed", "--enrich"], "poll the ATS boards, reload the seed list, recover JD text"),
    ("score",  [PY, "score.py"],                       "score everything unscored against the profile"),
    ("draft",  [PY, "draft.py"],                       "write referral drafts for anything above the bar"),
    ("digest", [PY, "digest.py"],                      "write today's digest"),
]


def run_stage(name: str, cmd: list[str]) -> tuple[bool, str]:
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())
    ok = proc.returncode == 0
    print(f"--- {name}: {'ok' if ok else f'FAILED ({proc.returncode})'} "
          f"in {time.time() - t0:.0f}s\n")
    return ok, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-draft", action="store_true")
    ap.add_argument("--only", action="append", choices=[s[0] for s in STAGES])
    args = ap.parse_args()

    stages = [s for s in STAGES if not (args.no_draft and s[0] == "draft")]
    if args.only:
        stages = [s for s in stages if s[0] in args.only]

    print(f"jobhunt run — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    if args.dry_run:
        for name, cmd, why in stages:
            print(f"  {name:<8} {' '.join(cmd[1:]):<34} {why}")
        return 0

    # Check the one dependency that fails silently and expensively. An expired Midway session
    # does not make `claude` error cleanly: some calls exit 1, others hang until their timeout,
    # so score and draft used to burn ~25 minutes discovering it one call at a time. fetch
    # needs no credentials, so still run it — a digest built on a stale score beats no digest.
    if any(s[0] in ("score", "draft") for s in stages):
        import claudecall
        ok, msg = claudecall.preflight()
        if not ok:
            print(f"claude is not usable: {msg}\n")
            stages = [s for s in stages if s[0] not in ("score", "draft")]
            print("Skipping score and draft. Running "
                  f"{', '.join(s[0] for s in stages) or 'nothing'} instead; re-run once "
                  "`claude` works to score and draft what today's fetch brought in.\n")

    failed = []
    for name, cmd, _ in stages:
        print(f"=== {name} " + "=" * (70 - len(name)))
        ok, _ = run_stage(name, cmd)
        if not ok:
            failed.append(name)
            # Keep going. A board timing out should not cost you the digest, and the digest is
            # the only part of this you actually read.

    if failed:
        print(f"stages that failed: {', '.join(failed)}")
        print("The digest still reflects whatever made it into the store.")
    print(f"\nRead: digest/{datetime.now().strftime('%Y-%m-%d')}.md")
    return 1 if "digest" in failed else 0


if __name__ == "__main__":
    sys.exit(main())
