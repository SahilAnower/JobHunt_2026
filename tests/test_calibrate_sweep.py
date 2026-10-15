"""
The weight sweep: its objective, its recall constraint, and its refusal to score a candidate
the real scorer would reject.

DATA SAFETY. Nothing here opens a database. `run_sweep` takes an already-materialised row list,
which is what makes it testable at all — the rows below are plain dicts, because `score_one`
reads a row through `_row` and never requires a sqlite3.Row.

What is worth testing here is not "does ascent find the optimum" — it is a local search over a
corpus-specific surface and the optimum is not knowable from a unit test. It is the three
properties that decide whether a reported win means anything:

  1. The objective prefers precision, and uses the with-JD Spearman only to break a tie.
  2. Recall is a hard constraint against the baseline, not a term to be traded away. A sweep
     free to trade recall "wins" by promoting four reqs and getting three right.
  3. A candidate value that `build_context` would refuse is never scored, so every weight the
     sweep recommends is a weight the owner can actually paste into their profile.
"""

from __future__ import annotations

import pytest

import calibrate
import engine_heuristic as eh
import jobprofile

EXAMPLE_PROFILE = calibrate.ROOT / "config" / "profile.example.yaml"


@pytest.fixture(scope="module")
def profile():
    return jobprofile.load(EXAMPLE_PROFILE)


def metrics(precision, recall, rho_jd):
    return {"precision": precision, "recall": recall, "rho_jd": rho_jd}


# --------------------------------------------------------------------------- the objective

def test_the_objective_prefers_higher_precision():
    worse = metrics(0.40, 0.72, +0.62)
    better = metrics(0.45, 0.72, +0.62)
    assert calibrate.sweep_objective(better) > calibrate.sweep_objective(worse)


def test_at_equal_precision_the_objective_breaks_the_tie_on_the_with_jd_rho():
    flat = metrics(0.40, 0.72, +0.60)
    sharper = metrics(0.40, 0.72, +0.65)
    assert calibrate.sweep_objective(sharper) > calibrate.sweep_objective(flat)


def test_precision_outranks_rho_rather_than_being_averaged_with_it():
    # A configuration that correlates better overall but admits more junk past the cut must
    # lose: the cut is what gates drafting, and rho is only the tie-break.
    precise = metrics(0.45, 0.72, +0.50)
    correlated = metrics(0.40, 0.72, +0.70)
    assert calibrate.sweep_objective(precise) > calibrate.sweep_objective(correlated)


def test_a_nan_rho_does_not_crash_the_comparison():
    # The with-JD slice can be empty or single-rowed on a small corpus, and spearman returns
    # NaN there. NaN in a tuple comparison is silently false in both directions, which would
    # make the ascent's "is this better" test unreliable rather than loud.
    m = metrics(0.40, 0.72, float("nan"))
    assert calibrate.sweep_objective(m) == (0.40, 0.0)


# --------------------------------------------------------------------------- the constraint

def test_recall_below_the_baseline_is_inadmissible_however_precise():
    # The failure mode the constraint exists for: precision 1.0 by promoting almost nothing.
    assert not calibrate.sweep_admissible(metrics(1.0, 0.50, +0.9), baseline_recall=0.72)


def test_recall_equal_to_the_baseline_is_admissible():
    assert calibrate.sweep_admissible(metrics(0.45, 0.72, +0.6), baseline_recall=0.72)


def test_recall_above_the_baseline_is_admissible():
    assert calibrate.sweep_admissible(metrics(0.45, 0.80, +0.6), baseline_recall=0.72)


def test_the_constraint_tolerates_float_dust_at_exact_equality():
    # recall is a ratio of small integers recomputed per candidate, so an identical recall can
    # differ in the last bit. Without the epsilon the sweep would reject configurations that
    # are exactly as good as the baseline.
    assert calibrate.sweep_admissible(metrics(0.45, 0.72 - 1e-12, +0.6), baseline_recall=0.72)


