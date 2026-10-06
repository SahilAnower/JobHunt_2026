"""
URL -> ATS platform, which is how config/boards.yaml gets derived without probing anything.

A misclassification here is quiet and total: the company is recorded under the wrong platform
and its fetcher then returns nothing, which looks exactly like a company with no open reqs.
Each URL shape below is one real board from the seed list.

No network. classify() only parses the string.
"""

from __future__ import annotations

import pytest

import seed_boards


@pytest.mark.parametrize("url, expected", [
    # Greenhouse's slug is the first path segment on both of its host spellings.
    ("https://job-boards.greenhouse.io/cloudflare/jobs/7831810",
     ("greenhouse", {"slug": "cloudflare"})),
    # Ashby slugs are case-sensitive, so the case must survive classification.
    ("https://jobs.ashbyhq.com/Confluent/abc-123",
     ("ashby", {"slug": "Confluent"})),
    ("https://jobs.lever.co/mindtickle/uuid",
     ("lever", {"slug": "mindtickle"})),
    # Workday carries three identifiers, and the "en-US" locale segment has to be skipped to
    # find the site name. Submitting the locale as the site is an HTTP 404 on every request.
    ("https://autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/Bangalore/x_123",
     ("workday", {"tenant": "autodesk", "instance": "wd1", "site": "Ext"})),
    ("https://careers.smartrecruiters.com/Experian/123",
     ("smartrecruiters", {"slug": "Experian"})),
    # Avature is white-labelled onto the employer's host, so jobs.ea.com gives nothing away.
    # The path is the tell, and the portal segment is part of every later request.
    ("https://jobs.ea.com/en_US/careers/JobDetail/slug/216197",
     ("avature", {"host": "jobs.ea.com", "portal": "en_US"})),
    # Radancy is also host-agnostic and matched purely on the /job/<city>/<title>/<org>/<req>
    # path shape.
    ("https://careers.netapp.com/job/bengaluru/software-engineer/27600/101302054944",
     ("radancy", {"host": "careers.netapp.com"})),
    # ...and the city/title segments are literal dashes on a direct link, which is why the
    # rule does not constrain them beyond "not a slash".
    ("https://careers.netapp.com/job/-/-/27600/97816764496",
     ("radancy", {"host": "careers.netapp.com"})),
    ("https://ats.rippling.com/foo/jobs/uuid",
     ("rippling", {"slug": "foo"})),
    # No rule matches, so the company runs its own careers page. Recorded rather than guessed
    # at, because own_site means "cannot be polled, needs an email alert".
    ("https://www.phonepe.com/careers/job-openings/",
     ("own_site", {"host": "www.phonepe.com"})),
])
def test_classify(url, expected):
    assert seed_boards.classify(url) == expected


def test_a_bare_platform_host_classifies_with_no_identity():
    # match_smartrecruiters returns a None ident when there is no path segment to read. main()
    # defends against it with `**(ident or {})`, so this shape has to stay representable.
    assert seed_boards.classify("https://careers.smartrecruiters.com/") == (
        "smartrecruiters", None)


def test_rippling_is_not_pollable():
    # classify recognising a platform does not mean fetch.py can read it. Rippling and
    # own_site have no documented public job-board API, so they are deliberately absent here.
    assert "rippling" not in seed_boards.POLLABLE
    assert "own_site" not in seed_boards.POLLABLE
    assert "greenhouse" in seed_boards.POLLABLE
