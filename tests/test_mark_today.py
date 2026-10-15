"""
mark_today reads the digest and hands keys to mark.py. Its failure mode was silence:
an unresolvable URL produced a mark.py call that matched zero rows, mark.py exited 1,
and the script neither noticed nor said so, so the owner believed a req was marked
when it was not.
"""
import sqlite3

import pytest

import mark_today as mt


# --------------------------------------------------------------------------- id fallback

@pytest.mark.parametrize("url,want", [
    # Greenhouse and friends put the req in the query string.
    ("https://boards.greenhouse.io/cloudflare/jobs/8259402?gh_jid=8259402", "8259402"),
    ("https://www.mongodb.com/careers/job/?gh_jid=7597723", "7597723"),
    # Greenhouse without the query string falls back to the path tail.
    ("https://job-boards.greenhouse.io/gitlab/jobs/8775507002", "8775507002"),
    # Instahyre encodes it in the slug.
    ("https://www.instahyre.com/job-446794-software-engineer-at-blue-yonder/", "ih-446794"),
    # iCIMS ends the path with "/job", which used to yield the literal id "job".
    ("https://globalcareers-atlassian.icims.com/jobs/25540/backend-engineer/job?mode=apply",
     "25540"),
    # Oracle keeps it as the last path segment.
    ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/210790233",
     "210790233"),
    # Workday separates the req with an underscore.
    ("https://expedia.wd108.myworkdayjobs.com/en-US/search/job/India/SDE-II_R-110193",
     "R-110193"),
    # ...and sometimes appends a dedup suffix that is not part of the req id. The stored
    # key is R171461, so keeping the "-1" matched nothing at all.
    ("https://adobe.wd5.myworkdayjobs.com/en-US/x/job/Bangalore/Computer-Scientist_R171461-1",
     "R171461"),
])
def test_ident_from_url(url, want):
    assert mt.ident_from_url(url) == want


# --------------------------------------------------------------------------- key resolution

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("create table jobs (key text, source text, status text, url text)")
    c.executemany("insert into jobs values (?,?,?,?)", [
        ("adobe::R171461", "workday", "scored", "https://adobe.example/job/x_R171461-1"),
        ("atlassian::25540", "atlassian", "scored", "https://atl.example/jobs/25540/be/job"),
        # A hand-seeded row and the row the fetcher later found, sharing one URL.
        ("coinbase::abc123", "seed", "scored", "https://cb.example/p/8165441?gh_jid=8165441"),
        ("coinbase::8165441", "greenhouse", "scored", "https://cb.example/p/8165441?gh_jid=8165441"),
        # Two seeded rows on one URL: genuinely ambiguous, must not be guessed.
        ("meta::aaa", "seed", "scored", "https://meta.example/job/1"),
        ("meta::bbb", "seed", "scored", "https://meta.example/job/1"),
    ])
    return c


def test_a_single_row_resolves_exactly(conn):
    key, note = mt.key_from_url(conn, "https://adobe.example/job/x_R171461-1")
    assert key == "adobe::R171461"
    assert note == ""


def test_an_icims_url_resolves_even_though_its_path_ends_in_job(conn):
    # The id fallback would have produced "job" here and been skipped outright.
    key, _ = mt.key_from_url(conn, "https://atl.example/jobs/25540/be/job")
    assert key == "atlassian::25540"


def test_the_fetched_row_wins_over_a_seeded_duplicate(conn):
    key, note = mt.key_from_url(conn, "https://cb.example/p/8165441?gh_jid=8165441")
    assert key == "coinbase::8165441"
    assert "seeded row" in note


def test_a_genuinely_ambiguous_url_is_refused_rather_than_guessed(conn):
    key, note = mt.key_from_url(conn, "https://meta.example/job/1")
    assert key is None
    assert "2 rows" in note


def test_an_unknown_url_is_refused_so_the_caller_can_fall_back(conn):
    key, note = mt.key_from_url(conn, "https://nowhere.example/job/9")
    assert key is None
    assert "not in the database" in note


# --------------------------------------------------------------------------- digest parsing

DIGEST = """# jobhunt digest — 2026-10-15

## Do today

- **Blue Yonder — [Software Engineer](https://a.example/1)** · 8/10 · home
  - draft: `outbox/x.md`
  - warm path: cold, find a human first
- **Swiggy — [SDE 1](https://a.example/2)** · 8/10 · remote ⚠ no JD
  - draft: `outbox/y.md`

## Scored 7+ and not yet acted on (1)

| Score | Company | Role | Where | Lane | Time to offer |
|---|---|---|---|---|---|
| 7 | Groww | [SDE II](https://a.example/3) | relocate | A | Brisk loop. |

- **Groww — SDE II**: matched java, aws; in band

<details><summary>More open reqs at companies already in today's list (1)</summary>

At most 2 req(s) per company reach the lists above, so these are deferred rather than dropped.

- 8/10 JP Morgan Chase — [SE III](https://a.example/capped)

</details>

## In motion (2)

| Company | Role | Stage | Last touch |
|---|---|---|---|
| Adobe | [MTS II](https://a.example/inmotion) | applied | 0d ago |
"""


def test_only_the_do_today_bullets_are_collected_by_default():
    assert mt.urls_from_digest(DIGEST, with_7plus=False) == [
        "https://a.example/1", "https://a.example/2"]


def test_the_7plus_table_is_added_on_request_but_not_its_reason_bullets():
    got = mt.urls_from_digest(DIGEST, with_7plus=True)
    assert got == ["https://a.example/1", "https://a.example/2", "https://a.example/3"]


def test_reqs_deferred_by_the_company_cap_are_never_marked():
    # The cap exists to keep these out of today's work. Marking them from the digest
    # would record an application the owner never made.
    both = mt.urls_from_digest(DIGEST, with_7plus=True)
    assert "https://a.example/capped" not in both


def test_in_motion_rows_are_outside_every_collected_section():
    both = mt.urls_from_digest(DIGEST, with_7plus=True)
    assert "https://a.example/inmotion" not in both


def test_a_blind_score_marker_does_not_break_the_bullet_match():
    # Item 6 appends a marker to the bullet; the URL must still be found.
    assert "https://a.example/2" in mt.urls_from_digest(DIGEST, with_7plus=False)


def test_a_missing_do_today_heading_yields_nothing_rather_than_raising():
    assert mt.urls_from_digest("# digest\n\n## In motion (0)\n", with_7plus=True) == []