# --------------------------------------------------------------------------- the validators

def test_a_positive_penalty_candidate_is_refused_rather_than_scored(profile):
    # off_lane_max is in PENALTY_KEYS, so a positive value would ADD points on a dimension
    # named for removing them. build_context is the same gate the real scorer passes, so a
    # refusal here is a guarantee the recommendation is paste-able.
    assert calibrate.sweep_context(profile, None, {"off_lane_max": 0.5}) is None


def test_a_zero_saturation_candidate_is_refused(profile):
    # overlap_saturate is a divisor: zero is a ZeroDivisionError inside score_one.
    assert calibrate.sweep_context(profile, None, {"overlap_saturate": 0.0}) is None


def test_a_negative_bonus_candidate_is_refused(profile):
    assert calibrate.sweep_context(profile, None, {"overlap_max": -1.0}) is None


def test_a_valid_candidate_builds_a_context(profile):
    ctx = calibrate.sweep_context(profile, None, {"off_lane_max": -3.0})
    assert ctx is not None
    assert ctx.w["off_lane_max"] == -3.0


def test_every_sweepable_weight_has_a_shipped_value_to_centre_on():
    # The spec names keys by hand, so a rename in DEFAULTS would otherwise surface as a
    # KeyError in the middle of an eleven-minute run.
    merged = eh.DEFAULTS
    for name, where, step in calibrate.SWEEP_SPEC:
        src = merged["weights"] if where == "weights" else merged
        assert name in src, f"{name} is in SWEEP_SPEC but not in DEFAULTS"
        assert step > 0


# --------------------------------------------------------------------------- overrides

def test_with_overrides_does_not_mutate_the_profile_it_is_given(profile):
    before = profile["scoring"]["weights"]["off_lane_max"]
    calibrate.with_overrides(profile, {"off_lane_max": -9.0})
    assert profile["scoring"]["weights"]["off_lane_max"] == before


def test_with_overrides_puts_a_top_level_key_outside_the_weights_block(profile):
    # `base` and `base_no_jd` live under `scoring:` directly. Writing them into
    # scoring.weights would make _check_weights reject them as unknown bonus keys, and the
    # sweep would silently score the shipped base on every candidate.
    out = calibrate.with_overrides(profile, {"base": 4.0, "off_lane_max": -3.0})
    assert out["scoring"]["base"] == 4.0
    assert "base" not in out["scoring"]["weights"]
    assert out["scoring"]["weights"]["off_lane_max"] == -3.0


def test_the_candidate_list_is_seven_values_centred_on_the_current_one():
    vals = calibrate.sweep_candidates(3.0, 0.5)
    assert len(vals) == 7
    assert sorted(vals) == [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]
    # Nearest first, so a truncated budget is spent close to the shipped configuration rather
    # than on the extremes.
    assert vals[0] == 3.0


# --------------------------------------------------------------------------- end to end

def rows_for_sweep():
    """
    A corpus small enough to sweep in a test. Two reqs the owner wants, two off-lane ones
    labelled low, so a harsher off_lane penalty is genuinely the right answer here.
    """
    good = ("Required: 3 years of experience with java, spring boot, aws, microservices, "
            "kafka and distributed systems.")
    offlane = ("Requirements: 2 years of experience. angular, angular, angular, rxjs, ngrx, "
               "typescript. Some java exposure helps.")
    return [
        {"key": "a::1", "company": "Acme", "title": "Software Engineer II", "source": "seed",
         "description": good, "geo": "home"},
        {"key": "a::2", "company": "Acme", "title": "Software Engineer II", "source": "seed",
         "description": good, "geo": "home"},
        {"key": "a::3", "company": "Acme", "title": "Frontend Engineer", "source": "seed",
         "description": offlane, "geo": "home"},
        {"key": "a::4", "company": "Acme", "title": "Frontend Engineer", "source": "seed",
         "description": offlane, "geo": "home"},
    ]


