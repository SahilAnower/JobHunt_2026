"""
Which rows enrichment spends its budget on, and what it remembers afterwards.

DATA SAFETY. Every test passes tmp_path to store.connect() and `db=` to fetch.enrich(); the
default is the live jobhunt.db. NO NETWORK: the two functions that reach out, fetch._get_text
and fetch.jd_from_api, are monkeypatched in every test that calls enrich(), and the per-worker
time.sleep is stubbed so the suite stays under a second.

The ordering is the point of the whole item. The budget used to go to whatever SQLite handed
over first, which was the oldest rows — the JS-shell careers pages at Meta, Microsoft and Apple
and the URLs that 404, the only rows that can never succeed. Two rules fix that: rank by what
the real scorer already makes of the row, and stop retrying a row that has failed three times.
"""

from __future__ import annotations

import urllib.error

import pytest

import engine_heuristic as eh
import fetch
import store

JD = ("Responsibilities: build backend services in Java and Spring Boot on AWS. "
      "Qualifications: 2+ years of experience with distributed systems and microservices. "
      + "We work on high throughput systems with Kafka and SQS. " * 12)


@pytest.fixture()
def conn(tmp_path):
    with store.connect(tmp_path / "test.db") as c:
        yield c


@pytest.fixture()
def ctx():
    """The real scoring context, from the tracked example profile."""
    import jobprofile
    return eh.build_context(jobprofile.load(fetch.ROOT / "config" / "profile.example.yaml"),
                            None)


def seed(conn, company, title, *, first_seen, description=None, attempts=0, url="x",
         still_open=1, source="greenhouse", location="Hyderabad, India"):
    """One job row, with the columns the ranking and the candidate query read."""
    key = f"{company.lower()}::{title.lower()}::{url}"
    conn.execute(
        "INSERT INTO jobs (key, company, title, location, url, source, description, "
        "enrich_attempts, first_seen, last_seen, still_open, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
        (key, company, title, location, f"https://example.test/{url}", source, description,
         attempts, first_seen, first_seen, still_open),
    )
    return key


def row(conn, key):
    return conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()


# --- the candidate query

def test_a_promising_blind_row_is_tried_before_an_older_hopeless_one(conn, ctx):
    """
    The ordering rule, measured rather than asserted by name: the row the scorer puts higher
    goes first even though it is newer, and the test checks the scores really do differ.
    """
    good = seed(conn, "Cloudflare", "Software Engineer, Backend", first_seen="2026-02-01")
    weak = seed(conn, "Acme", "Technical Content Writer", first_seen="2026-01-01")

    scores = {r["key"]: eh.score_one(r, ctx).score
              for r in conn.execute("SELECT * FROM jobs")}
    assert scores[good] >= 6 > scores[weak], "fixture no longer exercises the ranking"

    got = [r["key"] for r in fetch.enrich_candidates(conn, ctx, limit=10)]
    assert got == [good, weak]


def test_rows_at_the_attempt_ceiling_are_not_chased_again(conn, ctx):
    live = seed(conn, "Cloudflare", "Software Engineer", first_seen="2026-01-01", attempts=2)
    spent = seed(conn, "Stripe", "Software Engineer II", first_seen="2026-02-01", attempts=3)

    got = [r["key"] for r in fetch.enrich_candidates(conn, ctx, limit=10, max_attempts=3)]
    assert got == [live]
    assert spent not in got


def test_a_raised_ceiling_brings_a_written_off_row_back(conn, ctx):
    spent = seed(conn, "Stripe", "Software Engineer II", first_seen="2026-02-01", attempts=3)
    assert fetch.enrich_candidates(conn, ctx, limit=10, max_attempts=3) == []
    got = [r["key"] for r in fetch.enrich_candidates(conn, ctx, limit=10, max_attempts=4)]
    assert got == [spent]


def test_rows_that_already_have_a_jd_are_not_candidates(conn, ctx):
    seed(conn, "Cloudflare", "Software Engineer", first_seen="2026-02-01", description=JD)
    blind = seed(conn, "Stripe", "Software Engineer II", first_seen="2026-01-01",
                 description="a few words, not a JD")
    got = [r["key"] for r in fetch.enrich_candidates(conn, ctx, limit=10)]
    assert got == [blind]


def test_closed_rows_are_not_candidates(conn, ctx):
    seed(conn, "Acme", "Software Engineer", first_seen="2026-02-01", still_open=0)
    assert fetch.enrich_candidates(conn, ctx, limit=10) == []


def test_the_budget_is_a_hard_cap(conn, ctx):
    for i in range(5):
        seed(conn, f"Co{i}", "Software Engineer", first_seen=f"2026-01-0{i + 1}", url=str(i))
    assert len(fetch.enrich_candidates(conn, ctx, limit=2)) == 2


def test_no_context_falls_back_to_newest_first(conn):
    """A broken scoring config costs the ranking, not the JD recovery."""
    old = seed(conn, "Cloudflare", "Software Engineer", first_seen="2026-01-01")
    new = seed(conn, "Acme", "Technical Content Writer", first_seen="2026-03-01")
    got = [r["key"] for r in fetch.enrich_candidates(conn, None, limit=10)]
    assert got == [new, old]


def test_equal_scores_keep_newest_first(conn, ctx):
    # Same company and title, so the two rows score identically and only the date can order
    # them. The scores are asserted equal below rather than assumed.
    old = seed(conn, "Acme", "Software Engineer", first_seen="2026-01-01", url="1")
    new = seed(conn, "Acme", "Software Engineer", first_seen="2026-03-01", url="2")
    ranked = fetch.enrich_candidates(conn, ctx, limit=10)
    scores = [eh.score_one(r, ctx).score for r in ranked]
    assert scores[0] == scores[1], "fixture no longer exercises the tie-break"
    assert [r["key"] for r in ranked] == [new, old]


