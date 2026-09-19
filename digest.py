#!/usr/bin/env python3
"""
Write digest/YYYY-MM-DD.md — the one file you actually read each morning.

Structure, in the order it is useful at 9am:

    1. Do today       promoted reqs with a draft sitting in outbox/, ready to send
    2. New and scored everything that cleared the threshold since the last digest
    3. In motion      referral asks and applications, with an age, so silence is visible
    4. Reqs gone      things that dropped off a board, which is usually "filled"
    5. Below the bar  collapsed count, with the top few, so you can sanity-check the scorer
    6. The read       plain-language state of the search, generated last from the numbers

    python3 digest.py             # write today's digest
    python3 digest.py --stdout    # print it instead
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

import store

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "config" / "profile.yaml"
DIGEST_DIR = ROOT / "digest"


def age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).days
    except ValueError:
        return None


def link(job) -> str:
    title = job["title"] or "untitled"
    return f"[{title}]({job['url']})" if job["url"] else title


def build(conn, profile: dict) -> str:
    rt = profile.get("runtime", {})
    threshold = rt.get("score_threshold", 7)
    today = date.today().isoformat()
    L: list[str] = [f"# jobhunt digest — {today}", ""]

    promoted = store.promoted(conn, threshold)
    drafted = {
        r["job_key"]: r
        for r in conn.execute(
            "SELECT * FROM outreach WHERE sent_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
    }

    # 1. ready to send
    ready = [j for j in promoted if j["key"] in drafted]
    L += ["## Do today", ""]
    if ready:
        L.append("A draft is written and waiting. Read it, fix anything that is not your")
        L.append("voice, send it yourself, then run the `mark.py` line at the bottom of it.")
        L.append("")
        for j in ready:
            d = drafted[j["key"]]
            L += [
                f"- **{j['company']} — {link(j)}** · {j['score']}/10 · {j['geo'] or '?'}",
                f"  - draft: `{d['draft_path']}`",
                f"  - warm path: {j['human_path'] or 'cold, find a human first'}",
            ]
    else:
        L.append("_Nothing drafted and waiting._ Run `python3 draft.py` if the list below")
        L.append("has anything worth an ask.")
    L.append("")

    # 2. new and scored
    fresh = [j for j in promoted if j["key"] not in drafted]
    L += [f"## Scored {threshold}+ and not yet acted on ({len(fresh)})", ""]
    if fresh:
        L.append("| Score | Company | Role | Where | Lane | Time to offer |")
        L.append("|---|---|---|---|---|---|")
        for j in fresh:
            L.append(
                f"| {j['score']} | {j['company']} | {link(j)} | "
                f"{j['geo'] or '?'} | {j['lane'] or '?'} | {j['speed_note'] or ''} |"
            )
        L.append("")
        for j in fresh:
            L.append(f"- **{j['company']} — {j['title']}**: {j['score_reason'] or ''}")
    else:
        L.append("_Nothing new above the bar._")
    L.append("")

    # 3. in motion
    live = store.active(conn)
    L += [f"## In motion ({len(live)})", ""]
    if live:
        L.append("| Company | Role | Stage | Last touch |")
        L.append("|---|---|---|---|")
        for j in live:
            a = age_days(j["last_seen"])
            nudge = " ⚠️ nudge" if a is not None and a >= 7 else ""
            L.append(
                f"| {j['company']} | {link(j)} | {j['status']} | "
                f"{a if a is not None else '?'}d ago{nudge} |"
            )
    else:
        L.append("_Nothing out yet._ The pipeline is empty until a referral ask leaves.")
    L.append("")

    # 4. reqs that disappeared
    gone = conn.execute(
        "SELECT * FROM jobs WHERE still_open = 0 AND status IN ('new','scored') "
        "ORDER BY last_seen DESC LIMIT 10"
    ).fetchall()
    if gone:
        L += [f"## Dropped off a board ({len(gone)})", "",
              "These vanished from the ATS feed, which usually means filled or pulled.", ""]
        for j in gone:
            L.append(f"- {j['company']} — {j['title']} (last seen {age_days(j['last_seen'])}d ago)")
        L.append("")

    # 5. below the bar
    low = conn.execute(
        "SELECT * FROM jobs WHERE score IS NOT NULL AND score < ? AND still_open = 1 "
        "AND status = 'scored' ORDER BY score DESC LIMIT 8",
        (threshold,),
    ).fetchall()
    low_total = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE score IS NOT NULL AND score < ? AND still_open = 1",
        (threshold,),
    ).fetchone()["c"]
    if low_total:
        L += [f"<details><summary>Below the bar ({low_total}) — check the scorer</summary>", ""]
        for j in low:
            L.append(f"- {j['score']}/10 {j['company']} — {j['title']}: {j['score_reason'] or ''}")
        L += ["", "</details>", ""]

    # 6. the read
    unscored = len(store.unscored(conn, 500))
    counts = {
        r["status"]: r["c"]
        for r in conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
    }
    asks_out = counts.get("referral_ask", 0)
    applied = counts.get("applied", 0)
    interviews = counts.get("screen", 0) + counts.get("interview", 0)

    L += ["## The read", ""]
    L.append(
        f"{len(promoted)} req(s) above the bar, {asks_out} referral ask(s) out, "
        f"{applied} application(s) in, {interviews} conversation(s) live."
        + (f" {unscored} still unscored." if unscored else "")
    )
    L.append("")
    if len(promoted) <= 2 and not live:
        L.append(
            "The constraint here is supply, not sourcing. Across every board this project "
            "can read, only a handful of India reqs exist at SDE I-II right now, and the "
            "pipeline is empty. So the leverage is not in scanning harder. It is in the two "
            "warm paths that already exist (the Microsoft recruiter thread and the Google "
            "referral) and in preparation, because when a req does open, the loop is what "
            "decides the outcome."
        )
    elif not live and promoted:
        L.append(
            "Reqs are sitting above the bar with nothing sent. That is the bottleneck today: "
            "a scored req is worth nothing until a human at the company has seen your name."
        )
    elif asks_out and not applied:
        L.append(
            "Asks are out and nothing has converted to an application yet. Give a referral "
            "ask five working days, then apply cold rather than letting the req close."
        )
    else:
        L.append(
            "Pipeline is moving. Keep the prep hours protected; the scoring and drafting are "
            "the parts this project is supposed to take off your hands."
        )
    L.append("")
    L.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} "
             f"{rt.get('timezone', '')}. Nothing in this project sends anything._")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    profile = yaml.safe_load(PROFILE.read_text())
    with store.connect() as conn:
        text = build(conn, profile)
        store.log_run(conn, "digest")

    if args.stdout:
        print(text)
        return 0

    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"{date.today().isoformat()}.md"
    path.write_text(text)
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
