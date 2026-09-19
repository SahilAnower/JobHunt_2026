#!/usr/bin/env python3
"""
Move a job along, or record that you sent something. This is the only write you do by hand.

    python3 mark.py <job-key> applied
    python3 mark.py <job-key> referral_ask --contact "Name" --note "asked on LinkedIn"
    python3 mark.py <job-key> sent            # timestamps the outbox draft as sent
    python3 mark.py --list                    # show keys, since you need one to mark

Statuses: new scored skipped referral_ask applied screen interview offer rejected closed
"""

from __future__ import annotations

import argparse
import sys

import store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?")
    ap.add_argument("status", nargs="?")
    ap.add_argument("--contact")
    ap.add_argument("--note")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    with store.connect() as conn:
        if args.list or not args.key:
            rows = conn.execute(
                "SELECT key, company, title, score, status FROM jobs WHERE still_open = 1 "
                "ORDER BY score IS NULL, score DESC"
            ).fetchall()
            for r in rows:
                s = f"{r['score']:>2}" if r["score"] is not None else " ?"
                print(f"{s}  {r['status']:<12} {r['key']:<32} {r['company']} — {r['title'][:40]}")
            return 0

        row = conn.execute("SELECT * FROM jobs WHERE key = ?", (args.key,)).fetchone()
        if not row:
            # A partial key is easier to type than a full one; accept it if it is unambiguous.
            cands = conn.execute(
                "SELECT * FROM jobs WHERE key LIKE ?", (f"%{args.key}%",)
            ).fetchall()
            if len(cands) != 1:
                print(f"no unique job for '{args.key}' ({len(cands)} matches). Try --list.")
                return 1
            row = cands[0]

        # `sent` is not a status, it is a fact about a draft: stamp the outreach row instead.
        if args.status == "sent":
            n = conn.execute(
                "UPDATE outreach SET sent_at = ? WHERE job_key = ? AND sent_at IS NULL",
                (store.now(), row["key"]),
            ).rowcount
            print(f"marked {n} draft(s) sent for {row['company']} — {row['title']}")
            return 0

        if args.status not in store.STATUSES:
            print(f"status must be one of: {' '.join(store.STATUSES)}")
            return 1

        note = args.note
        conn.execute(
            "UPDATE jobs SET status = ?, notes = COALESCE(?, notes), last_seen = ? WHERE key = ?",
            (args.status, note, store.now(), row["key"]),
        )
        if args.status in ("referral_ask", "applied"):
            kind = "referral" if args.status == "referral_ask" else "application"
            if not store.outreach_exists(conn, row["key"], kind):
                store.add_outreach(
                    conn, job_key=row["key"], company=row["company"], kind=kind,
                    contact=args.contact, sent_at=store.now(),
                )
            else:
                conn.execute(
                    "UPDATE outreach SET sent_at = COALESCE(sent_at, ?), "
                    "contact = COALESCE(?, contact) WHERE job_key = ? AND kind = ?",
                    (store.now(), args.contact, row["key"], kind),
                )
        print(f"{row['company']} — {row['title']} -> {args.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