def test_the_sweep_never_reports_a_best_worse_than_its_own_baseline(profile):
    """
    The guarantee that makes a reported win readable. Ascent starts AT the baseline and only
    replaces it on a strict improvement, so `best` is the baseline itself when nothing helps.
    """
    rows = rows_for_sweep()
    labels = {"a::1": 9, "a::2": 8, "a::3": 2, "a::4": 3}
    res = calibrate.run_sweep(rows, profile, None, labels, 7, budget=40, passes=1,
                              log=lambda *a, **k: None)
    assert calibrate.sweep_objective(res["best"]) >= calibrate.sweep_objective(res["baseline"])
    assert res["best"]["recall"] >= res["baseline"]["recall"] - 1e-9


def test_the_sweep_respects_its_evaluation_budget(profile):
    res = calibrate.run_sweep(rows_for_sweep(), profile, None,
                              {"a::1": 9, "a::2": 8, "a::3": 2, "a::4": 3}, 7,
                              budget=5, passes=3, log=lambda *a, **k: None)
    assert res["evals"] <= 5


def test_the_sweep_is_deterministic(profile):
    labels = {"a::1": 9, "a::2": 8, "a::3": 2, "a::4": 3}
    a = calibrate.run_sweep(rows_for_sweep(), profile, None, labels, 7, budget=40, passes=1,
                            log=lambda *a, **k: None)
    b = calibrate.run_sweep(rows_for_sweep(), profile, None, labels, 7, budget=40, passes=1,
                            log=lambda *a, **k: None)
    assert a["values"] == b["values"]
    assert a["evals"] == b["evals"]


def test_the_sweep_reports_the_shipped_values_as_its_starting_point(profile):
    res = calibrate.run_sweep(rows_for_sweep(), profile, None,
                              {"a::1": 9, "a::2": 8, "a::3": 2, "a::4": 3}, 7,
                              budget=3, passes=1, log=lambda *a, **k: None)
    merged = eh._merge(eh.DEFAULTS, profile.get("scoring") or {})
    assert res["base_values"]["off_lane_max"] == merged["weights"]["off_lane_max"]
    assert res["base_values"]["base"] == merged["base"]


# --------------------------------------------------------------------------- the live-DB guard

def test_calibrate_refuses_the_live_database_by_filename(tmp_path):
    """
    The guard the whole harness rests on, and it was ineffective from a worktree.

    calibrate.py compared `--db` against ROOT/"jobhunt.db". In a git worktree that file does
    not exist, so the comparison never matched the real database one directory up and
    `--db ../../jobhunt.db` went straight through. Matching on the filename is what makes the
    refusal hold however the file is reached — absolute, relative, or through a symlink.
    """
    live = tmp_path / "jobhunt.db"
    live.write_bytes(b"")
    with pytest.raises(SystemExit) as e:
        calibrate.main(["--db", str(live)])
    assert "refusing to read" in str(e.value)


def test_calibrate_refuses_a_relative_path_to_the_live_database():
    with pytest.raises(SystemExit) as e:
        calibrate.main(["--db", "../../jobhunt.db"])
    assert "refusing to read" in str(e.value)


def test_the_refusal_names_the_copy_command_to_run_instead(tmp_path):
    live = tmp_path / "jobhunt.db"
    live.write_bytes(b"")
    with pytest.raises(SystemExit) as e:
        calibrate.main(["--db", str(live)])
    assert "cp " in str(e.value) and "/tmp/cal.db" in str(e.value)


def test_a_copy_under_any_other_name_is_not_refused(tmp_path):
    """The guard must not block the measurement it exists to protect."""
    copy = tmp_path / "cal-copy.db"
    import store
    with store.connect(copy) as c:
        c.commit()
    # Gets past the refusal and fails later, on there being no labels — not on the path.
    with pytest.raises(SystemExit) as e:
        calibrate.main(["--db", str(copy)])
    assert "refusing to read" not in str(e.value)
