#!/usr/bin/env python3
"""
SQLite store for jobhunt. One file, jobhunt.db, three tables:

    jobs      one row per requisition ever seen. `key` is the dedupe identity.
    outreach  one row per referral ask or application, so nothing is asked twice.
    runs      one row per pipeline run, for "did the cron actually fire" questions.

Everything is idempotent: upsert_job on an already-seen key updates the volatile columns
(still_open, last_seen) and leaves your own annotations (status, notes, score) alone. That
property is what lets run.py be scheduled without any state outside this file.

    python3 store.py             # counts and the current pipeline
    python3 store.py --schema    # print the schema
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jobhunt.db"

# Job lifecycle. Only `new` is machine-assigned; the rest you set as things happen.
STATUSES = [
    "new",          # just fetched, not yet scored
    "scored",       # has a score, awaiting your decision
    "skipped",      # you looked and passed
    "referral_ask", # a referral request is out
    "applied",
    "screen",
    "interview",
    "offer",
    "rejected",
    "closed",       # req taken down
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key          TEXT PRIMARY KEY,   -- stable identity; see job_key()
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT,
    url          TEXT,
    source       TEXT,               -- greenhouse | ashby | workday | seed | email | manual
    req_id       TEXT,
    posted_at    TEXT,               -- ISO date from the ATS, when it gives one
    description  TEXT,               -- JD text, trimmed; feeds the scorer

    lane         TEXT,               -- A (backend/platform) | B (agentic AI)
    geo          TEXT,               -- home | relocate | remote | unclear
    score        INTEGER,            -- 1-10 from score.py
    score_reason TEXT,
    speed_note   TEXT,               -- the scorer's read on time-to-offer

    status       TEXT NOT NULL DEFAULT 'new',
    notes        TEXT,               -- yours, never overwritten by a fetch
    human_path   TEXT,               -- who can refer, or empty for cold

    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    still_open   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

CREATE TABLE IF NOT EXISTS outreach (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key    TEXT REFERENCES jobs(key),
    company    TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- referral | recruiter_reply | application | followup
    contact    TEXT,                 -- person's name or handle
    channel    TEXT,                 -- linkedin | email | portal
    draft_path TEXT,                 -- the file in outbox/ you would send
    sent_at    TEXT,                 -- set by hand once YOU send it; NULL = still a draft
    reply_at   TEXT,
    outcome    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_job ON outreach(job_key);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    stage      TEXT NOT NULL,        -- fetch | score | draft | digest
    fetched    INTEGER DEFAULT 0,
    new_jobs   INTEGER DEFAULT 0,
    scored     INTEGER DEFAULT 0,
    detail     TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def job_key(company: str, title: str, url: str = "", req_id: str = "") -> str:
    """
    Stable identity for a requisition.

    Prefer the ATS req id: it survives a title tweak or a URL change. Fall back to a hash of
    company + normalized title, NOT the URL — the same req often appears under several URLs
    (a LinkedIn redirect, a careers-page path, a direct ATS link), and keying on the URL
    would let the same job into the digest three times.
    """
    if req_id:
        return f"{company.lower().strip()}::{str(req_id).strip()}"
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    digest = hashlib.sha1(f"{company.lower().strip()}|{norm}".encode()).hexdigest()[:12]
    return f"{company.lower().strip()}::{digest}"


# Columns a fetch is allowed to refresh. Anything you write by hand is deliberately absent:
# re-running the pipeline must never clobber your own notes or a status you advanced.
# `source` is also absent — it records where a req was FIRST seen, and letting a later pass
# rewrite it would move rows out from under mark_closed().
FETCH_OWNED = ["title", "location", "url", "req_id", "posted_at", "description"]


def upsert_job(conn: sqlite3.Connection, job: dict) -> bool:
    """
    Insert or refresh one job. Returns True if this is the first time it was seen.

    `job["weak"]` names columns holding guessed values — a title scraped out of a URL slug,
    for instance. Those only fill a column that is still empty; a real value already in the
    row wins. Without this, the same req arriving from both a hand-collected link and its ATS
    board would have its proper title overwritten by the guess.
    """
    key = job.get("key") or job_key(
        job["company"], job["title"], job.get("url", ""), job.get("req_id", "")
    )
    ts = now()
    weak = set(job.get("weak") or ())
    row = conn.execute("SELECT key FROM jobs WHERE key = ?", (key,)).fetchone()

    if row:
        sets, vals = [], []
        for c in FETCH_OWNED:
            v = job.get(c)
            if v in (None, ""):
                continue
            # Weak values defer to whatever is already stored; strong values overwrite.
            sets.append(f"{c} = COALESCE({c}, ?)" if c in weak else f"{c} = ?")
            vals.append(v)
        conn.execute(
            f"UPDATE jobs SET {', '.join(sets + ['last_seen = ?', 'still_open = 1'])} "
            "WHERE key = ?",
            [*vals, ts, key],
        )
        return False

    cols = {
        "key": key,
        "company": job["company"],
        "title": job["title"],
        "location": job.get("location"),
        "url": job.get("url"),
        "source": job.get("source", "manual"),
        "req_id": job.get("req_id"),
        "posted_at": job.get("posted_at"),
        "description": job.get("description"),
        "lane": job.get("lane"),
        "geo": job.get("geo"),
        "status": job.get("status", "new"),
        "notes": job.get("notes"),
        "human_path": job.get("human_path"),
        "first_seen": ts,
        "last_seen": ts,
        "still_open": 1,
    }
    conn.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )
    return True


def set_score(conn, key: str, score: int, reason: str, lane=None, speed_note=None) -> None:
    conn.execute(
        """UPDATE jobs SET score = ?, score_reason = ?,
                  lane = COALESCE(?, lane), speed_note = ?,
                  status = CASE WHEN status = 'new' THEN 'scored' ELSE status END
           WHERE key = ?""",
        (score, reason, lane, speed_note, key),
    )


def mark_closed(conn, source: str, seen_keys: set[str]) -> int:
    """
    Flag reqs from `source` that this run did not see. A req vanishing from a board usually
    means it was filled or pulled, which is itself worth knowing — so still_open goes to 0
    but the row stays, and a status you already advanced is left alone.
    """
    rows = conn.execute(
        "SELECT key FROM jobs WHERE source = ? AND still_open = 1", (source,)
    ).fetchall()
    gone = [r["key"] for r in rows if r["key"] not in seen_keys]
    if gone:
        conn.executemany(
            "UPDATE jobs SET still_open = 0, last_seen = last_seen WHERE key = ?",
            [(k,) for k in gone],
        )
    return len(gone)


def unscored(conn, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND still_open = 1 "
        "ORDER BY first_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()


def promoted(conn, threshold: int, since: str | None = None) -> list[sqlite3.Row]:
    """Scored at or above the threshold and not yet acted on — the digest's main table."""
    sql = (
        "SELECT * FROM jobs WHERE score >= ? AND still_open = 1 "
        "AND status IN ('scored', 'new') "
    )
    args: list = [threshold]
    if since:
        sql += "AND first_seen >= ? "
        args.append(since)
    return conn.execute(sql + "ORDER BY score DESC, company", args).fetchall()


