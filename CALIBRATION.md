# Calibration — the heuristic engine against Claude's labels

The `jobs.score` column holds reqs that `engine: claude` already judged — 806 of them when the
tables below were measured, 918 today. That is the only ground truth this project has, and it
is destructible: `score.py --rescore` overwrites it. So every number below was measured against
a **copy**, and `calibrate.py` refuses to open the live `jobhunt.db` at all. The labels are now
also exported to `calibration/claude-labels.json`; see **The label snapshot** below.

Re-derive any of it:

```bash
cp jobhunt.db /tmp/cal.db
python3 calibrate.py --db /tmp/cal.db
python3 calibrate.py --db /tmp/cal.db --before-high2     # the pre-fix skill weights
```

## The label snapshot

Every table below this section was measured when the corpus held **806** scored rows. It has
since grown to **918**, and the labels now live in a file as well as in the column:

```bash
python3 calibration_labels.py --export          # writes calibration/claude-labels.json
python3 calibrate.py --db /tmp/cal.db --labels  # measure against that file
```

| | |
|---|---|
| labels exported | **918** |
| had a JD at export (700+ chars) | 616 |
| blind at export | 302 |
| provenance guard rejected | 0 — every stored reason differs from the heuristic's own output |
| labels digest | `0ae6c682c99fbc7545e864a303e9b017021026f81e5b7cc20e2c3fc9febd0342` |
| file sha256 | `704daa6d69e6c86c4f43792ab5a77c8cd0a323e3de71f5937bb53f0b4c73c2a9` |

The labels digest hashes the labels only, so a re-export of an unchanged corpus reproduces it;
the file sha256 covers the `exported_at` stamp too and will not match a second export. The file
is gitignored — every row quotes a `score_reason` naming the owner and their situation — so
those two hashes and the row count are the record that it is intact. `--export` refuses to
overwrite it without `--force`.

That the guard rejected 0 rows is the proof that all 918 scores are Claude's: for every row,
the shipped heuristic produces a different reason string than the one stored.

On the 918-row corpus the shipped scorer measures Spearman **+0.542** all rows and **+0.622**
on the with-JD slice, promoting **276** rows at 7+ against Claude's 154 (precision 0.40,
recall 0.72) — the same figures the sweep table below reports on its `shipped` row, which is
how the two sections are kept honest against each other. These replace the **+0.505** / **+0.585**
and 287 promotes measured when this section was first written: that was before the
requirements-zone weighting and the dominant-stack penalty landed, and both changed the scorer.
Re-measure after any change to a scoring dimension, because this paragraph claims what the code
does *now*, not what it did when the labels were exported.

The file and the column agree to the digit, which is the check that the two measurement paths
are the same measurement:

```bash
python3 calibrate.py --db /tmp/cal-0.db                                  # jobs.score
python3 calibrate.py --db /tmp/cal-0.db --labels calibration/claude-labels.json
```

## The corpus

| | n |
|---|---|
| scored rows | 806 |
| with JD text | 557 (69%) |
| no JD text | 249 (31%) |
| Claude at 7+ | 137 (17.0%) |

## What the shipped code produces

| Metric | Value |
|---|---|
| Spearman rho, all rows | **+0.517** |
| Spearman rho, with-JD slice | **+0.575** |
| Rows the heuristic promotes at 7+ | **261** against Claude's 137 |
| Of Claude's 137, also promoted | **103** (recall 0.75) |
| Extra rows admitted | **158** (precision 0.39) |

Score distribution:

| score | Claude | heuristic |
|---|---|---|
| 1 | 42 | 11 |
| 2 | 123 | 21 |
| 3 | 126 | 71 |
| 4 | 110 | 227 |
| 5 | 135 | 92 |
| 6 | 133 | 123 |
| 7 | 84 | 133 |
| 8 | 39 | 74 |
| 9 | 14 | 54 |

The two distributions have different shapes, not just different means. Claude's mass sits at
2-6; the heuristic's sits at 4 with a second lump at 7-9. **The heuristic's scale runs roughly
two points hotter at the top**: of its 54 nines, Claude scored 34 at 7+ and only 10 at or below
5. So a heuristic 9 is closer to a Claude 7 than to a Claude 9.

## Choosing `score_threshold`

The threshold gates the digest, drafting and tailoring together, so this is the one number worth
deliberating. Measured against Claude's 7+ as the target set:

| heuristic cut | promotes | of Claude's 137 | missed | extra | precision | recall |
|---|---|---|---|---|---|---|
| 5 | 476 | 125 | 12 | 351 | 0.26 | 0.91 |
| 6 | 384 | 119 | 18 | 265 | 0.31 | 0.87 |
| **7 (shipped)** | **261** | **103** | **34** | **158** | **0.39** | **0.75** |
| 8 | 128 | 63 | 74 | 65 | 0.49 | 0.46 |
| 9 | 54 | 34 | 103 | 20 | 0.63 | 0.25 |

