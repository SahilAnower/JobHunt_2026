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


# --- set_score and the label columns
#
# `label_score` is where Claude's judgement goes to survive a rescore, and `scored_by` is how a
# mixed database stays readable. The property that matters most is the one in
# test_a_heuristic_score_is_never_promoted_to_a_label: a heuristic score is a pure function of
# the row, so treating it as ground truth would let a second rescore launder the first one's
# output into the labels, and calibrate.py would then measure the heuristic against itself.

def test_set_score_records_the_engine(conn):
    key = seed(conn, "1")
    store.set_score(conn, key, 7, "good fit", scored_by="claude")
    assert row(conn, key)["scored_by"] == "claude"


def test_set_score_without_an_engine_leaves_the_old_one(conn):
    key = seed(conn, "1")
    store.set_score(conn, key, 7, "good fit", scored_by="claude")
    store.set_score(conn, key, 4, "on reflection")
    r = row(conn, key)
    assert r["score"] == 4
    assert r["scored_by"] == "claude", "a caller that does not know must not erase it"


def test_a_score_starts_with_no_label(conn):
    key = seed(conn, "1")
    store.set_score(conn, key, 7, "good fit", scored_by="claude")
    assert row(conn, key)["label_score"] is None
    assert store.unprotected_scores(conn) == 1


def test_protect_labels_copies_the_score_and_the_reason(conn):
    key = seed(conn, "1")
    store.set_score(conn, key, 7, "good fit", scored_by="claude")
    assert store.protect_labels(conn) == 1
    r = row(conn, key)
    assert (r["label_score"], r["label_reason"]) == (7, "good fit")
    assert store.unprotected_scores(conn) == 0


def test_protect_labels_does_not_touch_an_existing_label(conn):
    key = seed(conn, "1")
    store.set_score(conn, key, 7, "claude said so", scored_by="claude")
    store.protect_labels(conn)
    store.set_score(conn, key, 4, "heuristic said so", scored_by="heuristic")
    assert store.protect_labels(conn) == 0
    r = row(conn, key)
    assert (r["label_score"], r["label_reason"]) == (7, "claude said so")


def test_a_heuristic_score_is_never_promoted_to_a_label(conn):
    key = seed(conn, "1")
    store.set_score(conn, key, 4, "matched java, aws", scored_by="heuristic")
    assert store.protect_labels(conn) == 0
    assert row(conn, key)["label_score"] is None
    assert store.unprotected_scores(conn) == 0


def test_a_score_from_before_the_column_existed_counts_as_a_label(conn):
    """NULL scored_by is the claude era, which is every row in the current corpus."""
    key = seed(conn, "1")
    store.set_score(conn, key, 7, "good fit")
    assert store.unprotected_scores(conn) == 1
    assert store.protect_labels(conn) == 1
    assert row(conn, key)["label_score"] == 7


def test_an_unscored_row_is_not_protected(conn):
    seed(conn, "1")
    assert store.protect_labels(conn) == 0
    assert store.unprotected_scores(conn) == 0


# ---------------------------------------------------------------- confidence in the ordering

JD = ("Requirements: 3 years of experience building backend services in java and spring "
      "boot on aws. You will own microservices end to end, work with kafka and dynamodb, "
      "and help with system design reviews. " * 6)


def seed_at(conn, company, req_id, score, description=None, title="Software Engineer II"):
    """One scored, still-open, un-actioned req at a named company. Returns its key."""
    job = {"company": company, "title": title, "req_id": req_id,
           "url": f"https://example.test/{company}/{req_id}", "source": "greenhouse",
           "description": description}
    store.upsert_job(conn, job)
    key = store.job_key(company, title, job["url"], req_id)
    store.set_score(conn, key, score, "reason", scored_by="claude")
    return key


def test_a_sighted_req_outranks_an_equally_scored_blind_one(conn):
    """
    The ordering item 6 exists for. Both score 8, so before this the tie broke on company and
    the blind row won on alphabet alone. "Zeta" is deliberately last alphabetically: if the
    sighted row still comes first, the JD flag is what ordered them and not the name.
    """
    assert len(JD) >= 1200
    blind = seed_at(conn, "Acme", "1", 8)
    sighted = seed_at(conn, "Zeta", "2", 8, description=JD)

    rows = store.promoted(conn, 7)
    assert [r["key"] for r in rows] == [sighted, blind]


def test_the_jd_flag_does_not_outrank_the_score(conn):
    """Score is still the primary key of the ordering; confidence only breaks its ties."""
    low_sighted = seed_at(conn, "Acme", "1", 7, description=JD)
    high_blind = seed_at(conn, "Zeta", "2", 9)
    assert [r["key"] for r in store.promoted(conn, 7)] == [high_blind, low_sighted]


def test_company_still_breaks_the_remaining_tie(conn):
    """Determinism. Two rows alike on score and on confidence must still order stably."""
    a = seed_at(conn, "Acme", "1", 8, description=JD)
    z = seed_at(conn, "Zeta", "2", 8, description=JD)
    assert [r["key"] for r in store.promoted(conn, 7)] == [a, z]


def test_promoted_exposes_the_has_jd_flag(conn):
    seed_at(conn, "Acme", "1", 8, description=JD)
    seed_at(conn, "Zeta", "2", 8)
    rows = store.promoted(conn, 7)
    assert [bool(r["has_jd"]) for r in rows] == [True, False]


