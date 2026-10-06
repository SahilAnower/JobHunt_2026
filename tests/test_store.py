"""
The dedupe identity and the closure rule.

DATA SAFETY, and these are not negotiable:

  1. Never call store.connect() with no argument. The default is ROOT/"jobhunt.db" — the real
     database, with every tracked application in it. connect() also runs executescript(SCHEMA)
     and _migrate() against whatever it opens. Every test here passes tmp_path.
     In this worktree jobhunt.db is a SYMLINK into the main checkout, so the default is not
     even a local copy; it is the live file.
  2. No network. Nothing here touches fetch.FETCHERS, enrich or recheck_closed.
  3. The `verify` callback is always a local lambda, never fetch.url_is_live.

mark_closed is the function worth the most care in this repo. Closing on the first miss wrote
off 103 reqs, two of which were answering HTTP 200 at the time. The consecutive-miss rule is
what fixed that, and test_a_reappearing_req_resets_the_counter below is the property that
actually holds it in place.
"""

from __future__ import annotations

import re

import pytest

import store


@pytest.fixture()
def conn(tmp_path):
    """A real SQLite file, in a directory pytest throws away. Never the project database."""
    with store.connect(tmp_path / "test.db") as c:
        yield c


def seed(conn, req_id, source="greenhouse", url="https://example.test/1", title=None):
    """Insert one job and hand back its key, so a test never has to spell the hash out."""
    job = {"company": "Acme", "title": title or "Software Engineer",
           "req_id": req_id, "url": url, "source": source}
    store.upsert_job(conn, job)
    return store.job_key("Acme", job["title"], url or "", req_id)


def row(conn, key):
    return conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()


# --- job_key

def test_req_id_is_preferred():
    assert store.job_key("Cloudflare", "Software Engineer", "http://x", "8168623") == (
        "cloudflare::8168623")


def test_company_and_req_id_are_normalised():
    assert store.job_key("  Cloudflare ", "T", "", "9") == "cloudflare::9"
    assert store.job_key("Cloudflare", "T", "", " 9 ") == "cloudflare::9"


def test_the_url_is_deliberately_not_part_of_the_identity():
    """
    The point of the function. One req routinely appears under a LinkedIn redirect, a careers
    path and a direct ATS link. Keying on the URL would let the same job into the digest three
    times, so the fallback hashes company + normalised title instead.
    """
    a = store.job_key("Cloudflare", "Software Engineer", "https://linkedin.test/r/1")
    b = store.job_key("Cloudflare", "Software Engineer", "https://boards.test/cf/jobs/9")
    assert a == b


def test_title_normalisation_collapses_case_and_punctuation():
    assert store.job_key("Cloudflare", "Software  Engineer!") == (
        store.job_key("cloudflare", "software engineer"))


def test_different_titles_get_different_keys():
    # The flip side: the normalisation must not be so aggressive that two real reqs collide.
    assert store.job_key("X", "SDE I") != store.job_key("X", "SDE II")


def test_fallback_key_shape():
    key = store.job_key("Acme Corp.", "Software Engineer")
    assert re.fullmatch(r"[a-z0-9 .\-]+::[0-9a-f]{12}", key), key


# --- mark_closed

def test_nothing_missing_returns_early(conn):
    key = seed(conn, "1")
    assert store.mark_closed(conn, "greenhouse", {key}) == (0, 0)
    assert row(conn, key)["misses"] == 0


def test_first_miss_bumps_and_does_not_close(conn):
    key = seed(conn, "1")
    assert store.mark_closed(conn, "greenhouse", set(), verify=None) == (0, 1)
    assert row(conn, key)["misses"] == 1
    assert row(conn, key)["still_open"] == 1


def test_second_miss_bumps(conn):
    key = seed(conn, "1")
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert store.mark_closed(conn, "greenhouse", set(), verify=None) == (0, 1)
    assert row(conn, key)["misses"] == 2
    assert row(conn, key)["still_open"] == 1


