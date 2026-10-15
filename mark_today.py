#!/usr/bin/env python3
"""
Mark every job in today's digest "Do today" section with one status.

    python3 mark_today.py applied              # preview only, marks nothing
    python3 mark_today.py applied --go         # actually mark them
    python3 mark_today.py skipped --go         # mark them skipped instead
    python3 mark_today.py applied --with-7plus # also include the "Scored 7+ and not yet acted on" table

Resolution is by URL against the database, not by guessing an id out of the URL text.
The digest prints the same URL the fetcher stored, so the job key is a lookup rather
than a parse. That matters: an Atlassian link ends in `/job`, and a Workday link can
end `_R171461-1` where the stored key is `R171461`, so the old id-from-URL guess
produced zero matches and `mark.py` quietly marked nothing. Where the URL is not in
the database the id guess is still used as a fallback.

Every `mark.py` call is checked, and anything that did not get marked is listed at the
end. A run that reports nothing marked nothing.

Reqs deferred by the per-company cap are deliberately NOT included. They render as
`- 8/10 Company — [Role](url)` without the `**`, outside the action lists, and the
point of the cap is that they are not today's work.
"""
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import store

STATUSES = ["new", "scored", "skipped", "referral_ask", "applied",
            "screen", "interview", "offer", "rejected", "closed"]

FLAGS = {"--go", "--with-7plus"}


def ident_from_url(url: str) -> str:
    """Fallback only, for a URL the database does not have. See key_from_url first."""
    u = urlparse(url)
    q = parse_qs(u.query)
    if "gh_jid" in q:                       # Greenhouse-style links
        return q["gh_jid"][0]
    if "instahyre.com" in u.netloc:         # /job-410837-sdet-ii-at-... -> ih-410837
        m = re.search(r"/job-(\d+)-", u.path)
        if m:
            return f"ih-{m.group(1)}"
    if "icims.com" in u.netloc:             # /jobs/25540/backend-.../job -> 25540
        m = re.search(r"/jobs/(\d+)/", u.path)
        if m:
            return m.group(1)
    tail = re.split(r"[/_]", u.path.rstrip("/"))[-1]
    # Workday appends a dedup suffix to the slug that is not part of the req id, so
    # "..._R171461-1" has to fall back to "R171461" or it matches nothing at all.
    if re.fullmatch(r"[A-Za-z]*-?\d+-\d+", tail):
        tail = tail.rsplit("-", 1)[0]
    return tail


def key_from_url(conn, url: str) -> tuple[str | None, str]:
    """
    (key, note) for a digest URL. key is None when it cannot be resolved uniquely.

    A handful of URLs map to two rows, because a hand-seeded row and the row the
    fetcher later found can share a posting URL under different keys. Prefer the
    fetched row: it is the one carrying the real title, score and req id.
    """
    rows = list(conn.execute(
        "select key, source, status from jobs where url = ?", (url,)))
    if not rows:
        return None, "url not in the database"
    if len(rows) == 1:
        return rows[0][0], ""
    real = [r for r in rows if r[1] != "seed"]
    if len(real) == 1:
        others = ", ".join(r[0] for r in rows if r[0] != real[0][0])
        return real[0][0], f"url also matches the seeded row {others}"
    return None, (f"url maps to {len(rows)} rows and none is uniquely the fetched one: "
                  + ", ".join(r[0] for r in rows))


def section(text: str, heading: str) -> str:
    if heading not in text:
        return ""
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def urls_from_digest(text: str, with_7plus: bool) -> list[str]:
    urls = []
    for line in section(text, "## Do today").splitlines():
        m = re.match(r"- \*\*.*?\]\(([^)]+)\)", line)   # top-level bullets only
        if m:
            urls.append(m.group(1))

    if with_7plus:
        for line in section(text, "## Scored 7+ and not yet acted on").splitlines():
            m = re.search(r"\]\(([^)]+)\)", line)
            # Table rows only. The same section also prints a `- **Company — Title**:
            # reason` bullet per req, and the collapsed cap block prints `- 8/10 ...`;
            # neither should be marked from here.
            if line.startswith("|") and m:
                urls.append(m.group(1))
    return urls


def main() -> int:
    args = sys.argv[1:]
    unknown = [a for a in args if a.startswith("--") and a not in FLAGS]
    if unknown:
        # Silently ignoring a typo meant --wtih-7plus looked like it worked and marked
        # only half of what the owner meant to mark.
        print(f"unknown flag(s): {' '.join(unknown)}")
        print(f"valid flags: {' '.join(sorted(FLAGS))}")
        return 1

    go = "--go" in args
    with_7plus = "--with-7plus" in args
    positional = [a for a in args if not a.startswith("--")]

    if not positional:
        if go:
            # Defaulting to "applied" while --go is set would write a status the owner
            # never typed, across every req in the digest.
            print("refusing to mark without an explicit status.")
            print(f"usage: python3 mark_today.py <status> --go   ({' '.join(STATUSES)})")
            return 1
        print(f"no status given, previewing as 'applied'. statuses: {' '.join(STATUSES)}")
    status = positional[0] if positional else "applied"

    if status not in STATUSES:
        print(f"status must be one of: {' '.join(STATUSES)}")
        return 1

    path = Path("digest") / f"{date.today()}.md"
    if not path.exists():
        print(f"{path} not found. Run python3 run.py first.")
        return 1
    text = path.read_text()

    urls = urls_from_digest(text, with_7plus)
    if not urls:
        print("Nothing found under 'Do today'. Either it is empty or already marked.")
        return 0

    conn = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)

    marked, failed, skipped = [], [], []
    seen: set[str] = set()
    for url in urls:
        target, note = key_from_url(conn, url)
        if target is None:
            fallback = ident_from_url(url)
            if not fallback or fallback in {"job", "jobs"}:
                skipped.append((url, note or "cannot derive an id"))
                print(f"skipping ({note or 'cannot derive an id'}): {url}")
                continue
            target, note = fallback, f"{note}; falling back to the id {fallback!r}"

        if target in seen:
            continue
        seen.add(target)

        # Quoted because a key carries the company name and several have spaces in them
        # ("deutsche telekom digital labs::ih-424285"). The subprocess call below passes
        # argv directly so it is unaffected, but this line is printed to be re-run by
        # hand, and unquoted it would split into three arguments and fail.
        print(f"python3 mark.py {shlex.quote(target)} {status}"
              + (f"    # {note}" if note else ""))
        if not go:
            continue
        proc = subprocess.run([sys.executable, "mark.py", target, status])
        (marked if proc.returncode == 0 else failed).append(target)

    print()
    if not go:
        print(f"Preview only, {len(seen)} req(s) would be marked {status}. "
              "Add --go to actually mark them.")
        if skipped:
            print(f"{len(skipped)} could not be resolved and would be left alone.")
        return 0

    print(f"marked {len(marked)} req(s) {status}.")
    if skipped:
        print(f"{len(skipped)} skipped, unresolvable from the digest:")
        for url, why in skipped:
            print(f"  - {why}: {url}")
    if failed:
        # mark.py exits non-zero when a key matches zero or several rows. Reporting the
        # count is the difference between knowing and assuming.
        print(f"{len(failed)} FAILED and are still unmarked:")
        for t in failed:
            print(f"  python3 mark.py {shlex.quote(t)} {status}")
    return 1 if failed or skipped else 0


if __name__ == "__main__":
    sys.exit(main())
