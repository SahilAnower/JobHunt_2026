import re, subprocess, sys
from datetime import date

status = next((a for a in sys.argv[1:] if not a.startswith("--")), "applied")
go = "--go" in sys.argv

text = open(f"digest/{date.today()}.md").read()
section = text.split("## Do today")[1].split("\n## ")[0]

for line in section.splitlines():
    m = re.match(r"- \*\*.*?\]\(([^)]+)\)", line)
    if not m:
        continue
    ident = re.split(r"[/_]", m.group(1).rstrip("/"))[-1]
    cmd = ["python3", "mark.py", ident, status]
    print(" ".join(cmd))
    if go:
        subprocess.run(cmd)