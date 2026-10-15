"""
Which reqs a draft run selects, and the per-company cap that bounds it.

DATA SAFETY. `draft.py` writes files into outbox/ and rows into outreach, so every test here
stops at `--dry-run`, which prints the selection and returns before anything is written. The
database is always tmp_path, passed with `--db`; `jobprofile.load` is monkeypatched so no test
reads the owner's real profile.

The cap matters most here rather than in the digest. A digest row costs a few seconds of
reading; a draft is a referral ask to a human, and `--limit 6` means a run could spend all six
on one employer — JP Morgan Chase alone has 54 open un-actioned reqs, and 39 of the owner's 132
applications already went there.
"""

from __future__ import annotations

import pytest

import draft
import jobprofile
import store

JD = ("Requirements: 3 years of experience building backend services in java and spring "
      "boot on aws. You will own microservices end to end, work with kafka and dynamodb. " * 6)

PROFILE = {
    "candidate": {"name": "Test Owner", "short_name": "Test", "email": "t@example.test",
                  "phone": "0000000000", "links": {"linkedin": "https://example.test/in"}},
    "resume_summary": "Backend engineer, java and spring boot on aws.",
    "runtime": {"score_threshold": 7, "engine": "heuristic", "company_cap": 2},
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(jobprofile, "load", lambda *a, **k: PROFILE)
    path = tmp_path / "draft.db"
    with store.connect(path) as conn:
        yield path, conn


def scored(conn, company, req_id, score, description=JD):
    title = "Software Engineer II"
    job = {"company": company, "title": title, "req_id": req_id,
           "url": f"https://example.test/{company}/{req_id}", "source": "greenhouse",
           "description": description}
    store.upsert_job(conn, job)
    key = store.job_key(company, title, job["url"], req_id)
    store.set_score(conn, key, score, f"scored {score}", scored_by="claude")
    conn.execute("UPDATE jobs SET status = 'scored' WHERE key = ?", (key,))
    return key


def run(db_path, *extra):
    return draft.main(["--dry-run", "--db", str(db_path), *extra])


def test_five_reqs_at_one_company_select_two(db, capsys):
    path, conn = db
    for i, s in enumerate([9, 9, 8, 8, 7]):
        scored(conn, "Acme", str(i), s)
    conn.commit()

    assert run(path) == 0
    out = capsys.readouterr().out
    assert "2 draft(s) to write" in out
    assert out.count("Acme") == 2
    assert "3 req(s) held back by the per-company cap" in out


def test_the_cap_selects_the_highest_scoring_reqs(db, capsys):
    path, conn = db
    for i, s in enumerate([7, 9, 8, 7]):
        scored(conn, "Acme", str(i), s)
    conn.commit()

    run(path)
    out = capsys.readouterr().out
    assert " 9  Acme" in out and " 8  Acme" in out
    assert " 7  Acme" not in out


def test_the_cap_spreads_a_run_across_companies(db, capsys):
    """The behaviour the cap is for: six drafts reaching three employers, not one."""
    path, conn = db
    for i, s in enumerate([9, 9, 9, 9]):
        scored(conn, "Acme", str(i), s)
    for i, s in enumerate([8, 8]):
        scored(conn, "Bosch", str(i), s)
    conn.commit()

    run(path)
    out = capsys.readouterr().out
    assert "4 draft(s) to write" in out
    assert out.count("Acme") == 2
    assert out.count("Bosch") == 2


def test_no_company_cap_restores_the_old_selection(db, capsys):
    path, conn = db
    for i, s in enumerate([9, 9, 8, 8, 7]):
        scored(conn, "Acme", str(i), s)
    conn.commit()

    assert run(path, "--no-company-cap") == 0
    out = capsys.readouterr().out
    assert "5 draft(s) to write" in out
    assert "held back" not in out


def test_a_cap_of_zero_in_the_profile_disables_it(db, capsys, monkeypatch):
    path, conn = db
    monkeypatch.setattr(jobprofile, "load",
                        lambda *a, **k: {**PROFILE,
                                         "runtime": {**PROFILE["runtime"], "company_cap": 0}})
    for i, s in enumerate([9, 9, 8]):
        scored(conn, "Acme", str(i), s)
    conn.commit()

    run(path)
    assert "3 draft(s) to write" in capsys.readouterr().out


def test_naming_keys_explicitly_bypasses_the_cap(db, capsys):
    """
    --key is an instruction about specific reqs. Capping an explicit instruction is a bug, not
    a safeguard — the owner asked for these three by name.
    """
    path, conn = db
    keys = [scored(conn, "Acme", str(i), s) for i, s in enumerate([9, 9, 8])]
    conn.commit()

    assert run(path, *[a for k in keys for a in ("--key", k)]) == 0
    out = capsys.readouterr().out
    assert "3 draft(s) to write" in out
    assert "held back" not in out


def test_the_limit_still_applies_under_the_cap(db, capsys):
    path, conn = db
    for company in ("Acme", "Bosch", "Citrine"):
        for i, s in enumerate([9, 8]):
            scored(conn, company, str(i), s)
    conn.commit()

    run(path, "--limit", "3")
    assert "3 draft(s) to write" in capsys.readouterr().out


def test_an_already_drafted_req_is_still_skipped(db, capsys):
    """The cap sits alongside the one-ask-per-req rule; it does not replace it."""
    path, conn = db
    first = scored(conn, "Acme", "0", 9)
    scored(conn, "Acme", "1", 9)
    scored(conn, "Acme", "2", 8)
    store.add_outreach(conn, job_key=first, company="Acme", kind="referral",
                       draft_path="outbox/x.md")
    conn.commit()

    run(path)
    out = capsys.readouterr().out
    # The cap keeps reqs 0 and 1; req 0 is already drafted, so only req 1 is selected.
    assert "1 draft(s) to write" in out


def test_nothing_above_the_bar_selects_nothing(db, capsys):
    path, conn = db
    scored(conn, "Acme", "0", 6)
    conn.commit()
    assert run(path) == 0
    assert "nothing to draft" in capsys.readouterr().out
