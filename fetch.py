#!/usr/bin/env python3
"""
Pull requisitions from the public ATS JSON APIs in config/boards.yaml, filter them against
config/profile.yaml, and write survivors to the store.

Four platforms, all unauthenticated:

    greenhouse  GET  boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
    ashby       GET  api.ashbyhq.com/posting-api/job-board/<slug>
    lever       GET  api.lever.co/v0/postings/<slug>?mode=json
    workday     POST <tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs

Two gates run over every posting, in this order:

    1. title   exclude_titles first, then include_titles. The ceiling matters more than the
               floor here: these boards are dominated by Senior/Staff/Principal reqs, and a
               profile capped at SDE II drops most of what it sees.
    2. geo     require_india_signal drops anything with no India signal at all, rather than
               trying to enumerate every foreign city. See the note in profile.yaml.

    python3 fetch.py --dry-run           # show what would land, write nothing
    python3 fetch.py                     # poll every enabled board, write to the store
    python3 fetch.py --company Cloudflare
    python3 fetch.py --seed              # load the known-live reqs from config/seed.yaml
    python3 fetch.py --dry-run --show-dropped   # why each posting was rejected
"""

from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

import jobprofile
import store

ROOT = Path(__file__).resolve().parent
BOARDS = ROOT / "config" / "boards.yaml"
SEED = ROOT / "config" / "seed.yaml"

TIMEOUT = 25
# Both were effectively sized for the original 16 boards. The list is 37 now, and every one of
# these is a socket waiting on someone else's server rather than local work, so the cap was
# limiting throughput for no benefit.
BOARD_WORKERS = 12
ENRICH_WORKERS = 8
MAX_DESC = 6000          # plenty for the scorer; keeps the db small and prompts cheap
WORKDAY_PAGES = 4        # 20 reqs per page
WORKDAY_INDIA_PAGES = 12  # once the India facet is applied the whole set fits in a few pages
# Avature offers no location filter, so the whole board is read: 20 per page, and EA runs ~340
# reqs. The ceiling is generous because stopping early silently loses reqs rather than erroring.
AVATURE_PAGES = 30
# Shorter than TIMEOUT: a closure probe runs once per absent req and a slow host should not
# hold up the run. No answer just leaves the miss counter to decide.
CLOSURE_TIMEOUT = 10
# Instahyre pins its page size at 35 whatever you ask for, and carries ~12,900 jobs. 20 pages is
# 700 of the newest reqs across the software functions — enough to catch what appeared since the
# last run without draining a board whose tail is mostly staffing posts.
INSTAHYRE_PAGES = 20
ORACLE_PAGES = 24        # 25 reqs per page, per location — Uber has to be read unfiltered


# ----------------------------------------------------------------------------- http

# Some career hosts serve a chain that macOS's /etc/ssl/cert.pem does not complete —
# careers.netapp.com is the one that surfaced it, failing with CERTIFICATE_VERIFY_FAILED while
# curl against the same URL returned 200. certifi carries the intermediates, so prefer it and
# fall back to the system store when certifi is not installed. Never disable verification: a
# silent downgrade to unverified TLS is a worse outcome than a board that cannot be polled.
try:
    import certifi
    _TLS = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _TLS = ssl.create_default_context()


def _get(url: str, ua: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_TLS) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_text(url: str, ua: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_TLS) as r:
        return r.read().decode("utf-8", "replace")


def url_is_live(url: str, ua: str) -> bool | None:
    """
    Ask the posting URL directly whether the req still exists. True = still served,
    False = definitely gone, None = no usable answer, decide some other way.

    This is the only direct evidence available about closure. Everything else is inference from
    a board's filtered list, and that inference has been wrong: two reqs written off as pulled
    were answering 200 the whole time.

    Read the status codes carefully, because most of them say nothing about the job:
      404 / 410  the posting is gone. The one real "closed" signal.
      200        still being served.
      403 / 429  the site is blocking a script. Says nothing about the req.
      5xx        their problem, not an answer.
    A HEAD is enough and avoids pulling the body, but plenty of career sites answer 405 to
    HEAD, so fall back to a GET on anything inconclusive.

    Known limitation, and it is the safe direction: some platforms soft-404. Greenhouse answers
    200 for a job id that never existed, serving a 280 KB fallback board page. So a dead
    Greenhouse req reads as live here and the probe grants it a reprieve — costing one extra
    poll before the miss counter closes it. Deliberately not fixed by sniffing the body for
    "no longer available": that string varies per platform and per locale, and a wrong guess
    there closes live reqs, which is the failure this whole function exists to prevent.
    """
    def probe(method: str) -> int | None:
        req = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"})
        try:
            with urllib.request.urlopen(req, timeout=CLOSURE_TIMEOUT, context=_TLS) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001  — DNS, TLS, timeout: no answer either way
            return None

    code = probe("HEAD")
    if code in (405, 501, None):
        code = probe("GET")
    if code in (404, 410):
        return False
    if code is not None and 200 <= code < 400:
        return True
    return None


def _post(url: str, payload: dict, ua: str) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "User-Agent": ua,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_TLS) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()[:MAX_DESC]


# ----------------------------------------------------------------------------- gates

def _terms(cfg, key) -> list[str]:
    return [str(t).lower() for t in (cfg.get(key) or [])]


def _term_re(term: str) -> str:
    """
    Whole-word match, with a trailing period made optional so one entry covers both "Sr" and
    "Sr.". Boundaries on both sides are what keep "sr" from firing on "SRE" and "lead" from
    firing on "Leader" (list "leader" separately if you want it).
    """
    core = re.escape(term[:-1]) if term.endswith(".") else re.escape(term)
    return rf"(?<![a-z0-9]){core}\.?(?![a-z0-9])"


