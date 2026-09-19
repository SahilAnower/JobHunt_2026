# Email alerts — the route into the boards jobhunt can't poll

15 of your 31 targets run their careers page as a JavaScript application. A script gets a nav
bar and a cookie banner; there is no honest way to poll them. But all of them will *email* you
when a matching req opens, and an alert email is machine-readable in a way the page it links to
is not. So the alert email becomes the feed.

The other 16 are polled directly now, so you can skip them in step 3 below. The ones that still
need an alert are Google, Meta, Microsoft, LinkedIn, Apple, NVIDIA, ServiceNow, Cisco, Intuit,
Wells Fargo, Visa, Flipkart, PhonePe, Rippling and Juspay.

One-time setup, about 40 minutes. After that it runs itself.

---

## 1. Gmail label and filters

Make the label first, then the filters that fill it.

**Create the label:** Gmail → Settings (gear) → See all settings → Labels → Create new label →
`JobAlerts`. Add two nested ones: `JobAlerts/Targets` and `JobAlerts/Boards`.

**Create the filters:** Settings → Filters and Blocked Addresses → Create a new filter. For
each row below, paste the string into **From**, then on the next screen tick **Apply the label**
(pick the label in the row), **Never send it to Spam**, and **Skip the Inbox (Archive it)**.

That last one matters. These will be 30 to 60 emails a day. If they land in your inbox you will
stop reading your inbox.

| From contains | Label | Covers |
|---|---|---|
| `jobalerts-noreply@linkedin.com` | `JobAlerts/Boards` | LinkedIn saved-search alerts |
| `jobs-listings@linkedin.com` | `JobAlerts/Boards` | LinkedIn "jobs you may be interested in" |
| `no-reply@instahyre.com` | `JobAlerts/Boards` | Instahyre |
| `info@naukri.com` | `JobAlerts/Boards` | Naukri job agents |
| `team@hi.wellfound.com` | `JobAlerts/Boards` | Wellfound (ex-AngelList) |
| `notify@cutshort.io` | `JobAlerts/Boards` | Cutshort |
| `googlealerts-noreply@google.com` | `JobAlerts/Boards` | the catch-all in step 4 |
| `no-reply@greenhouse.io` | `JobAlerts/Targets` | Greenhouse job-alert subscriptions |
| `notifications@ashbyhq.com` | `JobAlerts/Targets` | Ashby |
| `no-reply@us.greenhouse-mail.io` | `JobAlerts/Targets` | Greenhouse's other sender |
| `careers@` | `JobAlerts/Targets` | most company alert senders |
| `talent@` | `JobAlerts/Targets` | the rest of them |

A second filter worth adding, because it is what actually protects your attention: **From**
`jobalerts-noreply@linkedin.com` **and** Subject `senior OR staff OR principal OR lead OR
director OR manager` → **Delete it**. Those are unwinnable at your band and they are the bulk
of the volume.

---

## 2. LinkedIn saved searches

LinkedIn alerts are the highest-volume source and the only one that reliably catches reqs at
companies with no public API. Create each search, set **Job alert** to **On**, frequency
**Daily**, delivery **Email**.

Set these filters on every one of them:

- Experience level: **Entry level** and **Associate** (not Mid-Senior. That is the whole trick;
  Mid-Senior is where the 5-to-8-year reqs live)
- Date posted: **Past 24 hours**
- Location: `Hyderabad, Telangana, India`, plus a second copy of each search on
  `India` with **Remote** ticked

The six searches:

1. `software engineer` — the base case, highest volume
2. `backend engineer` OR `back end developer`
3. `java developer` OR `spring boot`
4. `distributed systems` OR `platform engineer`
5. `AI engineer` OR `LLM engineer` OR `agent engineer` — your second lane
6. `forward deployed engineer` OR `member of technical staff` — the titles that pay well and
   that almost nobody searches for, so the applicant pools are small

Do not automate anything against LinkedIn beyond these alerts. Scraping or scripted messaging
gets accounts restricted, and losing the account costs you the referral graph, which is the
single most valuable asset in this search.

---

## 3. Per-company alerts at the 15 unpollable targets

This is the tedious part and it is worth doing once. For each company: open the careers page,
search for a role, and look for **"Create job alert"**, **"Join our talent community"**, or
**"Get notified"**. Use the address in `candidate.email`.

Set the alert filters to **Software Engineer, India** where the form allows it.

| Company | Where |
|---|---|
| Google | careers.google.com → the bell icon on a search result |
| Meta | metacareers.com → "Create job alert" at the bottom of a search |
| Microsoft | careers.microsoft.com → "Save search" then enable the email |
| LinkedIn | linkedin.com/careers → talent community |
| Apple | jobs.apple.com → "Save this search" (needs an Apple ID) |
| NVIDIA | nvidia.com/en-us/about-nvidia/careers → job alerts |
| ServiceNow | careers.servicenow.com → talent community |
| Cisco | jobs.cisco.com → "Create job alert" |
| Intuit | jobs.intuit.com → "Join our talent community" |
| Wells Fargo | wellsfargojobs.com → "Join our talent community" |
| Visa | usa.visa.com/careers → job alerts |
| Flipkart | flipkartcareers.com → talent community |
| PhonePe | phonepe.com/careers → the Greenhouse alert form |
| Rippling | rippling.com/careers → the Rippling ATS alert form |
| Juspay | juspay.io/careers → the form, or email careers@juspay.in |

Microsoft and Google are the two that matter most here, because those are the two warm paths
you already have. An alert on either is worth more than the other thirteen combined.

Six of the reqs in your seed list are already closed (ServiceNow, Adobe, both PhonePe, Wells
Fargo, Intuit). That is exactly why the alerts matter more than the URL list: the list is a
snapshot, the alerts are a stream.

---

## 4. Google Alerts as the catch-all

For anything the above misses. Go to google.com/alerts and create these, **As-it-happens**,
delivered to your Gmail:

```
"software engineer" site:boards.greenhouse.io India
"software engineer" site:jobs.ashbyhq.com India
"software engineer" site:jobs.lever.co India
("backend engineer" OR "platform engineer") Hyderabad
```

The first three are the highest-value ones: they surface Greenhouse, Ashby and Lever boards at
companies you have never heard of, which is where India IC seats at good comp are least
contested.

---

## 5. Wiring the alerts into jobhunt

Two ways, and the first is enough to start.

**By hand, two minutes a day.** Read the `JobAlerts` label. When something is in band, paste
its URL into `config/seed.yaml` under the company, then:

```bash
python3 fetch.py --seed-only --enrich   # pull it in and try for JD text
python3 score.py                        # score it
python3 digest.py                       # it shows up in today's digest
```

If the company is new, add it to `targets.companies` in `config/profile.yaml` too.

**Via the Gmail connector,** once the manual loop has proven which alerts are actually worth
reading. Claude Code → `/mcp` → add the Gmail connector → authorise read-only. Then a stage can
read the `JobAlerts` label and extract the URLs itself. Do this second, not first: you will
otherwise automate the ingestion of four alert sources that turn out to be noise.

Set read-only scope. There is no reason for this project to hold send permission, and there is
one good reason not to.

---

## What to expect

Roughly 30 to 60 alert emails a day once everything is live. Of those, two to five will be in
band, and on a normal day one will score 7 or higher.

That number is small and it is not a filtering problem. Across every board this project can
read, there are currently eight India reqs at SDE I-II. **Supply at your band is the
bottleneck, not sourcing.** Which is the argument for this whole setup: the scanning is now
cheap, so the hours go into preparation and into the warm paths you already have.
One recruiter thread or one referral is worth more than the next hundred alert emails.
