#!/usr/bin/env python3
"""
Find the real ATS behind a company's careers page, then verify it returns jobs.

The method is empirical, not guesswork. A careers page that is a JavaScript shell still has to
link somewhere when you click Apply, and that link points at the actual applicant tracking
system with the tenant and site baked into the URL:

    adobe.wd5.myworkdayjobs.com/external_experienced/job/Bangalore/...
    ^^^^^ ^^^                   ^^^^^^^^^^^^^^^^^^^ tenant, instance, site

So: fetch the careers and search pages, pull every ATS-shaped URL out of the HTML, then call
the matching public JSON API and keep only what actually returns requisitions. Nothing is
recorded on the strength of a pattern alone.

    python3 discover.py                  # probe and report
    python3 discover.py --merge          # also write findings into config/boards.yaml
    python3 discover.py --company Adobe
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

import fetch
import jobprofile

ROOT = Path(__file__).resolve().parent
BOARDS = ROOT / "config" / "boards.yaml"

# Where to look per company. A search-results page is far more productive than a landing page,
# because that is where the apply links live.
CANDIDATES: dict[str, list[str]] = {
    "Google": ["https://www.google.com/about/careers/applications/jobs/results/?location=India"],
    "Meta": ["https://www.metacareers.com/jobs?offices[0]=India"],
    "Microsoft": ["https://jobs.careers.microsoft.com/global/en/search?lc=India"],
    "LinkedIn": ["https://www.linkedin.com/careers/jobs", "https://careers.linkedin.com/jobs"],
    "Uber": ["https://www.uber.com/us/en/careers/list/?location=IND--Bangalore"],
    "Flipkart": ["https://www.flipkartcareers.com/#!/joblist", "https://www.flipkartcareers.com/"],
    "PhonePe": ["https://www.phonepe.com/careers/job-openings/"],
    "Atlassian": ["https://www.atlassian.com/company/careers/all-jobs"],
    "Apple": ["https://jobs.apple.com/en-in/search?location=india-INDC"],
    "Salesforce": ["https://careers.salesforce.com/en/jobs/", "https://www.salesforce.com/company/careers/"],
    "Adobe": ["https://careers.adobe.com/us/en/search-results"],
    "NVIDIA": ["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
               "https://www.nvidia.com/en-in/about-nvidia/careers/"],
    "Oracle": ["https://careers.oracle.com/jobs/#en/sites/jobsearch/jobs",
               "https://www.oracle.com/careers/"],
    "Rippling": ["https://www.rippling.com/careers/open-roles"],
    "Expedia Group": ["https://careers.expediagroup.com/jobs/"],
    "Wells Fargo": ["https://www.wellsfargojobs.com/en/jobs/"],
    "JP Morgan Chase": ["https://careers.jpmorgan.com/global/en/students/programs",
                        "https://www.jpmorganchase.com/careers"],
    "Mastercard": ["https://careers.mastercard.com/us/en/search-results"],
    "Visa": ["https://usa.visa.com/careers/job-search.html",
             "https://corporate.visa.com/en/jobs/"],
    "Juspay": ["https://juspay.io/careers"],
    "Cisco": ["https://jobs.cisco.com/jobs/SearchJobs/?21178=%5B169482%5D"],
    "Intuit": ["https://jobs.intuit.com/search-jobs/India"],
    "Experian": ["https://jobs.experian.com/search/?q=&locationsearch=india"],
    "ServiceNow": ["https://careers.servicenow.com/jobs/"],
    "Databricks": ["https://www.databricks.com/company/careers/open-positions"],
}

# Each pattern yields (platform, identity). Ordered most specific first.
PATTERNS = [
    ("workday", re.compile(
        r"([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?"
        r"([A-Za-z0-9_-]+)/job/", re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?"
                              r"([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(
        r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9]+)|"
        r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)")),
]

# Sites that host many employers; a hit here is not the company's own board.
GENERIC_SLUGS = {"embed", "job_board", "jobs", "www", "boards", "search", "api", "en", "us"}


def extract(html_text: str) -> list[dict]:
    found: list[dict] = []
    for platform, rx in PATTERNS:
        for m in rx.finditer(html_text):
            if platform == "workday":
                cand = {"platform": platform, "tenant": m.group(1).lower(),
                        "instance": m.group(2).lower(), "site": m.group(3)}
            else:
                slug = next((g for g in m.groups() if g), "")
                if slug.lower() in GENERIC_SLUGS:
                    continue
                cand = {"platform": platform, "slug": slug}
            if cand not in found:
                found.append(cand)
    return found


def verify(company: str, cand: dict, ua: str) -> tuple[bool, str, int]:
    """Call the real API. Only a non-empty job list counts as a find."""
    board = {"company": company, **cand}
    fn = fetch.FETCHERS.get(cand["platform"])
    if not fn:
        return False, "no fetcher", 0
    try:
        rows = fn(board, ua)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", 0
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__, 0
    return (len(rows) > 0), ("ok" if rows else "empty list"), len(rows)


def probe(company: str, urls: list[str], ua: str) -> dict:
    cands: list[dict] = []
    pages = 0
    for u in urls:
        try:
            html_text = fetch._get_text(u, ua)
            pages += 1
        except Exception:  # noqa: BLE001
            continue
        for c in extract(html_text):
            if c not in cands:
                cands.append(c)

    # A careers page links to plenty of things; verify each candidate rather than trusting the
    # first match, and keep whichever returns the most requisitions.
    results = []
    for c in cands[:8]:
        ok, why, n = verify(company, c, ua)
        results.append({**c, "ok": ok, "why": why, "count": n})
    best = max((r for r in results if r["ok"]), key=lambda r: r["count"], default=None)
    return {"company": company, "pages_read": pages, "candidates": results, "best": best}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", action="append")
    ap.add_argument("--merge", action="store_true", help="write findings into boards.yaml")
    args = ap.parse_args()

    profile = jobprofile.load()
    ua = jobprofile.user_agent(profile)

    boards = yaml.safe_load(BOARDS.read_text())["boards"]
    by_company = {b["company"]: b for b in boards}

    todo = {
        k: v for k, v in CANDIDATES.items()
        if (not args.company or k in args.company)
        and not by_company.get(k, {}).get("pollable")
    }
    print(f"probing {len(todo)} company careers site(s) for a real ATS\n")

    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(probe, c, u, ua): c for c, u in todo.items()}
        for f in as_completed(futs):
            r = f.result()
            out[r["company"]] = r
            if r["best"]:
                b = r["best"]
                ident = b.get("slug") or f"{b['tenant']}.{b['instance']}/{b['site']}"
                print(f"  FOUND  {r['company']:<16} {b['platform']:<11} {ident:<34} "
                      f"{b['count']} reqs")
            else:
                tried = ", ".join(f"{c['platform']}:{c.get('slug') or c.get('tenant')}"
                                  f"({c['why']})" for c in r["candidates"][:3]) or "no ATS URL in page"
                print(f"  --     {r['company']:<16} {tried}")

    found = {c: r["best"] for c, r in out.items() if r["best"]}
    print(f"\n{len(found)} of {len(todo)} resolved to a working JSON API")

    if not args.merge:
        print("(pass --merge to write these into config/boards.yaml)")
        return 0

    for company, b in found.items():
        row = {k: v for k, v in b.items() if k not in ("ok", "why", "count")}
        entry = by_company.get(company)
        note = (f"discovered by discover.py from the careers page, verified "
                f"{b['count']} reqs")
        if entry:
            entry.update({**row, "pollable": True, "enabled": True, "note": note})
        else:
            boards.append({"company": company, **row, "pollable": True,
                           "enabled": True, "note": note})

    header = [ln for ln in BOARDS.read_text().splitlines() if ln.startswith("#")]
    BOARDS.write_text(
        "\n".join(header) + "\n\n"
        + yaml.safe_dump({"boards": boards}, sort_keys=False, width=100, allow_unicode=True)
    )
    print(f"merged {len(found)} board(s) into {BOARDS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