def title_verdict(title: str, filters: dict) -> tuple[bool, str]:
    """(kept, reason). Exclusions win over inclusions — see the profile comment."""
    t = f" {title.lower()} "

    # Some legitimate in-band titles contain a word that is otherwise a seniority signal:
    # "Member of Technical Staff II" is an IC band roughly equal to SDE II, but it carries
    # "staff". Blank the phrase out rather than whitelisting the title, so the ceiling still
    # applies to what is left — "Sr Member of Technical Staff" loses the phrase and is then
    # caught by "sr".
    gated = t
    for phrase in _terms(filters, "exclude_overrides"):
        gated = gated.replace(phrase, " ")

    for term in _terms(filters, "exclude_titles"):
        if re.search(_term_re(term), gated):
            return False, f"excluded title term: {term}"
    inc = _terms(filters, "include_titles")
    if not inc:
        return True, "no include list"
    for term in inc:
        if term in t:
            return True, f"matched: {term}"
    return False, "no include_titles match"


def lane_of(title: str, filters: dict) -> str:
    t = title.lower()
    return "A" if any(x in t for x in _terms(filters, "lane_a_titles")) else "B"


def geo_verdict(location: str, geo: dict) -> tuple[str | None, str]:
    """
    (bucket, reason). bucket None means drop.

    Checked most-specific first: a "Hyderabad, India" req is `home`, not `relocate`.
    """
    loc = (location or "").lower()
    if not loc:
        return (None, "no location") if geo.get("require_india_signal") else ("unclear", "empty")

    def hit(key):
        # Whole-word, not substring. A plain `t in loc` matched "india" inside "Indianapolis"
        # and "Indiana", so a US Midwest req read as an India one — and "Remote - Indiana" read
        # as the most attractive bucket there is, remote-plus-India. The same boundary check
        # already existed in india_facet_values() for the Workday and Oracle location facets,
        # and the README's "Indiana contains india" note describes that one; it was never wired
        # into this gate, which is the one every req passes through.
        #
        # Masked until now because Workday and Oracle narrow to India server-side, so no Indiana
        # req ever reached here from them, and the boards with no server-side filter happened not
        # to have posted an in-band Midwest role yet.
        #
        # Applied to every geo list rather than just country_terms, because the trap is not
        # specific to India: "ncr" is in relocation_terms and is a substring of "Concrete",
        # which is a real town in Washington. Replaying all 636 stored reqs through both the old
        # and new matcher produced identical verdicts, so this corrects a latent fault without
        # moving the filter the tracked applications came through.
        return next((t for t in _terms(geo, key) if re.search(_term_re(t), loc)), None)

    # Precedence is deliberate: no-relocation beats remote, and remote beats relocation,
    # because that is the order the candidate would actually prefer the outcomes.
    if (t := hit("home_terms")):
        return "home", t

    remote_t, country_t = hit("remote_terms"), hit("country_terms")
    if remote_t and country_t:
        return "remote", f"{remote_t} + {country_t}"
    if remote_t and loc.strip() in _terms(geo, "remote_terms"):
        # The whole field is just "Remote" — country genuinely unstated, worth surfacing.
        return "remote", "remote, country unstated"

    if (t := hit("relocation_terms")):
        return "relocate", t
    if country_t:
        return "india", country_t
    if geo.get("require_india_signal"):
        return None, "no India signal"
    return "unclear", "kept, location unclear"


# -------------------------------------------------------------------------- fetchers

def fetch_greenhouse(board: dict, ua: str) -> list[dict]:
    slug = board["slug"]
    data = _get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", ua
    )
    out = []
    for j in data.get("jobs", []):
        out.append(
            {
                "company": board["company"],
                "title": (j.get("title") or "").strip(),
                "location": _greenhouse_location(j),
                "url": j.get("absolute_url"),
                "source": "greenhouse",
                "req_id": str(j.get("id") or ""),
                "posted_at": (j.get("updated_at") or j.get("first_published") or "")[:10],
                "description": strip_html(j.get("content")),
            }
        )
    return out


def _greenhouse_location(j: dict) -> str:
    """
    Greenhouse's `location.name` is not reliably a place. Several boards (Cloudflare is the
    example that bit us) put a work arrangement there — "In-Office", "Hybrid" — and keep the
    real city in `offices[]` and a "Job Posting Location" metadata field. Both only appear
    with ?content=true, so merge all three and let the geo gate read the union.
    """
    parts = [((j.get("location") or {}).get("name") or "").strip()]
    parts += [(o.get("name") or "").strip() for o in (j.get("offices") or [])]
    for m in j.get("metadata") or []:
        if "location" not in (m.get("name") or "").lower():
            continue
        val = m.get("value")
        if isinstance(val, list):
            parts += [str(v).strip() for v in val]
        elif val:
            parts.append(str(val).strip())
    seen, uniq = set(), []
    for p in parts:
        k = p.lower()
        if p and k not in seen:
            seen.add(k)
            uniq.append(p)
    return " ; ".join(uniq)


def fetch_ashby(board: dict, ua: str) -> list[dict]:
    slug = board["slug"]          # case-sensitive
    data = _get(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", ua
    )
    out = []
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""]
        locs += [str(x) for x in (j.get("secondaryLocations") or [])]
        out.append(
            {
                "company": board["company"],
                "title": (j.get("title") or "").strip(),
                "location": " ; ".join([x for x in locs if x]),
                "url": j.get("jobUrl"),
                "source": "ashby",
                "req_id": str(j.get("id") or ""),
                "posted_at": (j.get("publishedAt") or "")[:10],
                "description": strip_html(j.get("descriptionHtml") or j.get("descriptionPlain")),
            }
        )
    return out


def fetch_lever(board: dict, ua: str) -> list[dict]:
    slug = board["slug"]
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json", ua)
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        out.append(
            {
                "company": board["company"],
                "title": (j.get("text") or "").strip(),
                "location": cats.get("location") or "",
                "url": j.get("hostedUrl"),
                "source": "lever",
                "req_id": str(j.get("id") or ""),
                "posted_at": "",
                "description": strip_html(j.get("descriptionPlain") or j.get("description")),
            }
        )
    return out


