#!/usr/bin/env python3
"""
Mark every job in today's digest "Do today" section with one status.

    python3 mark_today.py applied              # preview only, marks nothing
    python3 mark_today.py applied --go         # actually mark them
    python3 mark_today.py skipped --go         # mark them skipped instead
    python3 mark_today.py applied --with-7plus # also include the "Scored 7+ and not yet acted on" table

Uses mark.py's partial-key matching, so an ID that matches zero or several jobs is
refused by mark.py and nothing is marked for it.
"""
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATUSES = ["new", "scored", "skipped", "referral_ask", "applied",
            "screen", "interview", "offer", "rejected", "closed"]


def ident_from_url(url: str) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    if "gh_jid" in q:                       # Greenhouse-style links
        return q["gh_jid"][0]
    if "instahyre.com" in u.netloc:         # /job-410837-sdet-ii-at-... -> ih-410837
        m = re.search(r"/job-(\d+)-", u.path)
        if m:
            return f"ih-{m.group(1)}"
    return re.split(r"[/_]", u.path.rstrip("/"))[-1]


def section(text: str, heading: str) -> str:
    if heading not in text:
        return ""
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def main() -> int:
    args = sys.argv[1:]
    go = "--go" in args
    with_7plus = "--with-7plus" in args
    positional = [a for a in args if not a.startswith("--")]
    status = positional[0] if positional else "applied"

    if status not in STATUSES:
        print(f"status must be one of: {' '.join(STATUSES)}")
        return 1

    path = Path("digest") / f"{date.today()}.md"
    if not path.exists():
        print(f"{path} not found. Run python3 run.py first.")
        return 1
    text = path.read_text()

    urls = []
    for line in section(text, "## Do today").splitlines():
        m = re.match(r"- \*\*.*?\]\(([^)]+)\)", line)   # top-level bullets only
        if m:
            urls.append(m.group(1))

    if with_7plus:
        for line in section(text, "## Scored 7+ and not yet acted on").splitlines():
            m = re.search(r"\]\(([^)]+)\)", line)
            if line.startswith("|") and m:
                urls.append(m.group(1))

    if not urls:
        print("Nothing found under 'Do today'. Either it is empty or already marked.")
        return 0

    seen = set()
    for url in urls:
        ident = ident_from_url(url)
        if ident in seen:
            continue
        seen.add(ident)
        if not ident or ident in {"job", "jobs"}:
            print(f"skipping (cannot derive an ID): {url}")
            continue
        cmd = [sys.executable, "mark.py", ident, status]
        print("python3 mark.py", ident, status)
        if go:
            subprocess.run(cmd)

    if not go:
        print("\nPreview only. Add --go to actually mark them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())