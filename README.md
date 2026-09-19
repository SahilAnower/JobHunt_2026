# jobhunt

A small pipeline that does the tedious half of a job search: poll the ATS boards, score every
req against your actual resume, draft the referral asks, and put one digest on disk each
morning. You read the digest, send what's worth sending, and spend the rest of the day
preparing. That division of labour is the whole point.

**Nothing here sends anything.** No email is sent, no LinkedIn message is posted, no
application is submitted. Drafts land in `outbox/` and you send them by hand. This is a design
decision, not an unfinished feature: automated LinkedIn outreach gets accounts restricted, and
a referral ask that reads as machine-written costs you the contact permanently.

Everything is plain Python and one SQLite file. No server, no daemon, no framework.

---

## 1. Prerequisites

| Need | Why | Check |
|---|---|---|
| Python 3.10+ | f-strings with `\|` unions and `match`-era syntax | `python3 -V` |
| PyYAML | the only dependency | `pip3 install -r requirements.txt` |
| The `claude` CLI, logged in | scoring and drafting shell out to it | `claude -p "say ok"` |

There is **no API key anywhere in this project.** `score.py` and `draft.py` run
`claude -p --output-format text` against your existing Claude subscription, so a run costs
nothing per req. If `claude` is not on your `PATH`, put its absolute path in
`runtime.claude_bin` in your profile.

Internet access to the ATS APIs is the only other requirement. Nothing needs authentication —
every board this polls is a public unauthenticated JSON endpoint.

---

## 2. Setup, first time

```bash
cd jobhunt
pip3 install -r requirements.txt
mkdir -p logs                                          # cron writes here
cp config/profile.example.yaml config/profile.yaml
cp config/seed.example.yaml    config/seed.yaml
```

Both real config files are gitignored. The `.example.yaml` pair is tracked, which is why a
fresh clone has something to start from. **Keep real details out of the examples.**

### 2a. Fill in `config/profile.yaml`

This is the only file with opinions about you in it, and it decides everything the digest shows
you. Every key is commented in the example. The four that matter most:

- **`resume_summary`** — paste your real experience as prose. The scorer can only judge overlap
  it can actually see, so a vague summary produces vague scores. This is the single biggest
  lever on output quality.
- **`filters.exclude_titles`** — your seniority ceiling. Checked *before* `include_titles`, and
  it drops the large majority of what gets fetched. Raise or lower this block to move your band.
- **`geo`** — an allowlist. With `require_india_signal: true`, a posting with no match in any
  geo list is dropped outright.
- **`situation`** — free text pasted into the scoring prompt. Be blunt about your timeline and
  your weak spots; it tunes every score.

`comp.floor_lpa` and `comp.target_lpa` are in lakhs per annum. Change `comp.currency` and the
numbers if you are searching outside India, and replace the `geo` lists to match.

### 2b. Fill in `config/seed.yaml`

List your target companies. For each one you can find a live job URL for, paste it under
`reqs:`. That URL does double duty: the req enters the pipeline, **and** the ATS behind it is
derived from the URL shape, so every future req at that board is polled automatically from then
on. `job-boards.greenhouse.io/cloudflare/jobs/123` *is* the Greenhouse slug `cloudflare` — no
probing needed.

Companies with no recognisable ATS URL are recorded as `own_site` and cannot be polled; those
come in by email alert instead (see `EMAIL-ALERTS.md`).

### 2c. Derive the board list

```bash
python3 seed_boards.py            # print what it derived, write nothing
python3 seed_boards.py --write    # write config/boards.yaml
```

Re-run this with `--write` any time you add a company or a URL to `seed.yaml`.

`seed_boards.py` also carries a `KNOWN` table of boards that were verified by hand and cannot be
derived from a URL — Oracle Recruiting Cloud tenants and Atlassian's bespoke endpoint. Those are
merged in automatically, each with a note recording the req count it was confirmed against.

### 2d. First run

```bash
python3 run.py
cat digest/$(date +%F).md
```