def india_facet_values(facets: object, geo: dict | None = None) -> dict[str, list[str]]:
    """
    Find the facet value ids that mean "in India" on a board we have never seen before, keyed by
    the facet parameter that owns them.

    The big enterprise boards carry thousands of reqs, so paging them unfiltered and gating on
    geography afterwards would read 7,000 postings to keep four. They all expose a location
    facet instead, but the value ids are opaque GUIDs that differ per tenant, and the facet is
    sometimes country-level ("India") and sometimes city-level ("Pune, India"). So rather than
    hardcode an id per company, walk whatever facet tree came back and match on the descriptor.

    Workday nests facets: `locationMainGroup` holds a `locationCountry` facet which holds the
    values. The id has to be submitted under the *innermost* enclosing facetParameter, so the
    walk carries that name down with it. Submitting it under the outer one is an HTTP 400.

    The other trap: "Indiana" and "Remote - Indiana" contain "india". A trailing-boundary check
    is what keeps Indianapolis out of an India-only search.
    """
    cities = set(_terms(geo or {}, "relocation_terms")) | set(_terms(geo or {}, "home_terms"))
    rx = re.compile(r"(?<![a-z])india(?![a-z])", re.I)
    found: dict[str, list[str]] = {}

    def walk(node: object, param: str) -> None:
        if isinstance(node, dict):
            param = str(node.get("facetParameter") or param)
            desc = str(node.get("descriptor") or node.get("Name") or node.get("name") or "")
            ident = node.get("id") or node.get("Id")
            low = desc.lower()
            if ident and (rx.search(low) or any(c in low for c in cities)):
                ids = found.setdefault(param, [])
                if str(ident) not in ids:
                    ids.append(str(ident))
            for v in node.values():
                walk(v, param)
        elif isinstance(node, list):
            for v in node:
                walk(v, param)

    walk(facets, "")
    return found


