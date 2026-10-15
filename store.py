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
import collections
import hashlib
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jobhunt.db"

# How much description text counts as "this req has a usable JD". The single source of that
# rule: `fetch.JD_MIN_CHARS` is assigned from this, so enrichment's candidate query, the label
# export's `has_jd` flag and the digest's confidence marker all read the same number.
#
# 700 is where a JavaScript-shell careers page stops being distinguishable from a real posting
# — a shell comes back as a few hundred characters of nav and a cookie banner. It lives here
# rather than in fetch.py because `store` is the module everything else already imports, and
# `fetch` imports `store` (never the reverse), so this is the only direction that does not
# create a cycle.
#
# Note the scorer does NOT use it: `engine_heuristic.score_one` treats any non-empty
# description as a JD. The two rules disagree on rows holding 1-699 characters, which the
# digest marks as blind while the scorer scored them as sighted. Changing the scorer's split is
# a separate piece of work; see the known gaps note in CALIBRATION.md.
JD_MIN_CHARS = 700

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
    still_open   INTEGER NOT NULL DEFAULT 1,
    -- Consecutive polls in which the board did not return this req. Closure used to happen on
    -- the first miss, which wrote off 103 reqs — and at least two of those were provably still
    -- live, answering HTTP 200 while recorded as pulled. A single absence is weak evidence: the
    -- Workday and Oracle India facets are moving windows, so a req can drop out of a filtered
    -- read without being withdrawn. Reset to 0 every time the req comes back.
    misses       INTEGER NOT NULL DEFAULT 0
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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Add columns that CREATE TABLE IF NOT EXISTS cannot add to a database that already exists.
    Kept deliberately dumb — read the existing columns, add what is absent — because the store
    is a single local file with no deployment story, and a migration framework would be more
    machinery than the whole project.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "misses" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN misses INTEGER NOT NULL DEFAULT 0")
    # A second home for the Claude labels, inside the database they were produced in.
    # calibration/claude-labels.json is the primary copy and the one `calibrate.py` measures
    # against; these columns exist so that a DB which has been re-scored still carries what it
    # used to say, and so `calibrate.py --label-column label_score` can measure a rescored
    # file against its own history. Written only by `calibration_labels.py --apply` and by
    # `score.py --rescore` just before it overwrites a score. Never by a fetch.
    if "label_score" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN label_score INTEGER")
    if "label_reason" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN label_reason TEXT")
    # What `fetch.py --enrich` has already tried on this row. Without it, enrichment has no
    # memory: the JS-shell careers pages and the URLs that answer 403 come back into the
    # candidate set every single morning, and they are precisely the rows that can never
    # succeed. Three recorded failures and a row stops eating the budget, which is what makes
    # a budget of 120 land on rows that might actually yield text.
    if "enrich_attempts" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN enrich_attempts INTEGER NOT NULL DEFAULT 0")
    if "enrich_last" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN enrich_last TEXT")
    if "enrich_note" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN enrich_note TEXT")
    # Which engine wrote the score in this row. NULL means "written before this column
    # existed", which for every row in the current corpus means claude. Without it a database
    # that has been partly re-scored holds two different scales in one column with no way to
    # tell the rows apart.
    if "scored_by" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN scored_by TEXT")


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
        # `misses = 0` is what makes the consecutive-absence rule actually consecutive: a req
        # that flickers out of one poll and back into the next must not keep its strike.
        conn.execute(
            f"UPDATE jobs SET {', '.join(sets + ['last_seen = ?', 'still_open = 1', 'misses = 0'])} "
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


def set_score(conn, key: str, score: int, reason: str, lane=None, speed_note=None,
              scored_by: str | None = None) -> None:
    """
    Write a score. `scored_by` records which engine produced it.

    That column is what makes a mixed database readable. The corpus is 918 rows of `claude`
    judgement, and a heuristic run writes on the same scale without saying so, which is how a
    column ends up holding two incomparable scales with no way to tell which row is which.
    `COALESCE(?, scored_by)` so a caller that does not know leaves the existing value alone
    rather than erasing it.
    """
    conn.execute(
        """UPDATE jobs SET score = ?, score_reason = ?,
                  lane = COALESCE(?, lane), speed_note = ?,
                  scored_by = COALESCE(?, scored_by),
                  status = CASE WHEN status = 'new' THEN 'scored' ELSE status END
           WHERE key = ?""",
        (score, reason, lane, speed_note, scored_by, key),
    )


# Engines whose output is worth nothing as ground truth, because it can be re-derived from the
# row at any time. The heuristic is a pure function of the JD and the profile: re-run it and you
# get the same number back. Claude's judgement cannot be re-derived without spending the
# owner's subscription on the whole corpus again, so that is what `label_score` is for.
#
# Getting this predicate wrong is not a cosmetic bug. Treating a heuristic score as a label
# would have a second rescore quietly promote the first rescore's output to ground truth, and
# then `calibrate.py --label-column label_score` would be measuring the heuristic against
# itself and reporting a near-perfect correlation.
REPLACEABLE_ENGINES = ("heuristic",)

_IRREPLACEABLE = (
    "score IS NOT NULL AND label_score IS NULL "
    "AND (scored_by IS NULL OR scored_by NOT IN ('heuristic'))"
)


def protect_labels(conn) -> int:
    """
    Copy `score`/`score_reason` into `label_score`/`label_reason` for every row holding
    judgement that cannot be re-derived. Returns how many rows were protected.

    Called once, immediately before a rescore starts overwriting. Two rows are skipped: one
    that already has a label — the label is the older and more trustworthy value — and one
    whose score the heuristic wrote, which is not a label at all.
    """
    cur = conn.execute(
        f"UPDATE jobs SET label_score = score, label_reason = score_reason "
        f"WHERE {_IRREPLACEABLE}"
    )
    return cur.rowcount


def unprotected_scores(conn) -> int:
    """
    Rows whose `score` is the only copy of judgement that cannot be re-derived.

    This is the number a rescore would destroy for good, and what the gate in `score.py`
    compares the label export against.
    """
    return conn.execute(
        f"SELECT COUNT(*) c FROM jobs WHERE {_IRREPLACEABLE}"
    ).fetchone()["c"]


MISS_LIMIT = 3          # consecutive absences before a req is written off


def mark_closed(conn, source: str, seen_keys: set[str], miss_limit: int = MISS_LIMIT,
                verify=None) -> tuple[int, int]:
    """
    Record that this run did not see certain reqs from `source`, and close only the ones that
    have now been absent `miss_limit` polls in a row. Returns (closed, pending).

    Closing on the first miss was wrong and measurably so. It wrote off 103 reqs, and two of
    them — a JP Morgan observability role and a Visa SDE — were still answering HTTP 200 while
    recorded as pulled. One absence is weak evidence, because `fetch.py` narrows the big boards
    to India server-side and those facets are moving windows: a req can fall out of a filtered
    read, or a board can time out mid-run, without anything happening to the job.

    Three consecutive absences is strong evidence. Anything that reappears resets the counter,
    so a flickering req never accumulates its way to closed.

    `verify(url) -> bool | None` is the second gate, used only at the limit: True keeps the req
    open regardless of the count, False closes it immediately, None means "no answer" and lets
    the counter decide. fetch.py passes an HTTP probe, because a posting URL returning 404 or
    410 is the one piece of direct evidence available — that is the board saying the req is
    gone, rather than us inferring it from a filtered list.
    """
    rows = conn.execute(
        "SELECT key, url, misses FROM jobs WHERE source = ? AND still_open = 1", (source,)
    ).fetchall()
    missing = [r for r in rows if r["key"] not in seen_keys]
    if not missing:
        return 0, 0

    to_close, to_bump, reprieved = [], [], []
    for r in missing:
        n = (r["misses"] or 0) + 1
        if n < miss_limit:
            to_bump.append((n, r["key"]))
            continue
        live = verify(r["url"]) if (verify and r["url"]) else None
        if live is True:
            # Still being served. Park the counter one below the limit so it closes on the next
            # miss if the URL later goes away, rather than probing from scratch every run.
            reprieved.append((miss_limit - 1, r["key"]))
        else:
            to_close.append((r["key"],))

    if to_bump:
        conn.executemany("UPDATE jobs SET misses = ? WHERE key = ?", to_bump)
    if reprieved:
        conn.executemany("UPDATE jobs SET misses = ? WHERE key = ?", reprieved)
    if to_close:
        conn.executemany(
            "UPDATE jobs SET still_open = 0, last_seen = last_seen WHERE key = ?", to_close,
        )
    return len(to_close), len(to_bump) + len(reprieved)


def unscored(conn, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND still_open = 1 "
        "ORDER BY first_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()


def promoted(conn, threshold: int, since: str | None = None) -> list[sqlite3.Row]:
    """
    Scored at or above the threshold and not yet acted on — the digest's main table.

    Rows carry a computed `has_jd` flag, and a sighted row outranks an equally-scored blind
    one. That ordering is the honest one: a 7 derived from a JD and a 7 derived from a title,
    a company and a city are not the same claim, and the second is the weaker of the two
    because the scorer had almost nothing to read. 302 of the 918 scored rows are blind, so
    this is not an edge case — ordering them below their sighted equals puts the reqs the
    scorer actually had evidence for at the top of the list the owner reads first.

    This changes the ORDER BY only. The threshold still decides what is promoted, no row is
    filtered, and `company` still breaks the remaining ties so the output stays deterministic.
    """
    sql = (
        "SELECT *, (COALESCE(length(description), 0) >= ?) AS has_jd FROM jobs "
        "WHERE score >= ? AND still_open = 1 "
        "AND status IN ('scored', 'new') "
    )
    args: list = [JD_MIN_CHARS, threshold]
    if since:
        sql += "AND first_seen >= ? "
        args.append(since)
    return conn.execute(sql + "ORDER BY score DESC, has_jd DESC, company", args).fetchall()


def has_jd(job) -> bool:
    """
    Did the scorer have a JD to read for this row.

    Reads the `has_jd` flag `promoted` computes when it is there and falls back to measuring
    the description, so a caller holding a row from any other query gets the same answer. The
    fallback matters: `digest.py` renders rows from three different queries and the marker has
    to mean the same thing in all three.
    """
    try:
        flag = job["has_jd"]
    except (IndexError, KeyError, TypeError):
        flag = None
    if flag is not None:
        return bool(flag)
    try:
        desc = job["description"]
    except (IndexError, KeyError, TypeError):
        return False
    return len(desc or "") >= JD_MIN_CHARS


COMPANY_CAP = 2


def cap_per_company(rows, cap: int = COMPANY_CAP) -> tuple[list, list]:
    """
    Split `rows` into (kept, overflow), keeping at most `cap` rows per company. Pure.

    Order-preserving in both halves, so the score ordering `promoted` established survives the
    split and the overflow is itself in score order. The input order IS the priority: this
    takes the first `cap` rows each company offers and defers the rest.

    Why this exists. 39 of 132 applications went to JP Morgan Chase — 30% of the search in one
    company — and the digest's action list is what drives that: JPMC has 54 open un-actioned
    reqs, Bosch 29, Mastercard 21, so a list ordered by score alone is mostly one employer
    whatever the scores say. Concentrating a search in one company is a worse bet than the same
    effort spread across three, and a referral ask is per-company anyway.

    This DEFERS, it does not drop. The caller is responsible for rendering the overflow
    somewhere, and the digest does: reqs over the cap go into a collapsed block rather than
    disappearing. A function that silently hid 52 open reqs would be a data-loss bug dressed
    as a feature.

    `cap <= 0` means no cap, rather than an empty list. A cap of zero is a config typo, and the
    failure mode of reading it literally is a blank action list with every req in the overflow
    block — a worse outcome than ignoring it. `--no-company-cap` is the supported way off.
    """
    if cap <= 0:
        return list(rows), []
    kept, overflow, seen = [], [], collections.Counter()
    for r in rows:
        company = (r["company"] or "").strip().casefold()
        seen[company] += 1
        (kept if seen[company] <= cap else overflow).append(r)
    return kept, overflow


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