The first pass takes 5–15 minutes, mostly waiting on the big Workday and Oracle boards. Expect
a few thousand postings fetched and a couple of dozen kept. That ratio is normal —
[see below](#why-it-drops-so-much).

Sanity checks if something looks wrong:

```bash
python3 store.py                              # pipeline counts by status and score
python3 store.py --schema                     # the three tables
python3 fetch.py --dry-run --show-dropped     # every posting, and why it was dropped
python3 score.py --dry-run                    # the exact prompt, calling nothing
```

---

## 3. Running it

`run.py` is the whole thing. Four stages, each tolerating the previous one having a bad day —
a board timing out should not cost you the digest.

```bash
python3 run.py               # fetch --seed --enrich  ->  score  ->  draft  ->  digest
python3 run.py --dry-run     # list the stages and exit
python3 run.py --no-draft    # score and digest only, write no outreach
python3 run.py --only score --only digest
```

Stages individually, when you are iterating on one thing:

```bash
python3 fetch.py --seed --enrich          # poll boards, reload seed reqs, recover JD text
python3 fetch.py --company Adobe          # one board only
python3 fetch.py --enrich-only            # just chase JD text for reqs missing it
python3 score.py                          # score everything unscored
python3 score.py --rescore --limit 20     # re-judge after editing the profile
python3 draft.py --min-score 8            # drafts only for the strongest
python3 digest.py --stdout                # print today's digest instead of writing it
```

If you edit `profile.yaml` in a way that changes the bar — a new `exclude_titles` entry, a
different `floor_lpa`, a rewritten `situation` — run `python3 score.py --rescore` so the
existing pipeline is judged by the new rules.

### The daily loop

Four steps, about ten minutes of your time:

```bash
python3 run.py                          # 1. cron usually did this already
cat digest/$(date +%F).md               # 2. read it. This is the only file you have to read.
ls outbox/                              # 3. send the drafts you approve of, by hand
python3 mark.py --list                  # 4. record what you did
python3 mark.py cloudflare::8168623 referral_ask --contact "Name" --note "asked on LinkedIn"
```

Statuses `mark.py` accepts: `new scored skipped referral_ask applied screen interview offer
rejected closed`. Marking matters more than it looks — the digest ranks on it, so an unmarked
pipeline slowly fills with reqs you already actioned.

---

## 4. Automating the daily run

Pick one of the two. Both assume absolute paths, because a scheduled job inherits almost no
environment.

### cron

```bash
mkdir -p logs
crontab -e
```

```cron
0 8 * * *  cd /path/to/jobhunt && /usr/bin/python3 run.py >> logs/run.log 2>&1
```

On macOS, `cron` needs **Full Disk Access** before it can write inside your home directory:
System Settings → Privacy & Security → Full Disk Access → add `/usr/sbin/cron`. Without it the
job runs and fails silently, which is the worst of both worlds. If you would rather not grant
that, use launchd below.

### launchd (the macOS-native option)

`launchd` also runs a missed job when the machine wakes, which cron does not. Write
`~/Library/LaunchAgents/com.jobhunt.daily.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.jobhunt.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/jobhunt/run.py</string>
  </array>
  <key>WorkingDirectory</key>   <string>/path/to/jobhunt</string>
  <key>StandardOutPath</key>    <string>/path/to/jobhunt/logs/run.log</string>
  <key>StandardErrorPath</key>  <string>/path/to/jobhunt/logs/run.err</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>   <integer>8</integer>
    <key>Minute</key> <integer>0</integer>
  </dict>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- `claude` lives outside launchd's minimal PATH. -->
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.jobhunt.daily.plist
launchctl start com.jobhunt.daily        # run it now, to prove it works
tail -40 logs/run.log
```

Whichever you choose, verify once by hand before trusting it. The two failure modes are both
environmental and both silent: `claude` not on the scheduler's `PATH`, and `logs/` not existing.
`run.py` prints a per-stage `ok` / `FAILED` line, so `grep FAILED logs/run.log` is the one-line
health check.

An 8am run means the digest is waiting before you start. Boards in the US and Europe post
overnight in IST, so a morning run catches a full day of new reqs.

---

## 5. The files

| File | What it does |
|---|---|
| `config/profile.yaml` | You. Resume, band, geography, comp floor, filters, exclusions. **The only file with opinions about you in it.** Gitignored. |
| `config/seed.yaml` | Target companies and the reqs you found by hand, with your own read on each. Gitignored. |
| `config/boards.yaml` | Generated by `seed_boards.py`. Which ATS each company runs on. Gitignored. |
| `config/*.example.yaml` | Tracked templates for the two above. |
| `seed_boards.py` | Derives `boards.yaml` from the seed URLs, plus a table of hand-verified boards. |
| `store.py` | SQLite: `jobs`, `outreach`, `runs`. Run it bare to see the pipeline. |
| `fetch.py` | Polls the boards, applies the title and geography gates, recovers JD text. |
| `score.py` | Scores 1–10 via `claude -p`, in batches, from the profile. |
| `draft.py` | Writes referral drafts to `outbox/`. |
| `digest.py` | Writes `digest/YYYY-MM-DD.md`. The one file you read. |
| `mark.py` | Moves a job along after you act. The only thing you type. |
| `run.py` | Runs the four stages in order. What cron calls. |
| `discover.py` | One-off: fingerprints a careers page for an ATS. Used to add boards, not in the daily run. |
| `EMAIL-ALERTS.md` | How to cover the companies that cannot be polled. |

`jobhunt.db`, `outbox/`, `digest/` and `logs/` are all gitignored. If you ever put this on a
remote, it must be a private one.

---

## Why it drops so much

A typical run fetches around 3,600 postings and keeps roughly 90, of which about 20 clear the
score bar. That funnel is working as intended:

- **The seniority ceiling does most of the work.** These boards are dominated by Senior, Staff
  and Principal reqs. A profile capped at SDE II drops the large majority of what it sees.
  `exclude_titles` is checked before anything else and is the highest-leverage list in the
  config. `exclude_overrides` exists for the awkward cases: "Member of Technical Staff II" is
  an in-band IC title that happens to contain the word "staff". Watch for bands written as
  abbreviations — `SMTS` and `PMTS` are Senior and Principal MTS, and each needs its own entry.
- **Geography is an allowlist, not a denylist.** `require_india_signal: true` drops any posting
  with no India signal at all. The alternative, listing every foreign city, is never complete,
  and every gap leaks an out-of-country req into the digest.
- **Greenhouse lies about location.** Several boards put a work arrangement ("Hybrid",
  "In-Office") in `location.name` and keep the real city in `offices[]` and a
  `Job Posting Location` metadata field. Both only appear with `?content=true`, so `fetch.py`
  merges all three. Without this, Cloudflare's India reqs are invisible.

To see the reasoning on any run: `python3 fetch.py --dry-run --show-dropped`.

## What this can and cannot see

Sixteen of the 31 target companies expose a public JSON API that can be polled:

| Platform | Companies |
|---|---|
| Greenhouse | Cloudflare, Databricks, Rubrik, Datadog, Razorpay |
| Workday | Adobe, Salesforce, Mastercard, Expedia Group, Autodesk |
| Oracle Recruiting Cloud | Oracle, JP Morgan Chase, Uber |
| Ashby | Confluent |
| SmartRecruiters | Experian |
| Bespoke endpoint | Atlassian |

Nine of those were found by `discover.py` and by fingerprinting careers-page HTML; none of
them advertise an API. The method that worked is in `discover.py`'s docstring, and the rule it
follows is worth keeping if you add boards yourself: **confirm a board with a live non-empty job
list, never with a URL that merely looks right.** Two methods that do **not** work, recorded so
nobody repeats them: probing `<tenant>.wd<N>.myworkdayjobs.com` hostnames answers HTTP 406 for
every combination including tenants that cannot exist, and scraping a careers page for apply
links only yields anything on the server-rendered ones.

The remaining 15 run their careers pages as JavaScript applications. A plain fetch gets a nav
bar and a cookie banner, so there is no honest way to poll them. For those, **email alerts are
the route in** — see `EMAIL-ALERTS.md`. A job alert email is machine-readable in a way the page
it links to is not.

### Reading the big boards

JP Morgan posts 7,414 reqs worldwide and Salesforce 1,481. Paging those unfiltered and gating
on geography afterwards would read thousands of postings to keep a handful, so `fetch.py`
narrows to India server-side first. The location facet ids are opaque per-tenant GUIDs, so
`india_facet_values()` discovers them at run time from the facet descriptors rather than having
them configured. Three traps are handled there and each one cost a debugging round:

- Workday nests facets, and the id must be submitted under the **innermost** `facetParameter`.
  Submitting it under the outer one is an HTTP 400.
- Workday reports `total` on the first page and `0` on every page after it. Believing the later
  value truncates every board to 40 reqs.
- "Indiana" contains "india". The match is boundary-checked on both sides.

Oracle's facet only lists the largest locations, so a company with reqs spread thinly across
India can show no India value at all — Uber does this. That case falls back to an unfiltered
read.

`fetch.py --enrich` recovers JD text for anything missing it. Workday and Oracle serve the
description from a second per-req API call, which is worth the extra request: their list
endpoints return no description at all, and a req scored blind caps out around 6. Everything
else falls back to fetching the posting page, which works on server-rendered pages and fails on
the JS ones. A `404` or `410` there is not a failure — the req is gone, and it gets marked
closed.

## Honest limitations

- **Reqs with no JD text score blind.** The scorer says so when it happens, and those scores
  cap out around 6. Treat them as "worth opening", not as a judgement.
- **Closure detection is inferred, not reported.** A req absent from a board it used to appear
  on is marked closed. That decision is made on everything the boards returned, *before* the
  title and geography gates — keying it on what survived the gates would make a tightened filter
  look identical to a req being pulled.
- **PhonePe cannot be polled.** It posts via `job-boards.greenhouse.io/phonepe`, but the
  Greenhouse API 404s on every board token tried. It is a private-token embedded board.
- **Scoring is one model's opinion.** It is calibrated by the rubric in `score.py` and the
  profile, and it is useful for ranking, not for deciding. A 6 you find interesting is worth
  more than a 7 you don't.
- **The store is local and unencrypted.** `jobhunt.db` holds your resume summary and pipeline.
  It is gitignored; keep it that way.
