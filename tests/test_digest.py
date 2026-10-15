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


# ------------------------------------------------------------------- confidence in the digest

JD = ("Requirements: 3 years of experience building backend services in java and spring "
      "boot on aws. You will own microservices end to end, work with kafka and dynamodb, "
      "and help with system design reviews. " * 6)


def scored(conn, company, req_id, score, description=None, title="Software Engineer II",
           status="scored"):
    """One scored, still-open req at a named company. Returns its key."""
    job = {"company": company, "title": title, "req_id": req_id,
           "url": f"https://example.test/{company}/{req_id}", "source": "greenhouse",
           "description": description}
    store.upsert_job(conn, job)
    key = store.job_key(company, title, job["url"], req_id)
    store.set_score(conn, key, score, f"scored {score}", scored_by="claude")
    conn.execute("UPDATE jobs SET status = ? WHERE key = ?", (status, key))
    return key


def test_a_blind_row_is_marked_and_a_sighted_one_is_not(conn):
    """
    The confidence marker. A 7 read off a full JD and a 7 read off a title, a company and a
    city are not the same claim, and the digest is the only place the owner sees either.
    """
    scored(conn, "Acme", "1", 8, description=JD)
    scored(conn, "Zeta", "2", 8)
    text = digest.build(conn, PROFILE)

    assert digest.NO_JD_CELL in text
    assert digest.NO_JD_BULLET in text
    # One marker in the table cell and one on the bullet, for the blind row only.
    assert text.count(digest.NO_JD_CELL) == 1
    assert text.count(digest.NO_JD_BULLET) == 1


def test_the_blind_marker_lands_on_the_blind_row_and_not_its_neighbour(conn):
    scored(conn, "Acme", "1", 8, description=JD)
    scored(conn, "Zeta", "2", 8)
    rows = [ln for ln in digest.build(conn, PROFILE).splitlines()
            if ln.startswith("| 8 |")]
    assert len(rows) == 2
    acme = next(ln for ln in rows if "Acme" in ln)
    zeta = next(ln for ln in rows if "Zeta" in ln)
    assert digest.NO_JD_CELL not in acme
    assert digest.NO_JD_CELL in zeta


def test_the_sighted_row_is_listed_above_the_blind_one(conn):
    """
    The ordering, end to end through the digest rather than just through store.promoted.
    "Zeta" sorts last, so if confidence did not order these the blind row would be first.
    """
    scored(conn, "Acme", "1", 8)                      # blind, sorts FIRST by company
    scored(conn, "Zeta", "2", 8, description=JD)      # sighted, sorts last by company
    text = digest.build(conn, PROFILE)
    assert text.index("Zeta") < text.index("Acme")


def test_nothing_is_marked_when_every_row_has_a_jd(conn):
    scored(conn, "Acme", "1", 8, description=JD)
    scored(conn, "Zeta", "2", 9, description=JD)
    text = digest.build(conn, PROFILE)
    assert digest.NO_JD_CELL not in text
    assert digest.NO_JD_BULLET not in text


def test_the_do_today_section_marks_a_blind_row(conn):
    key = scored(conn, "Acme", "1", 8)
    store.add_outreach(conn, job_key=key, company="Acme", kind="referral",
                       draft_path="outbox/x.md")
    text = digest.build(conn, PROFILE)
    ready = next(ln for ln in text.splitlines() if "outbox/x.md" in text and "8/10" in ln)
    assert digest.NO_JD_CELL in ready


def test_the_below_the_bar_block_marks_a_blind_row(conn):
    scored(conn, "Acme", "1", 4)
    scored(conn, "Zeta", "2", 5, description=JD)
    text = digest.build(conn, PROFILE)
    below = [ln for ln in text.splitlines() if ln.startswith("- 4/10")]
    assert len(below) == 1
    assert digest.NO_JD_BULLET in below[0]
    assert digest.NO_JD_BULLET not in next(ln for ln in text.splitlines()
                                           if ln.startswith("- 5/10"))


def test_the_marker_does_not_change_the_threshold_or_the_sections(conn):
    """
    The constraint on item 6: it surfaces confidence and changes nothing else. A blind 8 is
    still promoted and a sighted 6 is still below the bar.
    """
    scored(conn, "Acme", "1", 8)
    scored(conn, "Zeta", "2", 6, description=JD)
    text = digest.build(conn, PROFILE)
    assert "## Scored 7+ and not yet acted on (1)" in text
    assert "Below the bar (1)" in text
    for section in ("## Do today", "## In motion", "## The read"):
        assert section in text


# ------------------------------------------------------------------- the per-company cap

CAP_PROFILE = {"runtime": {"score_threshold": 7, "timezone": "Asia/Kolkata",
                           "company_cap": 2}}


