#!/usr/bin/env python3
"""
Derive config/boards.yaml from the URLs in config/seed.yaml.

No probing. A job URL already tells you which ATS a company runs on and under what slug:

    job-boards.greenhouse.io/cloudflare/jobs/7831810
                             ^^^^^^^^^^ greenhouse slug

    autodesk.wd1.myworkdayjobs.com/en-US/Ext/job/...
    ^^^^^^^^ ^^^                        ^^^ workday tenant / instance / site

Anything whose URL does not match a known ATS pattern is recorded as `own_site`: the
company runs its careers page itself (usually a JavaScript app that returns an empty shell
to a plain fetch), so it cannot be polled and must come in via an email alert instead.

    python3 seed_boards.py           # print what it derived
    python3 seed_boards.py --write   # write config/boards.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "config" / "seed.yaml"
BOARDS = ROOT / "config" / "boards.yaml"


# Each rule maps a URL to (platform, identity dict). First match wins.
def match_greenhouse(u, p):
    # job-boards.greenhouse.io/<slug>/jobs/<id>  |  boards.greenhouse.io/<slug>/jobs/<id>
    if "greenhouse.io" not in p.netloc:
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return None
    return "greenhouse", {"slug": parts[0]}


def match_ashby(u, p):
    # jobs.ashbyhq.com/<Slug>/<uuid>   (slug is case-sensitive)
    if "ashbyhq.com" not in p.netloc:
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return None
    return "ashby", {"slug": parts[0]}


def match_lever(u, p):
    # jobs.lever.co/<slug>/<uuid>
    if "lever.co" not in p.netloc:
        return None
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return None
    return "lever", {"slug": parts[0]}


def match_workday(u, p):
    # <tenant>.wd<N>.myworkdayjobs.com/<locale>/<site>/job/...
    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", p.netloc, re.I)
    if not m:
        return None
    tenant, instance = m.group(1), m.group(2)
    parts = [x for x in p.path.split("/") if x]
    # Skip a leading locale segment like "en-US" to find the site name.
    site = None
    for seg in parts:
        if re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", seg):
            continue
        site = seg
        break
    return "workday", {"tenant": tenant, "instance": instance, "site": site}


def match_smartrecruiters(u, p):
    if "smartrecruiters.com" not in p.netloc:
        return None
    parts = [x for x in p.path.split("/") if x]
    return "smartrecruiters", {"slug": parts[0]} if parts else None


def match_rippling(u, p):
    # ats.rippling.com/<slug>/jobs/<uuid> — Rippling's own ATS product.
    if "ats.rippling.com" not in p.netloc:
        return None
    parts = [x for x in p.path.split("/") if x]
    return "rippling", {"slug": parts[0] if parts else None}


RULES = [
    match_greenhouse,
    match_ashby,
    match_lever,
    match_workday,
    match_smartrecruiters,
    match_rippling,
]


def classify(url: str):
    p = urlparse(url)
    for rule in RULES:
        try:
            got = rule(url, p)
        except Exception:  # noqa: BLE001
            got = None
        if got:
            return got
    return "own_site", {"host": p.netloc}


# Platforms whose public JSON API fetch.py can read. `own_site` and `rippling` have no
# documented public job-board API.
POLLABLE = {"greenhouse", "ashby", "lever", "smartrecruiters", "workday", "oracle", "atlassian"}

# Some companies front their ATS with their own careers domain, so the URL hides the board.
# These are not guesses: each was confirmed by a live request on 2026-09-19 (HTTP 200 and a
# non-empty job list). Keyed by company name; overrides whatever the URL shape says.
# Add a line here only when you have actually seen the API return jobs.
KNOWN = {
    "Rubrik": {"platform": "greenhouse", "slug": "rubrik", "note": "139 reqs, 34 India; IC SDE seats rare, mostly Principal/EM"},
    "Databricks": {"platform": "greenhouse", "slug": "databricks", "note": "878 reqs, 99 India — the richest feed on the list"},
    "Datadog": {"platform": "greenhouse", "slug": "datadog", "note": "453 reqs, 8 India and all sales; keep for when engineering opens"},
    "Confluent": {"platform": "ashby", "slug": "Confluent", "note": "21 reqs; Ashby slugs are case-sensitive"},

    # Found by discover.py on 2026-09-19 and confirmed by a live job list. The careers pages
    # for all six are JavaScript shells; what gave them away was an apply link, a script tag,
    # or in Atlassian's case an endpoint its own site calls.
    "Adobe": {"platform": "workday", "tenant": "adobe", "instance": "wd5",
              "site": "external_experienced",
              "note": "639 reqs, 109 India + 5 Remote India via the locationMainGroup facet"},
    "Mastercard": {"platform": "workday", "tenant": "mastercard", "instance": "wd1",
                   "site": "CorporateCareers",
                   "note": "1066 reqs; India is city-level in the facet — Pune 181, Gurgaon 41"},
    "Salesforce": {"platform": "workday", "tenant": "salesforce", "instance": "wd12",
                   "site": "External_Career_Site",
                   "note": "1481 reqs, 121 India; found by probing the cxs API, not the page"},
    "Oracle": {"platform": "oracle", "host": "eeho.fa.us2.oraclecloud.com", "site": "CX",
               "note": "790 reqs, 48 India — Oracle Recruiting Cloud"},
    "JP Morgan Chase": {"platform": "oracle", "host": "jpmc.fa.oraclecloud.com", "site": "CX",
                        "note": "7415 reqs worldwide; only usable with the server-side India filter"},
    "Atlassian": {"platform": "atlassian",
                  "note": "289 reqs from www.atlassian.com/endpoint/careers/listings; "
                          "postings live on iCIMS, no location filter on the endpoint"},

    # Second discovery round: found by fingerprinting the careers page HTML for an ATS hostname
    # rather than an apply link, which is what these three leak.
    "Experian": {"platform": "smartrecruiters", "slug": "Experian",
                 "note": "30 India reqs; SmartRecruiters takes a country filter directly"},
    "Expedia Group": {"platform": "workday", "tenant": "expedia", "instance": "wd108",
                      "site": "search", "note": "180 reqs"},
    "Uber": {"platform": "oracle", "host": "iaziqy.fa.ocs.oraclecloud.com", "site": "CX",
             "note": "531 reqs; the ORC host is unguessable, it came out of the page source"},

    # Third round, 2026-09-22. Found by verifying candidate ATS slugs against the live public
    # APIs and keeping only boards that returned a non-empty job list. All 21 are recorded here
    # rather than derived from a seed URL, and deliberately carry no `reqs:` in seed.yaml:
    #
    #   1. Most of them serve `absolute_url` on their own careers domain
    #      (www.mongodb.com/careers/job/?gh_jid=...), which classifies as own_site and would
    #      silently drop the board. The real slug is the one in the API path.
    #   2. Even where the URL does derive, a seed req for an already-polled board is a straight
    #      duplicate of what the poll returns — and because these careers pages are JavaScript
    #      shells, the duplicate cannot be enriched, so it scores blind. Blind duplicates for
    #      MongoDB and Coinbase scored 8 and outranked the real reqs. Let the poll do the work.
    #
    # `in-band` below is what cleared both the geo and the title gate on 2026-09-22 — a
    # snapshot. The board is the durable fact; the count is not.
    "MongoDB": {"platform": "greenhouse", "slug": "mongodb", "verified": "2026-09-22",
                "note": "401 reqs, 75 India, 9 in-band — 6 of them 'Software Engineer 3' "
                        "(MongoDB's SDE II equivalent) in Gurugram. Richest in-band feed found"},
    "Stripe": {"platform": "greenhouse", "slug": "stripe", "verified": "2026-09-22",
               "note": "668 reqs, 48 India, 4 in-band IC 'Software Engineer' seats in Bangalore"},
    "GitLab": {"platform": "greenhouse", "slug": "gitlab", "verified": "2026-09-22",
               "note": "202 reqs, 25 India, 2 in-band; 'Intermediate' is GitLab's SDE II "
                       "equivalent and the company is all-remote, so no relocation cost"},
    "Coinbase": {"platform": "greenhouse", "slug": "coinbase", "verified": "2026-09-22",
                 "note": "218 reqs, 11 India, 1 in-band — Security Platform, Remote India"},
    "Okta": {"platform": "greenhouse", "slug": "okta", "verified": "2026-09-22",
             "note": "329 reqs, 99 India, 2 in-band (one is iOS, so a poor stack fit)"},
    "OpenAI": {"platform": "ashby", "slug": "openai", "verified": "2026-09-22",
               "note": "814 reqs, 11 India, 1 in-band — Applied AI Engineer, Delhi/Mumbai"},
    "Bosch": {"platform": "smartrecruiters", "slug": "BoschGroup", "verified": "2026-09-22",
              "note": "400 reqs and 341 of them in India, the highest India volume of anything "
                      "checked; 14 in-band but skewed to embedded C/C++, .NET/CAD and SAP BTP. "
                      "Expect the comp floor to filter most of it"},
    "Netskope": {"platform": "greenhouse", "slug": "netskope", "verified": "2026-09-22",
                 "note": "143 reqs, 20 India, 1 in-band and it is an IT/Workday systems seat"},
    "Mindtickle": {"platform": "lever", "slug": "mindtickle", "verified": "2026-09-22",
                   "note": "17 reqs, all India, 1 in-band — business systems, not product"},
    "Ubisoft": {"platform": "smartrecruiters", "slug": "Ubisoft2", "verified": "2026-09-22",
                "note": "only 4 reqs on the whole board, 1 in-band GenAI seat in Pune; gaming, "
                        "so comp is likely under the floor"},

    # Verified live but zero in-band India reqs on 2026-09-22. Kept on the Datadog precedent:
    # the board is real and the req list churns weekly, so polling costs one call.
    "Elastic": {"platform": "greenhouse", "slug": "elastic", "verified": "2026-09-22",
                "note": "372 reqs, 14 India — all Consulting Architect / field engineering"},
    "Zscaler": {"platform": "greenhouse", "slug": "zscaler", "verified": "2026-09-22",
                "note": "376 reqs, 84 India and all sales"},
    "Twilio": {"platform": "greenhouse", "slug": "twilio", "verified": "2026-09-22",
               "note": "141 reqs, 13 India and all marketing"},
    "Druva": {"platform": "greenhouse", "slug": "druva", "verified": "2026-09-22",
              "note": "36 reqs, 12 Pune — engineering present but Principal/Manager band only"},
    "Airbnb": {"platform": "greenhouse", "slug": "airbnb", "verified": "2026-09-22",
               "note": "157 reqs, 7 India, all Senior or non-engineering"},
    "Samsara": {"platform": "greenhouse", "slug": "samsara", "verified": "2026-09-22",
                "note": "266 reqs, 8 Bengaluru; Network Engineer II is close but not software"},
    "Groww": {"platform": "greenhouse", "slug": "groww", "verified": "2026-09-22",
              "note": "7 reqs, all India, none engineering. Note the host is "
                      "job-boards.eu.greenhouse.io, not the usual one"},
    "Fastly": {"platform": "greenhouse", "slug": "fastly", "verified": "2026-09-22",
               "note": "43 reqs, 3 Pune, support and CSM only"},
    "Grafana Labs": {"platform": "greenhouse", "slug": "grafanalabs", "verified": "2026-09-22",
                     "note": "137 reqs, 2 India, both Senior sales; remote-first so worth watching"},
    "Figma": {"platform": "greenhouse", "slug": "figma", "verified": "2026-09-22",
              "note": "155 reqs, 1 India (Account Executive). Thin, but engineering may open"},
    "ElevenLabs": {"platform": "ashby", "slug": "elevenlabs", "verified": "2026-09-22",
                   "note": "225 reqs, 16 India, all commercial. Second-lane if engineering opens"},
}

#
# Slugs tried on 2026-09-22 and confirmed dead (HTTP 404 on the public board API), recorded so a
# future session does not re-probe them. Not in KNOWN_DEAD because they are not in seed.yaml —
# KNOWN_DEAD only affects companies that are.
#   greenhouse 404: akamai, arcesium, aristanetworks, atlan, browserstack, canva, chargebee,
#     clevertap, cred, delhivery, doordash, dreamsports, freshworks, harness, hashicorp, hasura,
#     medianet, meesho, miro, moengage, navi, notion, nutanix, paloaltonetworks, pinelabs,
#     postman, redislabs, rippling, sarvamai, snowflakecomputing, sprinklr, swiggy, thoughtspot,
#     wayfair, zepto, zeta
#   lever 404: innovaccer, jupiter, khatabook, netradyne, plumhq, razorpay, setu, sharechat,
#     spinny, udaan, whatfix, zetwerk, zomato
#   ashby: anthropic 404, deel empty list
#   smartrecruiters: Philips and Visa both return an empty list
# Live but zero India reqs, so not added: Lyft, Pinterest, Reddit, Instacart, Asana, Discord
#   (greenhouse); Sierra, Ramp, Perplexity, Vanta, Linear (ashby).

# Confirmed dead ends. Recording them stops a future session re-probing the same tokens.
KNOWN_DEAD = {
    "PhonePe": "posts via job-boards.greenhouse.io/phonepe but boards-api 404s on every token tried "
               "(phonepe, phonepeprivatelimited, phonepeindia) — private-token embedded board",
    "Rippling": "migrated off Ashby to its own ATS at ats.rippling.com; no public JSON API",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    seed = yaml.safe_load(SEED.read_text())
    boards, reqs = [], []

    for c in seed["companies"]:
        name = c["name"]
        urls = [r["url"] for r in (c.get("reqs") or [])]
        for r in c.get("reqs") or []:
            reqs.append({"company": name, **r})

        # A company can appear on several hosts; prefer a pollable one over the first seen.
        seen = []
        for u in urls:
            platform, ident = classify(u)
            entry = {"platform": platform, **(ident or {})}
            if entry not in seen:
                seen.append(entry)

        origin = "url"
        if name in KNOWN:
            best = dict(KNOWN[name])
            origin = "verified"
        elif seen:
            best = next((e for e in seen if e["platform"] in POLLABLE), seen[0])
        else:
            best = {"platform": None}
            origin = "none"

        note = best.pop("note", None)
        # KNOWN entries carry their own verification date; the first two rounds were all
        # 2026-09-19, so that stays the default rather than being repeated 13 times.
        verified_on = best.pop("verified", "2026-09-19")
        pollable = best["platform"] in POLLABLE and name not in KNOWN_DEAD

        row = {"company": name, "platform": best["platform"], "pollable": pollable, "enabled": pollable}
        row.update({k: v for k, v in best.items() if k != "platform"})

        if name in KNOWN_DEAD:
            note = f"NOT pollable: {KNOWN_DEAD[name]}"
        elif note:
            note = f"verified live {verified_on} — {note}"
        elif origin == "none":
            note = f"no URL supplied ({c.get('status', 'unknown')})"
            if c.get("human_path"):
                note += f" — {c['human_path']}"
        else:
            note = f"derived from {len(urls)} seed URL(s)"
            if not pollable:
                note += " — no public JSON API; use an email alert or a manual check"
            if len(seen) > 1:
                note += f"; also seen on: {sorted({e['platform'] for e in seen[1:]})}"

        row["note"] = note
        boards.append(row)

    pollable = [b for b in boards if b.get("pollable")]
    print(f"{len(boards)} companies, {len(reqs)} seed reqs")
    print(f"{len(pollable)} pollable via a public JSON API:\n")
    for b in pollable:
        ident = b.get("slug") or f"{b.get('tenant')}/{b.get('site')}"
        print(f"  {b['company']:<16} {b['platform']:<16} {ident}")
    print("\nnot pollable (email alert or manual check):")
    for b in boards:
        if not b.get("pollable"):
            plat = b["platform"] or "—"
            print(f"  {b['company']:<16} {plat:<16} {b.get('host', '')}")

    if not args.write:
        print("\n(pass --write to save config/boards.yaml)")
        return 0

    BOARDS.write_text(
        "# Generated by seed_boards.py from config/seed.yaml — do not hand-edit;\n"
        "# add URLs to seed.yaml and re-run instead.\n"
        f"# Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"# {len(pollable)}/{len(boards)} companies are pollable via a public JSON API.\n\n"
        + yaml.safe_dump({"boards": boards}, sort_keys=False, width=100, allow_unicode=True)
    )
    print(f"\nwrote {BOARDS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