def fetch_workday(board: dict, ua: str) -> list[dict]:
    """
    Workday's CXS endpoint is a POST with an offset/limit body. It returns 20 per page and a
    `total`, so page until we have them all or hit WORKDAY_PAGES. The list response carries
    no JD text; `externalPath` gives the detail URL if you want to read one by hand.

    One free probe first, to read the location facet and narrow the search to India before
    paging. Salesforce is 1,481 reqs and 121 of them are in India; without the facet, four
    pages of 20 would be a random sample of the wrong 1,360.
    """
    tenant, inst, site = board["tenant"], board.get("instance", "wd1"), board["site"]
    base = f"https://{tenant}.{inst}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    geo = board.get("_geo") or {}

    applied: dict[str, list[str]] = {}
    pages = WORKDAY_PAGES
    try:
        probe = _post(f"{base}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0,
                                       "searchText": ""}, ua)
        applied = india_facet_values(probe.get("facets") or [], geo)
        if applied:
            # The filtered set is small, so it is cheap to read all of it.
            pages = WORKDAY_INDIA_PAGES
    except Exception:  # noqa: BLE001 - an unfacetable board still gets the plain paged read
        pass

    out, offset, total = [], 0, None
    for _ in range(pages):
        data = _post(
            f"{base}/jobs",
            {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": ""},
            ua,
        )
        # Workday reports `total` on the first page and 0 on every page after it. Trusting the
        # later value stops the read dead at 40 reqs, which silently truncated Adobe from 109.
        if total is None:
            total = data.get("total") or 0
        posts = data.get("jobPostings") or []
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath") or ""
            out.append(
                {
                    "company": board["company"],
                    "title": (j.get("title") or "").strip(),
                    "location": j.get("locationsText") or j.get("shortLocationsText") or "",
                    "url": f"https://{tenant}.{inst}.myworkdayjobs.com/en-US/{site}{path}",
                    "source": "workday",
                    "req_id": str(j.get("bulletFields", [""])[0] or path.rsplit("_", 1)[-1]),
                    "posted_at": "",
                    "description": "",
                }
            )
        offset += 20
        if offset >= total:
            break
        time.sleep(0.3)
    return out


def _orc_finder(site: str, limit: int, offset: int, loc_id: str = "") -> str:
    parts = [f"findReqs;siteNumber={site}", f"limit={limit}", f"offset={offset}",
             "sortBy=POSTING_DATES_DESC"]
    if loc_id:
        # ORC wants the id in both places: one selects the facet, the other filters the search.
        parts += [f"locationId={loc_id}", f"selectedLocationsFacet={loc_id}"]
    return ";".join([parts[0]]) + "," + ",".join(parts[1:])


def fetch_oracle(board: dict, ua: str) -> list[dict]:
    """
    Oracle Recruiting Cloud, which Oracle and JP Morgan both run. The public endpoint is
    `recruitingCEJobRequisitions` with a `finder` query, and it returns a `locationsFacet` in
    every response, so the India id can be read off the first call rather than configured.

    JP Morgan posts 7,415 reqs worldwide. Filtering server-side is not an optimisation here,
    it is the difference between the fetcher working and the fetcher being useless.
    """
    host, site = board["host"], board.get("site", "CX")
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    def call(finder: str) -> dict:
        # `expand` is not optional: without it the response carries TotalJobsCount and an empty
        # requisitionList, which looks exactly like a board with no jobs on it.
        url = (f"{api}?onlyData=true&expand=requisitionList.secondaryLocations&finder="
               + urllib.parse.quote(finder, safe=";,="))
        return _get(url, ua)["items"][0]

    ids: list[str] = []
    try:
        by_param = india_facet_values(call(_orc_finder(site, 1, 0)).get("locationsFacet") or [],
                                      board.get("_geo") or {})
        ids = [i for v in by_param.values() for i in v]
    except Exception:  # noqa: BLE001
        pass

    # The facet only lists the largest locations, so a company with reqs thinly spread across
    # India can show no India value at all — Uber does exactly this. In that case fall back to
    # reading the board unfiltered and let the geography gate sort it out. `[""]` is the
    # unfiltered pass; a real id list runs one pass per location and merges.
    out: list[dict] = []
    seen: set[str] = set()
    for loc_id in (ids or [""]):
        offset = 0
        for _ in range(ORACLE_PAGES):
            item = call(_orc_finder(site, 25, offset, loc_id))
            rows = item.get("requisitionList") or []
            if not rows:
                break
            for r in rows:
                rid = str(r.get("Id") or "")
                if rid in seen:
                    continue
                seen.add(rid)
                out.append({
                    "company": board["company"],
                    "title": (r.get("Title") or "").strip(),
                    "location": r.get("PrimaryLocation") or "",
                    "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                    "source": "oracle",
                    "req_id": rid,
                    "posted_at": (r.get("PostedDate") or "")[:10],
                    "description": "",
                })
            offset += 25
            if offset >= (item.get("TotalJobsCount") or 0):
                break
            time.sleep(0.3)
    return out


def fetch_atlassian(board: dict, ua: str) -> list[dict]:
    """
    Atlassian publishes its whole req list as one JSON array from its own marketing site. It is
    the only company on the list with a bespoke endpoint worth a bespoke fetcher, and it has no
    location filter, so the geography gate does the work downstream.
    """
    rows = _get("https://www.atlassian.com/endpoint/careers/listings", ua)
    out = []
    for r in rows if isinstance(rows, list) else []:
        post = r.get("portalJobPost") or {}
        rid = str(r.get("id") or post.get("id") or "")
        loc = r.get("locations") or r.get("location") or ""
        if isinstance(loc, list):
            loc = " ; ".join(str(x) for x in loc)
        # The endpoint splits the JD across three fields and has no `description`, so join them
        # rather than let a req with a perfectly good JD get scored blind.
        jd = " ".join(strip_html(r.get(k)) for k in ("overview", "responsibilities",
                                                     "qualifications") if r.get(k))
        out.append({
            "company": board["company"],
            "title": (r.get("title") or "").strip(),
            "location": str(loc),
            "url": r.get("applyUrl") or post.get("portalUrl") or "",
            "source": "atlassian",
            "req_id": rid,
            "posted_at": str(post.get("updatedDate") or "")[:10],
            "description": jd.strip(),
        })
    return out


def _sr_url(posting: dict) -> str:
    node: object = posting.get("ref")
    for key in ("jobAd", "publishedUrl"):
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node if isinstance(node, str) else ""


def fetch_smartrecruiters(board: dict, ua: str) -> list[dict]:
    """
    SmartRecruiters is the friendliest of the lot: it takes a `country` filter directly, so the
    India narrowing needs no facet discovery. The list response has no JD text, but the posting
    URL is server-rendered, so `--enrich` recovers it.
    """
    slug = board["slug"]
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    out, offset = [], 0
    for _ in range(4):
        data = _get(f"{base}?limit=100&offset={offset}&country=in", ua)
        rows = data.get("content") or []
        if not rows:
            break
        for r in rows:
            loc = r.get("location") or {}
            parts = [loc.get("city"), loc.get("region"), loc.get("country")]
            if loc.get("remote"):
                parts.append("Remote")
            rid = str(r.get("id") or "")
            out.append({
                "company": board["company"],
                "title": (r.get("name") or "").strip(),
                "location": ", ".join(p for p in parts if p),
                # `ref` and `ref.jobAd` are each an object on some postings and a bare URL
                # string on others, so dig with a guard at every level rather than assume.
                "url": _sr_url(r) or f"https://jobs.smartrecruiters.com/{slug}/{rid}",
                "source": "smartrecruiters",
                "req_id": rid,
                "posted_at": str(r.get("releasedDate") or "")[:10],
                "description": "",
            })
        offset += 100
        if offset >= (data.get("totalFound") or 0):
            break
        time.sleep(0.3)
    return out


def _avature_location(subtitle_html: str) -> str:
    """
    Pull every location out of an Avature result subtitle, which is bullet-separated and looks
    like:

        Stockholm, Sweden • Bucharest, Romania • Role ID 216197 • Regular Employee • CT - Frostbite

    A req can list up to five cities, and the India one is not always first: 98 of EA's 336 reqs
    are multi-location. Keeping only the leading segment loses those, which is the same mistake
    Greenhouse's `location.name` invites — so, as there, merge them all and let the geo gate read
    the union.

    Everything from the "Role ID" marker onward is metadata (req id, employment type, studio) and
    is dropped, because feeding a studio name like "CT - Frostbite" to the geo gate is noise. All
    336 subtitles carry that marker; if one ever does not, fall back to the first segment only,
    which is the safe half of the guess.
    """
    if not subtitle_html:
        return ""
    flat = html.unescape(re.sub(r"\s+", " ", strip_html(subtitle_html))).strip()
    segs = [s.strip(" ;,") for s in re.split(r"\s*[•·|]\s*", flat) if s.strip(" ;,")]
    cut = next((i for i, s in enumerate(segs) if re.match(r"role\s*id\b", s, re.I)), None)
    places = segs[:cut] if cut is not None else segs[:1]
    return " ; ".join(places)


def fetch_avature(board: dict, ua: str) -> list[dict]:
    """
    Avature, which EA runs white-labelled at jobs.ea.com. The only fetcher here that reads HTML
    instead of JSON, because this platform genuinely publishes no machine-readable feed: on EA's
    portal, JobRss, SearchJobsRss, JobSearchRss, /api/jobs and /careers/api/jobs all 404, and
    `?jobRss=1` answers with the same HTML page.

    What makes it workable anyway is that the search page is server-rendered — one
    `<article class="article--result">` per req, carrying an absolute JobDetail link, the title,
    and a location subtitle. That is a real distinction from a JS shell like Gap's: there is no
    guessing involved, the data is in the markup.

    Be honest about the tradeoff this carries. Every other board here is pinned to a documented
    JSON contract, while this one depends on Avature's CSS class names. A theme rename breaks it,
    and it breaks *quietly* — the selectors stop matching and the board simply reports 0 reqs,
    which is indistinguishable from "nothing is posted". That is why it raises on a page that
    parses to nothing while still advertising results, instead of returning an empty list and
    letting closure detection mark every EA req as filled.

    Paging is `?jobOffset=N` in steps of 20. There is no location facet in the URL, so this
    reads the whole board and leaves India filtering to the geo gate.
    """
    host = board.get("host") or "jobs.ea.com"
    portal = board.get("portal") or "en_US"
    base = f"https://{host}/{portal}/careers/SearchJobs"

    card_rx = re.compile(r'<article class="article article--result')
    link_rx = re.compile(
        r'href="(https?://[^"]*?/careers/JobDetail/[^"]+)"[^>]*>\s*(.*?)\s*</a>', re.S)
    sub_rx = re.compile(r'article__header__text__subtitle">(.*?)</div>', re.S)

    out: list[dict] = []
    seen: set[str] = set()
    for page in range(AVATURE_PAGES):
        offset = page * 20
        url = base if offset == 0 else f"{base}?jobOffset={offset}"
        try:
            raw = _get_text(url, ua)
        except urllib.error.HTTPError as e:
            if page == 0:
                raise
            break  # a later page failing is the end of the list, not a broken board
        cards = card_rx.split(raw)[1:]
        if not cards:
            if page == 0:
                # Advertised results but nothing parsed: the markup changed. Say so loudly.
                raise RuntimeError(
                    f"avature: no job cards parsed from {url} — the portal markup has probably "
                    "changed, so fetch_avature's selectors need updating")
            break
        added = 0
        for card in cards:
            m = link_rx.search(card)
            if not m:
                continue
            link = html.unescape(m.group(1))
            if link in seen:
                continue
            seen.add(link)
            title = html.unescape(re.sub(r"\s+", " ", strip_html(m.group(2)))).strip()
            sm = sub_rx.search(card)
            loc = _avature_location(sm.group(1) if sm else "")
            # Avature ends the JobDetail path with the numeric req id.
            rid = link.rstrip("/").split("/")[-1].split("?")[0]
            out.append({
                "company": board["company"],
                "title": title,
                "location": loc,
                "url": link,
                "source": "avature",
                "req_id": rid if rid.isdigit() else "",
                "posted_at": "",
                "description": "",
            })
            added += 1
        if not added:
            break
        time.sleep(0.3)
    return out


def fetch_instahyre(board: dict, ua: str) -> list[dict]:
    """
    Instahyre, and the first aggregator in here rather than an employer's own board. That
    difference matters in two ways.

    First, `company` comes from each row's `employer.company_name`, not from the board entry.
    Every other fetcher returns reqs for one company; this one returns reqs for hundreds, which
    is the point — it reaches employers that are nowhere in config/seed.yaml. The board's own
    name ("Instahyre") never appears on a req.

    Second, it is allowed. Its robots.txt is `User-agent: *` with no Disallow at all, and
    `/api/v1/job_search` is the same public JSON endpoint its own site reads. That is not true of
    the obvious alternatives: Naukri's robots.txt names Claude-User, claudebot and
    Claude-SearchBot explicitly under `Disallow: /`, and Cutshort and Wellfound both disallow the
    job-detail paths. Those three are therefore deliberately not polled, and should not be added.

    Volume is the real design problem, not access. The board carries ~12,900 live jobs, ~4,100
    under Backend Development alone, and roughly 39% of those clear the title gate — enough to
    bury the digest and spend a fortune in scoring calls on reqs the comp floor would reject
    anyway. So:

      - only the software job functions are requested, via `job_functions`
      - paging stops at INSTAHYRE_PAGES, newest first, rather than draining the board
      - `limit` is pinned at 35 because the API ignores anything larger

    Quality skews lower than an employer board: expect staffing firms and unfunded startups,
    which is what `exclusions.companies` in the profile is for. The IT-services names are already
    listed there; the agency names this surfaces have been added alongside them.
    """
    # The ids must be repeated as separate params. A comma-joined `job_functions=10,1,76` is an
    # HTTP 400, which is easy to misread as the endpoint being closed off rather than the
    # argument being malformed.
    funcs = str(board.get("job_functions") or "10,1,76")   # Backend, Full-Stack, Other Software
    qs = "&".join(f"job_functions={f.strip()}" for f in funcs.split(",") if f.strip())
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(INSTAHYRE_PAGES):
        url = (f"https://www.instahyre.com/api/v1/job_search"
               f"?limit=35&offset={page * 35}&{qs}")
        try:
            data = _get(url, ua)
        except urllib.error.HTTPError:
            if page == 0:
                raise
            break
        rows = (data or {}).get("objects") or []
        if not rows:
            break
        for j in rows:
            rid = str(j.get("id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            emp = j.get("employer") or {}
            company = (emp.get("company_name") or "").strip()
            if not company:
                continue          # without an employer name the req cannot be judged or gated
            out.append({
                "company": company,
                "title": (j.get("title") or "").strip(),
                # Comma-separated and genuinely mixed: "Bangalore,Gurgaon", "Work From Home",
                # and occasionally "United States (USA)". Left as-is for the geo gate to read.
                "location": (j.get("locations") or "").strip(),
                "url": j.get("public_url") or "",
                "source": "instahyre",
                "req_id": f"ih-{rid}",   # namespaced: a bare numeric id would collide with an
                                         # ATS req id and merge two unrelated jobs
                "posted_at": "",
                "description": "",
            })
        time.sleep(0.3)
    if not out:
        raise RuntimeError("instahyre: job_search returned no usable rows — check whether the "
                           "API shape or the job_functions ids have changed")
    return out


def fetch_radancy(board: dict, ua: str) -> list[dict]:
    """
    Radancy (formerly TalentBrew), the career-site front end NetApp and Intuit both run. The ATS
    behind it is something else again — NetApp's pages name SuccessFactors — but that is invisible
    from outside and does not matter, because the front end publishes everything needed.

    The route in is `/sitemap.xml`, not the job search. Radancy does expose
    `/search-jobs/results` as JSON, and it answers `hasJobs: true`, but `results` comes back empty
    for every parameter combination tried: the real call needs a facet id minted per portal. The
    sitemap needs none of that, and being XML it is a stabler contract than scraped markup.

    Every job URL carries the three fields worth having:

        /job/bengaluru/software-engineer-full-stack-engineer/27600/101302054944
              ^city     ^title slug                          ^org  ^req id

    So the title is a de-slugified guess, not the posted title — "Sr. Software Engineer" arrives
    as "Sr Software Engineer" and punctuation is gone. Good enough for the title gate, which
    matches on words, but it is marked `weak` so a real title from any other source wins and is
    never overwritten by this guess.

    One trap this platform sets: NetApp abbreviates manager as "mgr" in slugs, so
    "mgr-software-engineer" de-slugifies to "Mgr Software Engineer" and sails past an
    `exclude_titles` list containing only "manager". The profile needs "mgr" as its own entry, the
    same way SMTS and PMTS each need one.
    """
    host = board["host"]
    rows: list[dict] = []
    sitemap = _get_text(f"https://{host}/sitemap.xml", ua)
    urls = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    if not urls:
        raise RuntimeError(f"radancy: {host}/sitemap.xml returned no <loc> entries")

    job_rx = re.compile(r"^https?://[^/]+/job/([^/]+)/([^/]+)/(\d+)/(\d+)/?$")
    seen: set[str] = set()
    for u in urls:
        m = job_rx.match(u.strip())
        if not m:
            continue
        city, slug, _org, rid = m.groups()
        if rid in seen:
            continue
        seen.add(rid)
        rows.append({
            "company": board["company"],
            "title": slug.replace("-", " ").strip().title(),
            "location": city.replace("-", " ").strip().title(),
            "url": u.strip(),
            "source": "radancy",
            "req_id": rid,
            "posted_at": "",
            "description": "",
            # Both are derived from URL segments, so let anything better win. `--enrich` fetches
            # the posting page for JD text, and these pages are server-rendered.
            "weak": ("title", "location"),
        })
    if not rows:
        raise RuntimeError(
            f"radancy: {host}/sitemap.xml had {len(urls)} URLs but none matched the "
            "/job/<city>/<slug>/<org>/<id> shape — the URL scheme has probably changed")
    return rows


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "avature": fetch_avature,
    "radancy": fetch_radancy,
    "instahyre": fetch_instahyre,
    "workday": fetch_workday,
    "oracle": fetch_oracle,
    "atlassian": fetch_atlassian,
    "smartrecruiters": fetch_smartrecruiters,
}


# ------------------------------------------------------------------------------ seed

def title_from_url(url: str) -> str:
    """
    Best-effort title for a hand-collected URL. Most careers URLs carry a slug
    ("/software-engineer-java", "/Member-of-Technical-Staff-II"); when they do not, the
    company name plus "(check listing)" is honest about what we know.
    """
    # Path segments only. Including the host would happily return "Job Boards.Greenhouse.Io".
    segs = [s for s in urllib.parse.urlparse(url).path.split("/") if s]
    # Routing words that are never part of a job title, so "/profile/job_details/612870.../"
    # does not come back as "Job Details".
    noise = {"job", "jobs", "detail", "details", "job details", "careers", "career",
             "profile", "global", "en", "us", "en us", "en in", "department", "departments"}
    for seg in reversed(segs):
        words = re.sub(r"[-_.]+", " ", re.sub(r"\d{4,}", " ", seg)).strip()
        words = re.sub(r"\s+", " ", words)
        if words.lower() in noise or not words:
            continue
        if len(words) > 6 and re.search(r"[a-zA-Z]{4}", words) and " " in words:
            return words.title()
    return ""


def req_id_from_url(url: str) -> str:
    """
    Pull the ATS requisition id out of a seed URL when its shape reveals one. This is what
    stops a hand-collected Greenhouse link and the same req arriving from the board poll from
    becoming two rows: both end up keyed on company + this id.
    """
    m = re.search(r"greenhouse\.io/[^/]+/jobs/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"lever\.co/[^/]+/([0-9a-f-]{16,})", url)
    if m:
        return m.group(1)
    return ""


def load_seed(companies_filter: set[str] | None = None) -> list[dict]:
    seed = yaml.safe_load(SEED.read_text())
    out = []
    for c in seed["companies"]:
        if companies_filter and c["name"] not in companies_filter:
            continue
        for r in c.get("reqs") or []:
            url = r["url"]
            notes = " | ".join(x for x in [c.get("note"), r.get("note")] if x)
            derived = title_from_url(url)
            out.append(
                {
                    # A URL-derived title is a guess, so it is marked weak: it seeds an empty
                    # row but never overwrites a title the ATS actually gave us.
                    "weak": ("title", "location"),
                    "company": c["name"],
                    "title": derived or f"{c['name']} req (title unknown)",
                    "location": "",
                    "url": url,
                    "source": "seed",
                    "req_id": r.get("req_hint") or req_id_from_url(url),
                    "posted_at": "",
                    "description": "",
                    "notes": notes or None,
                    "human_path": c.get("human_path"),
                }
            )
    return out


# ---------------------------------------------------------------------------- enrich

# A JD is worth storing if it reads like a JD. Careers pages that are JavaScript shells come
# back as a few hundred characters of nav and a cookie banner, and feeding that to the scorer
# is worse than feeding it nothing: it looks like information and is not.
JD_SIGNALS = ("responsibilit", "qualification", "experience", "you will", "requirement",
              "what you", "skills", "about the role", "minimum")
JD_MIN_CHARS = 700


def extract_jd(html_text: str) -> str:
    """
    Pull JD text out of a careers page. Tries JSON-LD JobPosting first, since a page that
    publishes one is handing over clean text; falls back to stripping the whole body.
    """
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text, re.S | re.I,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:  # noqa: BLE001
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            if "JobPosting" in str(node.get("@type", "")) and node.get("description"):
                return strip_html(node["description"])
    return strip_html(html_text)


def looks_like_jd(text: str) -> bool:
    low = text.lower()
    return len(text) >= JD_MIN_CHARS and sum(s in low for s in JD_SIGNALS) >= 2


def jd_from_api(source: str, url: str, req_id: str, ua: str) -> str:
    """
    Workday and Oracle both serve the JD from a second API call, keyed off the posting URL. It
    is worth the extra request: their list endpoints return no description at all, so without
    this every req from those boards scores blind, and a blind score caps out around 6.

    Returns "" for any other source, so the caller falls back to scraping the page.
    """
    if source == "workday":
        # .../en-US/<site>/job/<City>/<Slug>_<ReqId>  ->  /wday/cxs/<tenant>/<site>/job/...
        m = re.match(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/[^/]+/([^/]+)(/job/.+)$",
                     url, re.I)
        if not m:
            return ""
        tenant, inst, site, path = m.groups()
        data = _get(f"https://{tenant}.{inst}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}",
                    ua)
        return strip_html((data.get("jobPostingInfo") or {}).get("jobDescription"))

    if source == "oracle":
        m = re.match(r"https://([^/]+)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/", url)
        if not m or not req_id:
            return ""
        host, site = m.groups()
        # The id has to be quoted inside the finder or the endpoint answers HTTP 400.
        finder = f'ById;Id="{req_id}",siteNumber={site}'
        data = _get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                    "?onlyData=true&expand=all&finder="
                    + urllib.parse.quote(finder, safe=';,="'), ua)
        item = (data.get("items") or [{}])[0]
        return " ".join(strip_html(item.get(k)) for k in
                        ("ExternalDescriptionStr", "ExternalQualificationsStr") if item.get(k))

    return ""


def recheck_closed(ua: str, min_score: int, limit: int) -> None:
    """
    Re-probe reqs already written off and reopen the ones still being served.

    Needed because closure used to happen on a single absence, which wrote off 103 reqs here —
    ten of them scoring 7 or above, including a 9/10. Two were confirmed by hand to be
    answering HTTP 200 while recorded as pulled. The miss counter stops that happening again,
    but it does nothing for the backlog, so this cleans it up once.

    Reopened reqs come back with `misses` one below the limit rather than zero. If a req really
    is gone and only looks live because its platform soft-404s (Greenhouse does exactly this),
    the next poll misses it and closes it again. The backlog therefore self-corrects instead of
    permanently resurrecting dead reqs.

    Scoped by score because reopening is not free: every recovered req reappears in the digest.
    A 2/10 that is still live is noise, so the default only revisits reqs that scored 6+ or were
    never scored at all.
    """
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT key, company, title, url, score FROM jobs "
            "WHERE still_open = 0 AND status IN ('new','scored') AND url IS NOT NULL "
            "AND (score IS NULL OR score >= ?) "
            "ORDER BY score IS NULL DESC, score DESC LIMIT ?",
            (min_score, limit),
        ).fetchall()
        print(f"re-probing {len(rows)} closed req(s) scoring {min_score}+ or unscored, "
              f"{ENRICH_WORKERS} at a time")
        if not rows:
            return

        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            verdicts = list(ex.map(lambda r: (r, url_is_live(r["url"], ua)), rows))

        back = 0
        for r, live in verdicts:
            s = r["score"] if r["score"] is not None else "?"
            if live is True:
                conn.execute(
                    "UPDATE jobs SET still_open = 1, misses = ?, notes = "
                    "TRIM(COALESCE(notes || ' | ', '') || ?) WHERE key = ?",
                    (store.MISS_LIMIT - 1,
                     f"reopened {store.now()[:10]}: posting URL still live after being "
                     "closed on a single missed poll", r["key"]),
                )
                back += 1
                print(f"  REOPENED  {s}/10  {r['company']:<17} {r['title'][:44]}")
            elif live is False:
                print(f"  gone      {s}/10  {r['company']:<17} {r['title'][:44]}")
            else:
                print(f"  no answer {s}/10  {r['company']:<17} {r['title'][:44]}")
        print(f"\nreopened {back}/{len(rows)}")


def enrich(ua: str, limit: int, verbose: bool = False) -> None:
    """Fetch the posting page for stored jobs that have no JD text, and keep what parses."""
    with store.connect() as conn:
        # Newest first, and that ordering is the whole point. Without it SQLite returns rowid
        # order, so the oldest reqs are tried every run — and the oldest are precisely the ones
        # that can never succeed: the JS-shell careers pages at Meta, Microsoft and Apple, plus
        # URLs that 404. They consumed the entire budget while reqs fetched minutes earlier
        # waited, which is how all seven NetApp reqs reached the scorer with no JD text and were
        # judged on their titles alone.
        rows = conn.execute(
            "SELECT key, company, title, url, source, req_id FROM jobs "
            "WHERE still_open = 1 AND url IS NOT NULL "
            "AND (description IS NULL OR length(description) < ?) "
            "ORDER BY first_seen DESC LIMIT ?",
            (JD_MIN_CHARS, limit),
        ).fetchall()

        print(f"\nenriching {len(rows)} job(s) with no JD text, "
              f"{ENRICH_WORKERS} at a time")

        # Each req is a separate request to a separate host, so these overlap cleanly. This
        # used to be one request at a time with a 0.5s sleep between them, which on a
        # TIMEOUT of 25s meant a handful of slow or blocked postings set the pace for the
        # whole stage. The fetching happens here; every DB write happens in the loop below,
        # because a sqlite3 connection is not thread-safe.
        def grab(r):
            try:
                text = jd_from_api(r["source"], r["url"], r["req_id"] or "", ua)
                if not text:
                    text = extract_jd(_get_text(r["url"], ua))
                return r, text, None
            except Exception as e:  # noqa: BLE001
                return r, None, e
            finally:
                # Kept per worker rather than per req: still spaces out repeat hits on any one
                # host, without the delay accumulating across the whole list.
                time.sleep(0.5)

        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            results = list(ex.map(grab, rows))

        got = 0
        for r, text, err in results:
            if err is not None:
                if isinstance(err, urllib.error.HTTPError):
                    # 404 and 410 on a posting URL are not fetch failures, they are the answer:
                    # the req is gone. Record that rather than retrying it every morning. 403
                    # and 429 are the site blocking a script, which says nothing about the req.
                    if err.code in (404, 410):
                        conn.execute(
                            "UPDATE jobs SET still_open = 0, notes = "
                            "TRIM(COALESCE(notes || ' | ', '') || ?) WHERE key = ?",
                            (f"posting URL returned HTTP {err.code} on {store.now()[:10]}, "
                             "req appears closed", r["key"]),
                        )
                        print(f"  {r['company']:<14} HTTP {err.code} — marked closed")
                    else:
                        print(f"  {r['company']:<14} HTTP {err.code} "
                              f"(blocked, req status unknown)")
                else:
                    print(f"  {r['company']:<14} {type(err).__name__}")
                continue

            if looks_like_jd(text):
                conn.execute(
                    "UPDATE jobs SET description = ? WHERE key = ?", (text[:MAX_DESC], r["key"])
                )
                got += 1
                print(f"  {r['company']:<14} {len(text):>6} chars  {r['title'][:38]}")
            else:
                print(f"  {r['company']:<14} {'no JD':>6}        "
                      f"{r['title'][:38]}  (page is {len(text)} chars, likely a JS shell)")
        print(f"recovered JD text for {got}/{len(rows)}")


# ------------------------------------------------------------------------------- main

def poll_board(board: dict, ua: str) -> tuple[dict, list[dict], str | None]:
    fn = FETCHERS.get(board.get("platform"))
    if not fn:
        return board, [], f"no fetcher for {board.get('platform')}"
    try:
        return board, fn(board, ua), None
    except urllib.error.HTTPError as e:
        return board, [], f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return board, [], f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--company", action="append", help="limit to these companies")
    ap.add_argument("--seed", action="store_true", help="also load config/seed.yaml reqs")
    ap.add_argument("--seed-only", action="store_true")
    ap.add_argument("--show-dropped", action="store_true")
    ap.add_argument("--enrich", action="store_true",
                    help="fetch posting pages for stored jobs missing JD text")
    ap.add_argument("--enrich-only", action="store_true")
    ap.add_argument("--enrich-limit", type=int, default=30,
                    help="how many JD-less reqs to chase per run (default: %(default)s)")
    ap.add_argument("--no-verify-closure", action="store_true",
                    help="skip the HTTP check on reqs about to be closed, and let the "
                         "consecutive-miss counter decide alone")
    ap.add_argument("--recheck-closed", action="store_true",
                    help="re-probe already-closed reqs and reopen any still being served")
    ap.add_argument("--recheck-min-score", type=int, default=6,
                    help="only re-probe closed reqs at or above this score, or unscored "
                         "(default: %(default)s)")
    ap.add_argument("--recheck-limit", type=int, default=60,
                    help="how many closed reqs to re-probe (default: %(default)s)")
    args = ap.parse_args()

    profile = jobprofile.load()
    filters, geo = profile["filters"], profile["geo"]
    ua = jobprofile.user_agent(profile)
    excluded = {c.lower() for c in (profile.get("exclusions", {}).get("companies") or [])}
    only = {c for c in (args.company or [])} or None

    if args.enrich_only:
        enrich(ua, args.enrich_limit)
        return 0

    if args.recheck_closed:
        recheck_closed(ua, args.recheck_min_score, args.recheck_limit)
        return 0

    postings: list[dict] = []

    if not args.seed_only:
        boards = yaml.safe_load(BOARDS.read_text())["boards"]
        live = [b for b in boards if b.get("enabled") and (not only or b["company"] in only)]
        for b in live:
            # The big boards filter by India server-side, and need the city list to spot the
            # facet. Passed through the board rather than imported, so fetchers stay testable.
            b["_geo"] = geo
        print(f"polling {len(live)} board(s)\n")
        with ThreadPoolExecutor(max_workers=BOARD_WORKERS) as ex:
            for board, rows, err in ex.map(lambda b: poll_board(b, ua), live):
                tag = f"  {board['company']:<14} {board['platform']:<11}"
                print(f"{tag} {'ERROR ' + err if err else f'{len(rows):>4} reqs'}")
                postings += rows

    if args.seed or args.seed_only:
        rows = load_seed(only)
        print(f"\n  {'seed.yaml':<14} {'manual':<11} {len(rows):>4} reqs")
        postings += rows

    kept, dropped = [], []
    for p in postings:
        if p["company"].lower() in excluded:
            dropped.append((p, "excluded company"))
            continue
        # Hand-collected seed reqs bypass the gates: you already looked at these and judged
        # them worth keeping, and their URL-derived titles are too thin to filter on.
        if p["source"] == "seed":
            p["geo"] = geo_verdict(p["location"], geo)[0] or "unclear"
            p["lane"] = lane_of(p["title"], filters)
            kept.append(p)
            continue
        ok, why = title_verdict(p["title"], filters)
        if not ok:
            dropped.append((p, why))
            continue
        bucket, why = geo_verdict(p["location"], geo)
        if bucket is None:
            dropped.append((p, why))
            continue
        p["geo"], p["lane"] = bucket, lane_of(p["title"], filters)
        kept.append(p)

    print(f"\n{len(postings)} fetched -> {len(kept)} passed the gates, {len(dropped)} dropped")

    if args.show_dropped:
        print("\ndropped:")
        for p, why in dropped[:80]:
            print(f"  {p['company']:<14} {p['title'][:52]:<52} {why}")

    if kept:
        print("\npassed:")
        for p in sorted(kept, key=lambda x: (x["company"], x["title"])):
            print(
                f"  {p['company']:<14} {p['title'][:50]:<50} "
                f"{p['geo']:<9} {p['lane']}  {(p['location'] or '')[:34]}"
            )

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    with store.connect() as conn:
        new = sum(store.upsert_job(conn, p) for p in kept)
        # Closure is decided on everything the boards returned, not on what survived the gates.
        # Keyed on `kept`, tightening a title rule would mark a perfectly live req as closed —
        # which is how two open Salesforce reqs got reported as pulled.
        by_source: dict[str, set] = {}
        for p in postings:
            k = store.job_key(p["company"], p["title"], p.get("url", ""), p.get("req_id", ""))
            by_source.setdefault(p["source"], set()).add(k)
        closed = pending = 0
        if not args.seed_only and not only:
            probe = (lambda u: None) if args.no_verify_closure else (
                lambda u: url_is_live(u, ua))
            for src, keys in by_source.items():
                if src != "seed":          # a seed list going quiet means nothing
                    c, p = store.mark_closed(conn, src, keys, verify=probe)
                    closed += c
                    pending += p
        store.log_run(
            conn, "fetch", fetched=len(postings), new_jobs=new,
            detail={"kept": len(kept), "dropped": len(dropped), "closed": closed,
                    "pending_closure": pending},
        )
    print(f"\nwrote: {new} new, {len(kept) - new} refreshed, {closed} marked closed, "
          f"{pending} absent but kept open")

    if args.enrich:
        enrich(ua, args.enrich_limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