**Recommendation: leave it at 7.** Raising it to 8 buys precision 0.39 to 0.49 and costs recall
0.75 to 0.46 — it drops 74 of the 137 reqs Claude would have surfaced. In a job search a missed
good req is an interview that never happens; an extra mediocre one costs a few seconds of
reading. The volumes are also bounded in a way the raw count hides: `draft.py` and `tailor.py`
cap each run at `--limit 6`, and `store.promoted` orders by score descending, so the six reqs
that actually get a draft come off the 9 and 8 band where precision is best (0.63 at 9). Raising
the bar would not reduce the number of drafts; it would only change which six.

If the digest feels long, raise `digest_below_bar` or lower `--limit`, not the threshold. That
is the same advice the profile comment already gives for a different reason.

## The weight sweep, and why the shipped weights did not change

`calibrate.py --sweep` runs coordinate ascent over ten named weights — the dimensions the
requirements-zone and dominant-stack work just added, plus the three that set the overall level
— seven candidate values each, three passes, hard-capped at 300 scoring passes. The objective is
**precision at the 7+ cut subject to recall not falling below the baseline measured in the same
run**, tie-broken on the with-JD Spearman. Recall is a constraint rather than a term because a
sweep allowed to trade it away "wins" by promoting almost nothing.

```bash
cp jobhunt.db /tmp/cal-5.db
python3 calibrate.py --db /tmp/cal-5.db --labels calibration/claude-labels.json --sweep
```

On all 918 rows it reports a win, 177 evaluations, 4 candidates refused by the validators:

| | precision@7+ | recall@7+ | rho (all) | rho (JD) | promotes |
|---|---|---|---|---|---|
| shipped | 0.402 | 0.721 | +0.542 | +0.622 | 276 |
| sweep best | **0.430** | 0.721 | +0.551 | +0.623 | 258 |

| weight | before | sweep |
|---|---|---|
| `overlap_preamble_mult` | 0.6 | 0.3 |
| `off_lane_max` | -2 | -6 |
| `dominant_stack_max` | -2 | -3.5 |
| `dominant_stack_ratio` | 1.5 | 0.5 |
| `yoe_per_year_over` | -0.55 | -0.4 |

**Those values are not shipped.** The sweep scores every candidate on all 918 labelled rows and
then reports the best of them, so its +0.028 is an in-sample number: there is no holdout, and
a flat objective surface plus 300 evaluations is exactly the setup in which a local search
reports the noise it fitted. So the win was tested the only way it can be — split the corpus in
half on the sorted key (alternating, so both halves carry the same mix of sources and bands),
sweep on one half, measure the winning values on the other.

| trained on | tested on | precision@7+ | recall@7+ | rho (JD) |
|---|---|---|---|---|
| — (shipped) | B | 0.436 | 0.747 | +0.658 |
| A | B | 0.449 | **0.701** | +0.614 |
| — (shipped) | A | 0.362 | 0.687 | +0.577 |
| B | A | 0.380 | 0.687 | +0.602 |

One transfer holds and one does not. The B-trained weights improve A (+0.018 precision, recall
held, rho up). The A-trained weights **fail the sweep's own recall constraint** on B: recall
falls 0.747 to 0.701, and the with-JD rho falls with it, so measured on rows it had not seen
that configuration is not admissible at all.

The weight values themselves say the same thing more plainly. Trained on two halves of one
corpus, the sweep picks:

| weight | shipped | A-trained | B-trained | all-918 |
|---|---|---|---|---|
| `overlap_preamble_mult` | 0.6 | 0 | 0.3 | 0.3 |
| `off_lane_max` | -2 | -6 | -3 | -6 |
| `dominant_stack_max` | -2 | -6.5 | -3.5 | -3.5 |
| `dominant_stack_ratio` | 1.5 | 1 | 0.5 | 0.5 |
| `yoe_per_year_over` | -0.55 | **-1.15** | **-0.4** | **-0.4** |

Four of the five move in a consistent direction on both halves — discount the preamble harder,
penalise off-lane work and a foreign dominant stack harder, let the dominant-stack rule fire
more readily. That much is probably real. But the *magnitudes* are not identified: `off_lane_max`
lands anywhere between -3 and -6, a factor of two, and `yoe_per_year_over` moves in **opposite
directions** on the two halves (-1.15 against -0.4, around a shipped -0.55). A weight whose
optimum flips direction between random halves of the corpus is not something 918 rows can
measure.