def test_a_description_below_the_minimum_counts_as_blind(conn):
    """
    The 700-char rule, which is the whole point of JD_MIN_CHARS living in one place. A
    JavaScript-shell careers page yields a few hundred characters of nav and a cookie banner;
    treating that as a JD is worse than treating it as nothing, because it looks like evidence.
    """
    shell = "x" * (store.JD_MIN_CHARS - 1)
    seed_at(conn, "Acme", "1", 8, description=shell)
    assert bool(store.promoted(conn, 7)[0]["has_jd"]) is False
    assert store.has_jd({"description": shell}) is False
    assert store.has_jd({"description": "x" * store.JD_MIN_CHARS}) is True


def test_has_jd_reads_the_flag_when_the_row_carries_one(conn):
    seed_at(conn, "Acme", "1", 8, description=JD)
    assert store.has_jd(store.promoted(conn, 7)[0]) is True


def test_has_jd_falls_back_to_the_description_on_a_row_from_another_query(conn):
    # digest.py renders rows from three different queries, only one of which is promoted().
    key = seed_at(conn, "Acme", "1", 8, description=JD)
    plain = conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()
    with pytest.raises((IndexError, KeyError)):
        plain["has_jd"]
    assert store.has_jd(plain) is True


def test_has_jd_treats_a_missing_description_as_blind(conn):
    assert store.has_jd({}) is False
    assert store.has_jd({"description": None}) is False


def test_fetch_reads_the_same_jd_minimum_as_the_store(conn):
    # One number, several readers. If these drift, a row can be "enriched enough" for the
    # candidate query and still blind to the digest.
    import fetch
    assert fetch.JD_MIN_CHARS == store.JD_MIN_CHARS == 700


def test_promoted_still_filters_on_the_threshold_and_the_status(conn):
    """The ordering changed; the filters did not. This is the guard on that."""
    keep = seed_at(conn, "Acme", "1", 7, description=JD)
    seed_at(conn, "Acme", "2", 6, description=JD)                 # below the bar
    acted = seed_at(conn, "Acme", "3", 9, description=JD)
    conn.execute("UPDATE jobs SET status = 'applied' WHERE key = ?", (acted,))
    closed = seed_at(conn, "Acme", "4", 9, description=JD)
    conn.execute("UPDATE jobs SET still_open = 0 WHERE key = ?", (closed,))
    assert [r["key"] for r in store.promoted(conn, 7)] == [keep]


# ------------------------------------------------------------------- per-company cap

def rows_for(*companies):
    """Plain dicts, because cap_per_company only ever reads `company`."""
    return [{"company": c, "score": 9 - i} for i, c in enumerate(companies)]


def test_five_reqs_at_one_company_keep_two_and_defer_three():
    rows = rows_for("Acme", "Acme", "Acme", "Acme", "Acme")
    kept, over = store.cap_per_company(rows, 2)
    assert len(kept) == 2
    assert len(over) == 3
    # Nothing invented and nothing lost: the two halves reconstruct the input exactly.
    assert kept + over == rows


def test_the_cap_preserves_the_incoming_score_order_in_both_halves():
    # promoted() hands rows over already ordered by score, so the split must not re-sort.
    rows = rows_for("Acme", "Acme", "Acme", "Acme")
    kept, over = store.cap_per_company(rows, 2)
    assert [r["score"] for r in kept] == [9, 8]
    assert [r["score"] for r in over] == [7, 6]


def test_the_cap_counts_per_company_not_overall():
    rows = rows_for("Acme", "Bosch", "Acme", "Bosch", "Acme")
    kept, over = store.cap_per_company(rows, 2)
    assert [r["company"] for r in kept] == ["Acme", "Bosch", "Acme", "Bosch"]
    assert [r["company"] for r in over] == ["Acme"]


def test_a_company_under_the_cap_is_untouched():
    rows = rows_for("Acme", "Bosch", "Citrine")
    kept, over = store.cap_per_company(rows, 2)
    assert kept == rows
    assert over == []


def test_the_cap_groups_companies_case_and_space_insensitively():
    # The same employer arrives spelled differently from different boards, and a cap that
    # counted "Acme" and "acme " separately would not be a cap at all.
    rows = rows_for("Acme", "acme", " ACME ")
    kept, over = store.cap_per_company(rows, 2)
    assert len(kept) == 2 and len(over) == 1


def test_a_cap_of_zero_means_no_cap_rather_than_an_empty_list():
    """
    A config typo must not blank the action list. Reading 0 literally would put every promoted
    req into the overflow block, which looks like the pipeline broke.
    """
    rows = rows_for("Acme", "Acme", "Acme")
    kept, over = store.cap_per_company(rows, 0)
    assert kept == rows and over == []
    assert store.cap_per_company(rows, -1) == (rows, [])


def test_the_cap_defaults_to_two():
    rows = rows_for("Acme", "Acme", "Acme")
    assert store.cap_per_company(rows) == store.cap_per_company(rows, 2)
    assert store.COMPANY_CAP == 2


def test_the_cap_handles_an_empty_input_and_a_missing_company():
    assert store.cap_per_company([], 2) == ([], [])
    kept, over = store.cap_per_company([{"company": None}, {"company": None},
                                        {"company": None}], 2)
    assert len(kept) == 2 and len(over) == 1


def test_the_cap_does_not_mutate_the_list_it_is_given():
    rows = rows_for("Acme", "Acme", "Acme")
    before = list(rows)
    store.cap_per_company(rows, 2)
    assert rows == before


def test_the_cap_applied_to_promoted_keeps_the_highest_scoring_two(conn):
    """End to end: the ordering promoted() establishes is the priority the cap respects."""
    for i, score in enumerate([7, 9, 8, 7]):
        seed_at(conn, "Acme", str(i), score, description=JD)
    kept, over = store.cap_per_company(store.promoted(conn, 7), 2)
    assert [r["score"] for r in kept] == [9, 8]
    assert [r["score"] for r in over] == [7, 7]