def active(conn) -> list[sqlite3.Row]:
    """Anything already in motion, so the digest can show what needs a nudge."""
    return conn.execute(
        "SELECT * FROM jobs WHERE status IN "
        "('referral_ask', 'applied', 'screen', 'interview', 'offer') "
        "ORDER BY last_seen DESC"
    ).fetchall()


def add_outreach(conn, **kw) -> int:
    kw.setdefault("created_at", now())
    cur = conn.execute(
        f"INSERT INTO outreach ({', '.join(kw)}) VALUES ({', '.join('?' * len(kw))})",
        list(kw.values()),
    )
    return cur.lastrowid


def outreach_exists(conn, job_key_: str, kind: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM outreach WHERE job_key = ? AND kind = ? LIMIT 1",
            (job_key_, kind),
        ).fetchone()
        is not None
    )


def log_run(conn, stage: str, **kw) -> None:
    kw = {k: v for k, v in kw.items() if v is not None}
    if isinstance(kw.get("detail"), (dict, list)):
        kw["detail"] = json.dumps(kw["detail"])
    cols = {"started_at": now(), "stage": stage, **kw}
    conn.execute(
        f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )


def summary(conn) -> str:
    total = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    open_ = conn.execute("SELECT COUNT(*) c FROM jobs WHERE still_open = 1").fetchone()["c"]
    lines = [f"{DB_PATH.name}: {total} jobs, {open_} still open"]

    by_status = conn.execute(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status ORDER BY c DESC"
    ).fetchall()
    if by_status:
        lines.append("\nby status:")
        lines += [f"  {r['status']:<14} {r['c']}" for r in by_status]

    rows = conn.execute(
        "SELECT company, title, score, status, geo FROM jobs WHERE still_open = 1 "
        "ORDER BY score IS NULL, score DESC, company LIMIT 25"
    ).fetchall()
    if rows:
        lines.append("\npipeline:")
        for r in rows:
            s = f"{r['score']:>2}" if r["score"] is not None else " ?"
            lines.append(
                f"  {s}  {r['company']:<16} {(r['title'] or '')[:44]:<44} "
                f"{r['status']:<12} {r['geo'] or ''}"
            )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", action="store_true", help="print the schema and exit")
    args = ap.parse_args()
    if args.schema:
        print(SCHEMA)
        return 0
    with connect() as conn:
        print(summary(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