def test_a_malformed_scoring_config_yields_no_context(capsys):
    bad = {"scoring": {"weights": {"overlap_max": -1.0}}}
    assert fetch.enrich_context(bad) is None
    assert "not ranking enrichment candidates" in capsys.readouterr().out


def test_no_profile_means_no_context():
    assert fetch.enrich_context(None) is None


# --- what enrich() records

@pytest.fixture()
def no_network(monkeypatch):
    """Nothing leaves the machine, and nothing waits half a second to prove it."""
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fetch, "jd_from_api", lambda *a, **k: "")

    def answer(fn):
        monkeypatch.setattr(fetch, "_get_text", fn)
    return answer


def test_a_recovered_jd_is_stored_and_the_attempt_recorded(tmp_path, no_network):
    db = tmp_path / "t.db"
    with store.connect(db) as c:
        key = seed(c, "Cloudflare", "Software Engineer", first_seen="2026-02-01")
    no_network(lambda url, ua: JD)

    fetch.enrich("ua", 10, None, db=db)

    with store.connect(db) as c:
        r = row(c, key)
    assert len(r["description"]) > fetch.JD_MIN_CHARS
    assert r["enrich_attempts"] == 1
    assert r["enrich_note"].startswith("recovered ")
    assert r["enrich_last"]


def test_a_js_shell_records_a_failed_attempt_and_stores_nothing(tmp_path, no_network):
    db = tmp_path / "t.db"
    with store.connect(db) as c:
        key = seed(c, "Meta", "Software Engineer", first_seen="2026-02-01")
    no_network(lambda url, ua: "<html>cookie banner</html>")

    fetch.enrich("ua", 10, None, db=db)

    with store.connect(db) as c:
        r = row(c, key)
    assert r["description"] is None
    assert r["enrich_attempts"] == 1
    assert "no JD" in r["enrich_note"]


def test_three_failed_runs_take_a_row_out_of_the_candidate_set(tmp_path, no_network):
    db = tmp_path / "t.db"
    with store.connect(db) as c:
        key = seed(c, "Meta", "Software Engineer", first_seen="2026-02-01")
    no_network(lambda url, ua: "<html>shell</html>")

    for _ in range(3):
        fetch.enrich("ua", 10, None, db=db, max_attempts=3)

    with store.connect(db) as c:
        assert row(c, key)["enrich_attempts"] == 3
        assert fetch.enrich_candidates(c, None, limit=10, max_attempts=3) == []


def test_a_404_marks_the_req_closed(tmp_path, no_network):
    db = tmp_path / "t.db"
    with store.connect(db) as c:
        key = seed(c, "Acme", "Software Engineer", first_seen="2026-02-01")

    def gone(url, ua):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    no_network(gone)

    fetch.enrich("ua", 10, None, db=db)

    with store.connect(db) as c:
        r = row(c, key)
    assert r["still_open"] == 0
    assert "HTTP 404" in r["notes"]
    assert r["enrich_note"] == "HTTP 404, req closed"


def test_a_403_does_not_mark_the_req_closed(tmp_path, no_network):
    db = tmp_path / "t.db"
    with store.connect(db) as c:
        key = seed(c, "Acme", "Software Engineer", first_seen="2026-02-01")

    def blocked(url, ua):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
    no_network(blocked)

    fetch.enrich("ua", 10, None, db=db)

    with store.connect(db) as c:
        r = row(c, key)
    assert r["still_open"] == 1, "a site blocking a script says nothing about the req"
    assert r["notes"] is None
    assert r["enrich_note"] == "HTTP 403, blocked"


def test_a_transport_error_records_its_type(tmp_path, no_network):
    db = tmp_path / "t.db"
    with store.connect(db) as c:
        key = seed(c, "Acme", "Software Engineer", first_seen="2026-02-01")

    def boom(url, ua):
        raise TimeoutError("too slow")
    no_network(boom)

    fetch.enrich("ua", 10, None, db=db)

    with store.connect(db) as c:
        r = row(c, key)
    assert r["enrich_attempts"] == 1
    assert r["enrich_note"] == "TimeoutError"
    assert r["still_open"] == 1


def test_enrich_writes_only_to_the_db_it_was_given(tmp_path, no_network, monkeypatch):
    """The --db passthrough, asserted where it matters: the default is the live file."""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "must-not-be-touched.db")
    db = tmp_path / "copy.db"
    with store.connect(db) as c:
        seed(c, "Cloudflare", "Software Engineer", first_seen="2026-02-01")
    no_network(lambda url, ua: JD)

    fetch.enrich("ua", 10, None, db=db)

    assert not (tmp_path / "must-not-be-touched.db").exists()


# --- the budget defaults

def test_the_shipped_budget_is_higher_than_the_old_thirty():
    assert fetch.ENRICH_LIMIT == 120
    assert fetch.ENRICH_MAX_ATTEMPTS == 3


def test_the_profile_can_set_the_budget():
    import jobprofile
    rt = jobprofile.runtime(jobprofile.load(fetch.ROOT / "config" / "profile.example.yaml"))
    assert rt["enrich_limit"] == fetch.ENRICH_LIMIT
    assert rt["enrich_max_attempts"] == fetch.ENRICH_MAX_ATTEMPTS