**Decision: keep the shipped weights.** The in-sample gain is +0.028 precision for 18 fewer
promoted reqs, and the evidence that it survives contact with new reqs is one transfer out of
two. `off_lane_max: -6` is a tripled penalty — one off-lane title term would move a req three
score bands — on an estimate that halves when it is re-measured on different rows. Shipping it
would be shipping the corpus, not the judgement.

What would settle it, and is a separate piece of work rather than a weight tweak: label a fresh
holdout (reqs scored after the export) with `engine: claude`, sweep on the existing 918, and
measure once on the new rows. Until then the honest reading is that the shipped weights are at a
local optimum this corpus cannot distinguish from its neighbours, and the sweep is in the repo so
the question can be re-asked cheaply when the corpus grows.

The harness is also usable as a one-off: `--sweep-budget` and `--sweep-passes` bound the run,
and it never writes a config file — it prints a YAML fragment to paste by hand, deliberately,
so that applying a sweep result is always a decision somebody made.

## Zero Claude invocations on the heuristic engine, verified by execution

`engine: heuristic` is supposed to shell out to nothing. Reading the code does not prove that —
any stage could reach `claudecall` by a path the reader did not follow, and a `claude` binary
that is simply absent from PATH proves nothing either, because a stage that tried and failed
could swallow the error. The check below settles it by execution. It is a manual one: it needs a
PATH shim and a database copy, neither of which belongs in pytest.

```bash
mkdir -p /tmp/shadow
cat > /tmp/shadow/claude <<'SH'
#!/bin/sh
echo "$(date +%H:%M:%S) claude invoked with: $@" >> /tmp/shadow/calls.log
exit 97
SH
chmod +x /tmp/shadow/claude
rm -f /tmp/shadow/calls.log

# Confirm the trap itself works before trusting its silence.
PATH=/tmp/shadow:$PATH claude -p test ; cat /tmp/shadow/calls.log ; rm -f /tmp/shadow/calls.log

cp jobhunt.db /tmp/cal-7.db
PATH=/tmp/shadow:$PATH python3 run.py --engine heuristic --db /tmp/cal-7.db
```

**Pass condition: every stage reports `ok`, and `/tmp/shadow/calls.log` does not exist.** The
shadow exits 97 rather than 0 on purpose — anything that called it fails loudly instead of
absorbing the error, and the log file appears either way.

Self-testing the shim first is not ceremony. A silent `calls.log` is only evidence if the shim
would have written one, and a typo in the heredoc or a missing `chmod +x` produces exactly the
same silence as a clean run.

Last run, on all five stages against a copy:

| stage | result | work actually done |
|---|---|---|
| fetch | ok, 459s | polled the boards, seeded, **recovered JD text for 85/120** enrich candidates |
| score | ok | **37 reqs scored** through the heuristic engine, in 1 batch |
| draft | ok | **6 drafts written**, 3 reqs held back by the per-company cap |
| tailor | ok | **6 resumes tailored** |
| digest | ok | wrote `digest/2026-10-15.md` |

`calls.log` was not created. The work matters as much as the exit codes: a score stage with
nothing to score never reaches an engine at all, so a 0-row pass would prove nothing. 37 reqs
went through `score_one` and 12 generated files were written without `claude` being invoked once.

The same pass is also the migration check. A copy of the live database arrives with none of the
columns this work added, so the run exercises `store._migrate` against real data: `label_score`,
`label_reason`, `enrich_attempts`, `enrich_last`, `enrich_note` and `scored_by` were all added
in place, 918 existing scores survived untouched, and the 37 new rows carry
`scored_by='heuristic'` — the column that makes a heuristic score distinguishable from a Claude
label from then on.

`run.py --db PATH` passes the path to every stage, which is what makes a full-pipeline check
possible without touching the live database.

## The two round-3 review findings

### HIGH-2: fourteen skill terms with no resume evidence

`sqs`, `s3`, `ecs`, `cloudformation`, `sns`, `high throughput`, `low latency`, `ci/cd`, `rag`,
`langchain`, `bedrock`, `typescript`, `scalable`, `restful` appear nowhere in `config/resume.md`
or the profile's `resume_summary`. They summed to **5.00** against `overlap_saturate` 4.0, so a
JD naming only AWS service nouns plus boilerplate earned **+2.63 of the maximum +3.00** overlap
bonus with no evidence behind it.

Fixed by moving all fourteen into a `boilerplate` tier whose entire summed contribution is
capped at `boilerplate_cap` 0.5, so the same JD now earns at most **+0.375**. A context-build
check warns if any term in `core`, `lane_b` or `secondary` loses its resume evidence.

Measured effect, both runs on the same copy:

