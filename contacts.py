#!/usr/bin/env python3
"""
Write contacts.md: a per-company recruiter tracker, ordered by where chasing actually pays.

    python3 contacts.py            # print a summary
    python3 contacts.py --write    # write contacts.md

This deliberately does NOT invent email addresses. Nothing in the pipeline carries recruiter
contacts: the 44 pollable boards return titles, locations, descriptions and req ids, and that is
all. Emitting `firstname.lastname@company.com` for 57 companies would look authoritative while
being fabricated, and the only two real addresses anywhere in the fetched JD text
(accommodations@adobe.com, candidate_accessibility@elastic.co) are disability-accommodation
aliases that must not be used for job enquiries.

So the contact columns start empty and you fill them from sources you have actually checked. What
this file does give you is the part that is genuinely knowable, per company:

  - whether you already have applications in flight, and how many
  - your best score there, so you know what you are chasing
  - the warm path if one is recorded
  - the live careers/board URL, reconstructed from config/boards.yaml
  - whether it is pollable or alert-only

Tiering is the useful bit. Tier 1 is companies with live applications — a recruiter note there
references something concrete, which is the only version of cold outreach that works. Tier 3 is
alert-only companies where you have nothing in flight, and those are the ones where an unprompted
email is least likely to land.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

import store

ROOT = Path(__file__).resolve().parent
BOARDS = ROOT / "config" / "boards.yaml"
OUT = ROOT / "contacts.md"

# Statuses that mean you have something live worth referencing in an email.
LIVE = ("referral_ask", "applied", "screen", "interview", "offer")


def careers_url(b: dict) -> str:
    """Reconstruct the public board URL from whatever identity boards.yaml holds."""
    p, host, slug = b.get("platform"), b.get("host"), b.get("slug")
    if p == "greenhouse":
        return f"https://job-boards.greenhouse.io/{slug}"
    if p == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}"
    if p == "lever":
        return f"https://jobs.lever.co/{slug}"
    if p == "smartrecruiters":
        return f"https://careers.smartrecruiters.com/{slug}"
    if p == "workday":
        return (f"https://{b.get('tenant')}.{b.get('instance')}.myworkdayjobs.com/"
                f"{b.get('site')}")
    if p == "oracle":
        return f"https://{host}/hcmUI/CandidateExperience/en/sites/{b.get('site')}"
    if p == "atlassian":
        return "https://www.atlassian.com/company/careers/all-jobs"
    if host:
        return f"https://{host}"
    return ""


def collect() -> list[dict]:
    boards = yaml.safe_load(BOARDS.read_text())["boards"]
    rows = []
    with store.connect() as conn:
        for b in boards:
            co = b["company"]
            agg = conn.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN status IN (?,?,?,?,?) THEN 1 ELSE 0 END) live, "
                "MAX(score) best FROM jobs WHERE company = ?",
                (*LIVE, co),
            ).fetchone()
            warm = conn.execute(
                "SELECT human_path FROM jobs WHERE company = ? AND human_path IS NOT NULL "
                "AND human_path <> '' LIMIT 1", (co,),
            ).fetchone()
            rows.append({
                "company": co,
                "pollable": bool(b.get("pollable")),
                "platform": b.get("platform") or "-",
                "url": careers_url(b),
                "seen": agg["n"] or 0,
                "live": agg["live"] or 0,
                "best": agg["best"],
                "warm": (warm["human_path"] if warm else "") or "",
            })
    return rows


def tier(r: dict) -> int:
    if r["live"]:
        return 1                      # applications in flight — most worth a nudge
    if r["pollable"]:
        return 2                      # polled, nothing out yet
    return 3                          # alert-only, nothing out


def table(rows: list[dict]) -> list[str]:
    L = ["| Company | Recruiter name | Email | LinkedIn | Where you found them | Last contacted |",
         "|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| **{r['company']}** |  |  |  |  |  |")
    return L


def scan() -> int:
    """
    Search stored JD text for anything that looks like a real contact. This is the honest half of
    finding recruiters: an address printed in a job description was published by the employer, so
    it is a fact rather than a guessed pattern. Accommodation and accessibility aliases are called
    out rather than filtered, because they look like contacts and must not be used as ones.
    """
    email_rx = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    # "recruiter" also appears in boilerplate ("no agency recruiters"), so require a nearby
    # capitalised name or a contact verb to cut the noise.
    named_rx = re.compile(
        r"(?:recruiter|talent acquisition|hiring manager|point of contact)[^.\n]{0,60}?"
        r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b")
    DO_NOT_USE = ("accommodation", "accessibility", "legal", "privacy", "abuse", "compliance")

    emails: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT company, description FROM jobs "
            "WHERE description IS NOT NULL AND description <> ''"
        ).fetchall()
    for r in rows:
        co, d = r["company"], r["description"] or ""
        for e in email_rx.findall(d):
            emails.setdefault(co, set()).add(e)
        for a, b in named_rx.findall(d):
            names.setdefault(co, set()).add(f"{a} {b}")

    print(f"scanned {len(rows)} job descriptions\n")
    print(f"email addresses found ({sum(len(v) for v in emails.values())}):")
    if not emails:
        print("  none")
    for co in sorted(emails):
        for e in sorted(emails[co]):
            flag = ""
            if any(w in e.lower() for w in DO_NOT_USE):
                flag = "  <-- DO NOT USE: accommodation/legal alias, not recruiting"
            print(f"  {co:<18} {e}{flag}")

    print(f"\npossible names near recruiter wording ({sum(len(v) for v in names.values())}):")
    if not names:
        print("  none — these boards do not name recruiters in the JD")
    for co in sorted(names):
        print(f"  {co:<18} {', '.join(sorted(names[co]))}")
    print("\nNothing here is guessed. Anything absent above is absent from the data, and no "
          "address\nshould be inferred from a name.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--scan", action="store_true",
                    help="search stored JD text for real contacts")
    args = ap.parse_args()

    if args.scan:
        return scan()

    rows = collect()
    for r in rows:
        r["tier"] = tier(r)
    rows.sort(key=lambda r: (r["tier"], -(r["live"] or 0), -(r["best"] or 0), r["company"]))

    t1 = [r for r in rows if r["tier"] == 1]
    t2 = [r for r in rows if r["tier"] == 2]
    t3 = [r for r in rows if r["tier"] == 3]

    print(f"{len(rows)} companies — tier 1: {len(t1)} with live applications, "
          f"tier 2: {len(t2)} polled/nothing out, tier 3: {len(t3)} alert-only")
    if not args.write:
        print("(pass --write to save contacts.md)")
        return 0

    L = [
        f"# Recruiter contacts — generated {date.today().isoformat()}",
        "",
        "Regenerate with `python3 contacts.py --write`. Your own edits to the contact columns",
        "will be overwritten, so keep filled-in rows in a copy, or stop regenerating once you",
        "start using it in earnest.",
        "",
        "**No email address in this file was guessed.** The contact columns are blank because the",
        "pipeline has no recruiter data to give: ATS APIs return reqs, not the people behind them.",
        "Fill them from sources you have actually checked. The two real addresses found anywhere in",
        "the fetched job descriptions are `accommodations@adobe.com` and",
        "`candidate_accessibility@elastic.co`, and both are disability-accommodation aliases that",
        "should not be used for job enquiries.",
        "",
        "Where to find a real name, in rough order of yield:",
        "",
        "1. **The reqs you already hold.** Workday and Oracle postings occasionally name a",
        "   recruiter, and any address in a JD is real rather than guessed. Search the stored",
        "   descriptions with `python3 contacts.py --scan`.",
        "2. **LinkedIn, by hand.** Search the company plus \"technical recruiter\" or \"talent",
        "   acquisition\" and filter to India. Do not script this: scraping gets accounts",
        "   restricted, and the account is the referral graph.",
        "3. **A referral instead.** A warm introduction outperforms a cold recruiter email by a",
        "   wide margin, which is the premise this whole project is built on.",
        "",
        "Tiers are about whether you have anything concrete to reference, because a note that",
        "names a req you applied to reads as a follow-up, and one that does not reads as a mass",
        "mail.",
        "",
    ]

    for name, group, blurb in [
        ("Tier 1 — live applications, worth a follow-up", t1,
         "You have something in flight at each of these. A short note referencing the specific "
         "req and its id is the highest-yield outreach available to you."),
        ("Tier 2 — polled, nothing out yet", t2,
         "Boards you watch daily but have not applied to. Apply first; a recruiter note before "
         "an application gives them nothing to look up."),
        ("Tier 3 — alert-only, nothing out", t3,
         "Not pollable, so these reach you by email alert. Cold outreach here is the weakest "
         "play on the page. Prioritise Google and Microsoft anyway, since those are the two "
         "warm paths you already hold."),
    ]:
        L += [f"## {name} ({len(group)})", "", blurb, ""]
        if not group:
            L += ["_None._", ""]
            continue
        L += ["| Company | Platform | Apps in flight | Best score | Board | Warm path |",
              "|---|---|---|---|---|---|"]
        for r in group:
            link = f"[board]({r['url']})" if r["url"] else "—"
            best = r["best"] if r["best"] is not None else "—"
            L.append(f"| **{r['company']}** | {r['platform']} | {r['live']} | {best} | "
                     f"{link} | {r['warm'] or ''} |")
        L += ["", "Fill these in as you find them:", ""] + table(group) + [""]

    L += [
        "## Before you send anything",
        "",
        "- One note per company, not per req. Several of your reqs are duplicate postings from the",
        "  same team, and the same recruiter sees them all.",
        "- Reference the req id. It is the difference between a follow-up and a mass mail.",
        "- Record it afterwards so the digest stops resurfacing the req:",
        "  `python3 mark.py <job-key> referral_ask --contact \"Name\"`",
        "- Nothing in this project sends email. This file is a reference you act on by hand.",
        "",
    ]

    OUT.write_text("\n".join(L) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
