"""
The digest's headline numbers.

digest.build() needs only a profile dict and a connection, and writes nothing — main() does
the file write — so it is directly testable against a tmp_path database. As in test_store.py,
every connection here is tmp_path; store.connect()'s default is the live jobhunt.db.
"""

from __future__ import annotations

import pytest

import digest
import store

PROFILE = {"runtime": {"score_threshold": 7, "timezone": "Asia/Kolkata"}}


@pytest.fixture()
def conn(tmp_path):
    with store.connect(tmp_path / "digest.db") as c:
        yield c


def add_job(conn, req_id="1", title="Software Engineer", **extra):
    job = {"company": "Acme", "title": title, "req_id": req_id,
           "url": f"https://example.test/{req_id}", "source": "greenhouse", **extra}
    store.upsert_job(conn, job)
    return store.job_key("Acme", title, job["url"], req_id)


def test_referral_asks_are_still_counted_after_the_req_moved_to_applied(conn):
    """
    The bug this fixes. An ask went out, then the req was marked applied, which overwrote
    jobs.status — and counting by status then reported "0 referral ask(s) out" on a day the
    outreach log held the ask.
    """
    key = add_job(conn)
    store.add_outreach(conn, job_key=key, company="Acme", kind="referral", sent_at=store.now())
    conn.execute("UPDATE jobs SET status = 'applied' WHERE key = ?", (key,))

    text = digest.build(conn, PROFILE)
    assert "1 referral ask(s) out" in text


def test_an_unsent_draft_is_not_counted_as_an_ask(conn):
    """
    The other half: draft.py writes an outreach row with sent_at NULL, because nothing in this
    project sends anything. Counting it would claim an ask that is still sitting in outbox/.
    The same row has to keep appearing under "Do today", so both readings stay consistent.
    """
    key = add_job(conn, score=9)
    conn.execute("UPDATE jobs SET score = 9, status = 'scored' WHERE key = ?", (key,))
    store.add_outreach(conn, job_key=key, company="Acme", kind="referral",
                       draft_path="outbox/x.md")

    text = digest.build(conn, PROFILE)
    assert "0 referral ask(s) out" in text
    assert "## Do today" in text
    assert "outbox/x.md" in text


def test_one_req_asked_once_counts_once(conn):
    # mark.py guards with outreach_exists, but the count is of reqs rather than rows, and
    # COUNT(DISTINCT job_key) makes that true by construction rather than by that guard.
    key = add_job(conn)
    store.add_outreach(conn, job_key=key, company="Acme", kind="referral", sent_at=store.now())
    store.add_outreach(conn, job_key=key, company="Acme", kind="referral", sent_at=store.now())
    assert "1 referral ask(s) out" in digest.build(conn, PROFILE)


def test_an_application_row_is_not_a_referral_ask(conn):
    # mark.py writes kind='application' for `applied`. Only referral asks belong in this count.
    key = add_job(conn)
    store.add_outreach(conn, job_key=key, company="Acme", kind="application",
                       sent_at=store.now())
    assert "0 referral ask(s) out" in digest.build(conn, PROFILE)


def test_an_empty_database_builds_a_digest(conn):
    # The state on a fresh clone, and on any morning before the first fetch. It must not raise.
    text = digest.build(conn, PROFILE)
    assert "0 referral ask(s) out" in text
    assert text.startswith("# jobhunt digest")
