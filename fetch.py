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

import store

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "config" / "profile.yaml"
BOARDS = ROOT / "config" / "boards.yaml"
SEED = ROOT / "config" / "seed.yaml"

TIMEOUT = 25
MAX_DESC = 6000          # plenty for the scorer; keeps the db small and prompts cheap
WORKDAY_PAGES = 4        # 20 reqs per page
WORKDAY_INDIA_PAGES = 12  # once the India facet is applied the whole set fits in a few pages
ORACLE_PAGES = 24        # 25 reqs per page, per location — Uber has to be read unfiltered


# ----------------------------------------------------------------------------- http

def _get(url: str, ua: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


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
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
        return next((t for t in _terms(geo, key) if t in loc), None)

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


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
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


def enrich(ua: str, limit: int, verbose: bool = False) -> None:
    """Fetch the posting page for stored jobs that have no JD text, and keep what parses."""
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT key, company, title, url, source, req_id FROM jobs "
            "WHERE still_open = 1 AND url IS NOT NULL "
            "AND (description IS NULL OR length(description) < ?) LIMIT ?",
            (JD_MIN_CHARS, limit),
        ).fetchall()

        print(f"\nenriching {len(rows)} job(s) with no JD text")
        got = 0
        for r in rows:
            try:
                text = jd_from_api(r["source"], r["url"], r["req_id"] or "", ua)
                if not text:
                    text = extract_jd(_get_text(r["url"], ua))
            except urllib.error.HTTPError as e:
                # 404 and 410 on a posting URL are not fetch failures, they are the answer:
                # the req is gone. Record that rather than retrying it every morning. 403 and
                # 429 are the site blocking a script, which says nothing about the req.
                if e.code in (404, 410):
                    conn.execute(
                        "UPDATE jobs SET still_open = 0, notes = "
                        "TRIM(COALESCE(notes || ' | ', '') || ?) WHERE key = ?",
                        (f"posting URL returned HTTP {e.code} on {store.now()[:10]}, "
                         "req appears closed", r["key"]),
                    )
                    print(f"  {r['company']:<14} HTTP {e.code} — marked closed")
                else:
                    print(f"  {r['company']:<14} HTTP {e.code} (blocked, req status unknown)")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"  {r['company']:<14} {type(e).__name__}")
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
            time.sleep(0.5)
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
    ap.add_argument("--enrich-limit", type=int, default=30)
    args = ap.parse_args()

    profile = yaml.safe_load(PROFILE.read_text())
    filters, geo = profile["filters"], profile["geo"]
    ua = " ".join((profile.get("runtime", {}).get("user_agent") or "jobhunt/1.0").split())
    excluded = {c.lower() for c in (profile.get("exclusions", {}).get("companies") or [])}
    only = {c for c in (args.company or [])} or None

    if args.enrich_only:
        enrich(ua, args.enrich_limit)
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
        with ThreadPoolExecutor(max_workers=6) as ex:
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
        closed = 0
        if not args.seed_only and not only:
            for src, keys in by_source.items():
                if src != "seed":          # a seed list going quiet means nothing
                    closed += store.mark_closed(conn, src, keys)
        store.log_run(
            conn, "fetch", fetched=len(postings), new_jobs=new,
            detail={"kept": len(kept), "dropped": len(dropped), "closed": closed},
        )
    print(f"\nwrote: {new} new, {len(kept) - new} refreshed, {closed} marked closed")

    if args.enrich:
        enrich(ua, args.enrich_limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
