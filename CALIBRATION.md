# Calibration — the heuristic engine against Claude's labels

The `jobs.score` column holds 806 reqs that `engine: claude` already judged. That is the only
ground truth this project has, and it is destructible: `score.py --rescore` overwrites it. So
every number below was measured against a **copy**, and `calibrate.py` refuses to open the live
`jobhunt.db` at all.

Re-derive any of it:

```bash
cp jobhunt.db /tmp/cal.db
python3 calibrate.py --db /tmp/cal.db
python3 calibrate.py --db /tmp/cal.db --before-high2     # the pre-fix skill weights
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