| | before | after |
|---|---|---|
| rho, all rows | +0.516 | +0.517 |
| rho, with JD | +0.577 | +0.575 |
| 9s | 56 | **54** |
| 8+ | 136 | 128 |
| 7+ | 266 | **261** |
| recall at 7+ | 0.75 | 0.75 |

**The fix is correct and it did not cause the nine pile-up.** The review called the fourteen
terms the "likely mechanism" behind it; measurement says no. Only 6 of 806 rows match
unevidenced terms *exclusively*, and the pile-up moved by two rows.

What actually drives it: 50 of the 54 nines have JD text, and they reach ~9 points by stacking
legitimate dimensions — base 3.7, source prior, YOE in band +1.3, saturated overlap +3.0,
in-band title +0.5, target employer +0.8, home geo +0.6. Any in-band Java-shaped JD at a target
company in Hyderabad lands there. It is a **scale** problem, not a free-points problem, which is
why the threshold table above is the actionable part and not a weight tweak. The ceiling
truncates only 9 of 806 rows, so this is not clamp compression either.

One vocabulary fix was tested and **rejected**: adding `angular`, `react`, `reactjs` and `.net`
to `off_lane_titles` moved the nines from 54 to 50 and rho by −0.003. Not worth the churn, and
chasing it further would be sweeping weights against the labels, which the design was told not
to do. It stays available as a profile override for an owner who disagrees.

### HIGH-1: the tailorer claim check

The check applies to the **TAILORED RESUME section only**. GAPS, WHAT CHANGED and KEYWORDS ADDED
are the tool's commentary and are exempt from `MASTER_CANON`. Under the all-sections reading
every row was refused and the stage wrote nothing, because GAPS exists precisely to name a JD
term the resume cannot claim.

Never-claim terms still cannot reach the resume body, and structurally rather than by assertion:
`MASTER_CANON` is built from the resume body only, so a line quoting one is in neither the canon
nor the template-literal set. `tests/test_resume_master.py` asserts both halves — a GAPS entry
naming `go` is written, and the same row's resume body contains no never-claim term.

## Dimensions, and how often each fires

Across all 806 rows: base 806, source 806, overlap 545, yoe 437, title_band 351, target 261,
never_claim 210, geo 158, cs_degree 100, off_lane 58, services 5, ceiling 4.

On the current 918-row corpus, which includes the `dominant_stack` dimension the 806-row
snapshot above predates: base 918, source 918, overlap 604, yoe 481, title_band 390, target 277,
never_claim 228, geo 173, cs_degree 104, off_lane 63, **dominant_stack 61**, services 5,
ceiling 4.

`dominant_stack` was designed expecting it to fire on materially more rows than `off_lane`'s 63,
on the reasoning that a thin title vocabulary misses foreign-stack reqs that the JD body would
catch. It fires on **61** — about the same, not more. But the two are largely different rows:

| | rows |
|---|---|
| `off_lane` only | 36 |
| `dominant_stack` only | **34** |
| both | 27 |
| either | 97 |

So the premise that JD-body evidence is much *broader* than title evidence did not hold, while
the premise that it is *different* did: the dimension catches 34 reqs the title check misses
entirely, and the 27-row overlap is where a React-titled req also has a React-dominant JD, which
is the intended stacking. Worth keeping, and not the large win it was scoped as.

`services` (5) and `ceiling` (4) barely fire. Both are kept deliberately: `exclusions.companies`
already drops the named agencies so `services` only catches unnamed ones, and the fetch gate
already filters senior titles so `ceiling` only catches what `exclude_overrides` and the seed
path readmit. They are guards on gaps in another module's filter, and guards are expected to be
quiet. `--rescore` re-scores the rows they do catch.

Two dimensions ship at weight **0.0** and are genuinely consumed anyway:

- **`comp_risk_employer`.** Measured: the `comp_risk` bloc scored *higher* than the other
  targets (mean 5.25 against 5.17), because JP Morgan Chase dominates it and most of its reqs
  are genuinely in band. A negative weight would be fitting a rule the labels contradict. At
  zero it still emits its `score_reason` clause and selects the `comp_risk` speed bucket.
- **`speed_points`.** Time-to-offer is not derivable from a JD, so a weight here is a weight on
  a company-shape guess. Wired and sweepable rather than written off, which is a stated
  divergence from `score.RUBRIC`'s "speed is a scoring dimension, not a footnote".

## YOE parsing

437 of 557 with-JD rows (78%) yield a figure. The remaining 22% get no YOE clause and no YOE
points, which is the right default: silence is not evidence of being in band.

Noise suppression is required rather than optional. `100-year company, we are proudly
building...` appears in the live data and a naive pattern reads it as a 100-year requirement;
`NOISE` blanks that shape and figures above 25 are discarded as a second net.