def test_three_consecutive_misses_close(conn):
    key = seed(conn, "1")
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert store.mark_closed(conn, "greenhouse", set(), verify=None) == (1, 0)
    assert row(conn, key)["still_open"] == 0


def test_a_reappearing_req_resets_the_counter(conn):
    """
    The most important test in this file.

    A req that flickers out of one poll and back into the next must not keep its strike, or it
    accumulates its way to closed over a few days while being live the whole time. The India
    facets on Workday and Oracle are moving windows, so flickering is normal, not exceptional.
    """
    key = seed(conn, "1")
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert row(conn, key)["misses"] == 2

    seed(conn, "1")                                   # the board returned it again
    assert row(conn, key)["misses"] == 0
    assert row(conn, key)["still_open"] == 1

    # Two more misses must still be short of the limit, not two on top of the old two.
    assert store.mark_closed(conn, "greenhouse", set(), verify=None) == (0, 1)
    assert store.mark_closed(conn, "greenhouse", set(), verify=None) == (0, 1)
    assert row(conn, key)["still_open"] == 1


def test_verify_true_grants_a_reprieve_and_parks_the_counter(conn):
    # The URL is still being served, so the req stays open whatever the counter says. The
    # counter parks one below the limit so the next miss closes it without probing again.
    key = seed(conn, "1")
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert store.mark_closed(conn, "greenhouse", set(), verify=lambda u: True) == (0, 1)
    assert row(conn, key)["still_open"] == 1
    assert row(conn, key)["misses"] == store.MISS_LIMIT - 1


def test_verify_false_closes_at_the_limit(conn):
    # 404 or 410: the board itself saying the req is gone. Direct evidence beats the counter.
    key = seed(conn, "1")
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert store.mark_closed(conn, "greenhouse", set(), verify=lambda u: False) == (1, 0)
    assert row(conn, key)["still_open"] == 0


def test_verify_none_defers_to_the_counter(conn):
    # No usable answer (403, 429, 5xx, timeout). The counter is at the limit, so it closes.
    key = seed(conn, "1")
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert store.mark_closed(conn, "greenhouse", set(), verify=lambda u: None) == (1, 0)
    assert row(conn, key)["still_open"] == 0


def test_verify_is_not_consulted_below_the_limit(conn):
    # An HTTP probe per absent req per run would be most of the fetch stage's runtime. The
    # probe is only worth paying for at the moment a req is about to be written off.
    seed(conn, "1")
    probed = []
    store.mark_closed(conn, "greenhouse", set(), verify=lambda u: probed.append(u) or True)
    assert probed == []


def test_a_row_with_no_url_cannot_be_verified(conn):
    # `verify and r["url"]` is falsy, so live stays None and the counter decides alone.
    key = seed(conn, "1", url=None)
    for _ in range(3):
        store.mark_closed(conn, "greenhouse", set(), verify=lambda u: True)
    assert row(conn, key)["still_open"] == 0


def test_miss_limit_of_one_closes_on_the_first_miss(conn):
    key = seed(conn, "1")
    assert store.mark_closed(conn, "greenhouse", set(), miss_limit=1, verify=None) == (1, 0)
    assert row(conn, key)["still_open"] == 0


def test_closure_is_scoped_to_one_source(conn):
    """
    A board timing out must not close another board's reqs. This is also why fetch.py keys
    mark_closed on everything a board returned rather than on what survived the gates.
    """
    gh = seed(conn, "1", source="greenhouse")
    wd = seed(conn, "2", source="workday", url="https://example.test/2")
    for _ in range(3):
        store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert row(conn, gh)["still_open"] == 0
    assert row(conn, wd)["still_open"] == 1
    assert row(conn, wd)["misses"] == 0


def test_already_closed_rows_are_ignored(conn):
    key = seed(conn, "1")
    for _ in range(3):
        store.mark_closed(conn, "greenhouse", set(), verify=None)
    assert row(conn, key)["still_open"] == 0
    # A closed req is not reconsidered every run; that is what --recheck-closed is for.
    assert store.mark_closed(conn, "greenhouse", set(), verify=None) == (0, 0)