# Five reqs that all clear the 7+ bar, so the cap is the only thing that can split them.
CAP_SCORES = [9, 9, 8, 8, 7]


def test_five_acme_reqs_are_all_present_two_in_the_table_and_three_deferred(conn):
    """
    The property that makes the cap an ordering and not a filter: every req still appears
    somewhere. Hiding 52 open reqs that cleared the bar would be a data-loss bug.
    """
    keys = [scored(conn, "Acme", str(i), s, description=JD)
            for i, s in enumerate(CAP_SCORES)]
    text = digest.build(conn, CAP_PROFILE)

    assert "## Scored 7+ and not yet acted on (2)" in text
    assert "More open reqs at companies already in today's list (3)" in text
    # Every one of the five is reachable from the digest, by its own posting URL.
    for i in range(len(CAP_SCORES)):
        assert f"https://example.test/Acme/{i}" in text
    # Two rows in the main table, three in the overflow block.
    assert len([ln for ln in text.splitlines() if ln.startswith("| ")
                and "example.test/Acme" in ln]) == 2
    block = text.split("More open reqs")[1].split("</details>")[0]
    assert len([ln for ln in block.splitlines() if ln.startswith("- ")]) == 3


def test_the_deferred_block_lists_the_lower_scoring_reqs(conn):
    for i, s in enumerate(CAP_SCORES):
        scored(conn, "Acme", str(i), s, description=JD)
    text = digest.build(conn, CAP_PROFILE)
    block = text.split("More open reqs")[1].split("</details>")[0]
    # The two 9s are kept, so the overflow is the 8s and the 7 — the cap defers by score.
    assert "8/10" in block and "7/10" in block
    assert "9/10" not in block


def test_the_cap_does_not_fire_across_different_companies(conn):
    for i, company in enumerate(["Acme", "Bosch", "Citrine", "Delta"]):
        scored(conn, company, str(i), 8, description=JD)
    text = digest.build(conn, CAP_PROFILE)
    assert "## Scored 7+ and not yet acted on (4)" in text
    assert "More open reqs" not in text


def test_a_drafted_req_is_not_pushed_out_of_do_today_by_fresh_ones(conn):
    """
    Why the two sections are capped separately. A draft already written and waiting is the one
    thing in the digest with a cost attached — the ask was prepared and not sent — so two
    fresh Acme reqs must not bury it.
    """
    drafted_key = scored(conn, "Acme", "d", 7, description=JD)
    store.add_outreach(conn, job_key=drafted_key, company="Acme", kind="referral",
                       draft_path="outbox/waiting.md")
    scored(conn, "Acme", "f1", 9, description=JD)
    scored(conn, "Acme", "f2", 8, description=JD)

    text = digest.build(conn, CAP_PROFILE)
    do_today = text.split("## Do today")[1].split("## Scored")[0]
    assert "outbox/waiting.md" in do_today
    assert "More open reqs" not in text


def test_the_above_the_bar_count_still_counts_every_promoted_req(conn):
    """The cap defers; it must not make the digest understate the state of the search."""
    for i, s in enumerate(CAP_SCORES):
        scored(conn, "Acme", str(i), s, description=JD)
    assert "5 req(s) above the bar" in digest.build(conn, CAP_PROFILE)


def test_the_cap_defaults_to_two_when_the_profile_does_not_set_it(conn):
    for i, s in enumerate([9, 9, 8, 8]):
        scored(conn, "Acme", str(i), s, description=JD)
    text = digest.build(conn, PROFILE)          # no company_cap key
    assert "## Scored 7+ and not yet acted on (2)" in text
    assert "More open reqs at companies already in today's list (2)" in text


def test_a_company_cap_of_zero_shows_everything_in_the_main_table(conn):
    for i, s in enumerate([9, 9, 8, 8]):
        scored(conn, "Acme", str(i), s, description=JD)
    off = {"runtime": {"score_threshold": 7, "company_cap": 0}}
    text = digest.build(conn, off)
    assert "## Scored 7+ and not yet acted on (4)" in text
    assert "More open reqs" not in text


def test_the_deferred_block_sits_after_section_two_and_before_in_motion(conn):
    for i, s in enumerate([9, 9, 8]):
        scored(conn, "Acme", str(i), s, description=JD)
    text = digest.build(conn, CAP_PROFILE)
    assert text.index("## Scored 7+") < text.index("More open reqs") < text.index("## In motion")


def test_the_cap_does_not_add_or_remove_a_section(conn):
    for i, s in enumerate([9, 9, 8]):
        scored(conn, "Acme", str(i), s, description=JD)
    text = digest.build(conn, CAP_PROFILE)
    for section in ("## Do today", "## Scored 7+", "## In motion", "## The read"):
        assert section in text
