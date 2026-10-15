"""
The rescore gate. The one destructive operation in this project, and what stops it.

DATA SAFETY. --rescore overwrites jobs.score, which holds 918 rows of Claude's judgement that
cannot be re-derived without spending the owner's subscription on the whole corpus again. Every
test here builds its own database under tmp_path and passes --db; none of them runs without it.
Nothing here calls claude: the engine is always `heuristic`, which is a pure function.

The four refusals, each of which exists because the failure it prevents is unrecoverable:

  1. no label export        -> the labels have no second copy at all
  2. an export too small    -> it was taken before the corpus grew, so the difference is lost
  3. an unreadable export   -> a file that exists is not the same as labels that are readable
  4. no backup              -> --backup PATH, or --i-have-a-backup said out loud

Plus two that protect the backup itself: not inside the repository (a database copy there is
one `git add -A` from a remote, which is how config/profile.yaml.bak once leaked), and never
on top of an existing file.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import jobprofile
import score
import store

EXAMPLE_PROFILE = score.ROOT / "config" / "profile.example.yaml"

JD = ("Responsibilities: build backend services in Java and Spring Boot on AWS. "
      "Qualifications: 2+ years with distributed systems. "
      + "We run high throughput systems on Kafka. " * 14)


@pytest.fixture()
def db(tmp_path):
    """Four scored, still-open rows and one unscored, in a throwaway file."""
    path = tmp_path / "copy.db"
    with store.connect(path) as conn:
        for i, sc in enumerate([9, 7, 4, 2, None], start=1):
            job = {"company": f"Co{i}", "title": "Software Engineer", "req_id": str(i),
                   "url": f"https://example.test/{i}", "source": "greenhouse",
                   "description": JD}
            store.upsert_job(conn, job)
            if sc is not None:
                store.set_score(conn, store.job_key(f"Co{i}", "Software Engineer",
                                                    f"https://example.test/{i}", str(i)),
                                sc, "claude said so", scored_by="claude")
    return path


@pytest.fixture()
def labels(tmp_path, db):
    """A label export that covers every row in `db`."""
    path = tmp_path / "labels.json"
    with store.connect(db) as conn:
        rows = [{"key": r["key"], "score": r["score"], "score_reason": r["score_reason"],
                 "has_jd": True, "desc_chars": len(r["description"] or "")}
                for r in conn.execute("SELECT * FROM jobs WHERE score IS NOT NULL")]
    path.write_text(json.dumps({"exported_at": "now", "rows": rows}))
    return path


@pytest.fixture()
def example_profile(monkeypatch):
    """
    main() reads the owner's gitignored config/profile.yaml. The tracked example stands in for
    it, so these tests measure the same thing on any checkout.
    """
    import yaml
    profile = yaml.safe_load(EXAMPLE_PROFILE.read_text())
    monkeypatch.setattr(jobprofile, "load", lambda *a, **k: profile)
    return profile


def scores(path):
    with store.connect(path) as conn:
        return {r["key"]: (r["score"], r["label_score"], r["scored_by"])
                for r in conn.execute("SELECT * FROM jobs")}


def run(argv):
    return score.main(argv)


# --- refusal 1: no export

def test_no_label_export_refuses_loudly_and_writes_nothing(db, tmp_path, capsys,
                                                           example_profile):
    before = scores(db)
    with pytest.raises(SystemExit) as e:
        run(["--rescore", "--db", str(db), "--engine", "heuristic",
             "--labels", str(tmp_path / "absent.json"), "--i-have-a-backup"])
    said = str(e.value)
    assert "REFUSING to rescore" in said
    assert "python3 calibration_labels.py --export" in said
    assert "=" * 78 in said, "the refusal has to be impossible to scroll past"
    assert scores(db) == before


# --- refusal 2: an export that does not cover the corpus

def test_an_export_smaller_than_the_corpus_refuses(db, labels, example_profile):
    stale = json.loads(labels.read_text())
    stale["rows"] = stale["rows"][:1]
    labels.write_text(json.dumps(stale))
    before = scores(db)

    with pytest.raises(SystemExit) as e:
        run(["--rescore", "--db", str(db), "--engine", "heuristic",
             "--labels", str(labels), "--i-have-a-backup"])
    said = str(e.value)
    assert "smaller than the set it has to protect" in said
    assert "unprotected if this ran" in said
    assert scores(db) == before


def test_an_export_larger_than_the_corpus_is_fine(db, labels, tmp_path, example_profile):
    """More labels than rows is a superset, not a shortfall."""
    data = json.loads(labels.read_text())
    data["rows"].append({"key": "ghost::1", "score": 5, "score_reason": "r",
                         "has_jd": False, "desc_chars": 0})
    labels.write_text(json.dumps(data))

    rc = run(["--rescore", "--db", str(db), "--engine", "heuristic",
              "--labels", str(labels), "--i-have-a-backup"])
    assert rc == 0


# --- refusal 3: an unreadable export

def test_an_unreadable_export_refuses(db, tmp_path, example_profile):
    bad = tmp_path / "labels.json"
    bad.write_text("[not an export]")
    before = scores(db)

    with pytest.raises(SystemExit) as e:
        run(["--rescore", "--db", str(db), "--engine", "heuristic",
             "--labels", str(bad), "--i-have-a-backup"])
    assert "not readable as a label export" in str(e.value)
    assert scores(db) == before


# --- refusal 4: no backup

def test_no_backup_and_no_confirmation_refuses(db, labels, example_profile):
    before = scores(db)
    with pytest.raises(SystemExit) as e:
        run(["--rescore", "--db", str(db), "--engine", "heuristic", "--labels", str(labels)])
    said = str(e.value)
    assert "REFUSING to rescore without a backup" in said
    assert "--backup" in said and "--i-have-a-backup" in said
    assert scores(db) == before


def test_backup_is_taken_before_anything_is_overwritten(db, labels, tmp_path,
                                                        example_profile):
    before = scores(db)
    backup = tmp_path / "out" / "pre-rescore.db"

    rc = run(["--rescore", "--db", str(db), "--engine", "heuristic",
              "--labels", str(labels), "--backup", str(backup)])
    assert rc == 0
    assert backup.exists()

    # The backup holds the pre-rescore scores, which is the whole point of taking it.
    raw = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    raw.row_factory = sqlite3.Row
    backed_up = {r["key"]: r["score"] for r in raw.execute("SELECT * FROM jobs")}
    raw.close()
    assert backed_up == {k: v[0] for k, v in before.items()}


def test_a_backup_inside_the_repository_refuses(db, labels, example_profile):
    before = scores(db)
    with pytest.raises(SystemExit) as e:
        run(["--rescore", "--db", str(db), "--engine", "heuristic",
             "--labels", str(labels), "--backup", str(score.ROOT / "nope.db")])
    assert "REFUSING to back up inside the repository" in str(e.value)
    assert not (score.ROOT / "nope.db").exists()
    assert scores(db) == before


def test_a_backup_will_not_overwrite_an_existing_file(db, labels, tmp_path, example_profile):
    existing = tmp_path / "already-here.db"
    existing.write_text("someone else's backup")
    with pytest.raises(SystemExit) as e:
        run(["--rescore", "--db", str(db), "--engine", "heuristic",
             "--labels", str(labels), "--backup", str(existing)])
    assert "REFUSING to overwrite an existing backup" in str(e.value)
    assert existing.read_text() == "someone else's backup"


# --- what a permitted rescore does

def test_a_permitted_rescore_labels_every_row_it_overwrites(db, labels, example_profile):
    before = scores(db)
    rc = run(["--rescore", "--db", str(db), "--engine", "heuristic",
              "--labels", str(labels), "--i-have-a-backup"])
    assert rc == 0

    after = scores(db)
    for key, (old_score, _, _) in before.items():
        new_score, label, by = after[key]
        if old_score is None:
            continue
        assert label == old_score, "the old score has to survive in label_score"
        assert by == "heuristic"
        assert new_score is not None


def test_a_second_rescore_leaves_the_labels_alone(db, labels, example_profile):
    """
    The label is Claude's, and a second rescore must neither replace it nor invent a new one
    out of the first rescore's output. A heuristic score is re-derivable from the row, so it is
    not ground truth and never becomes a label.
    """
    run(["--rescore", "--db", str(db), "--engine", "heuristic",
         "--labels", str(labels), "--i-have-a-backup"])
    first = scores(db)
    run(["--rescore", "--db", str(db), "--engine", "heuristic",
         "--labels", str(labels), "--i-have-a-backup"])
    second = scores(db)

    assert {k: v[1] for k, v in second.items()} == {k: v[1] for k, v in first.items()}
    # The row that was unscored before the first rescore has a heuristic score and no label.
    unscored_before = [k for k, v in first.items() if v[1] is None]
    assert len(unscored_before) == 1
    assert second[unscored_before[0]][1] is None


def test_a_dry_run_needs_no_gate_and_writes_nothing(db, tmp_path, example_profile):
    before = scores(db)
    rc = run(["--rescore", "--dry-run", "--db", str(db), "--engine", "heuristic",
              "--labels", str(tmp_path / "absent.json")])
    assert rc == 0
    assert scores(db) == before


def test_the_count_about_to_be_overwritten_is_printed(db, labels, capsys, example_profile):
    run(["--rescore", "--db", str(db), "--engine", "heuristic",
         "--labels", str(labels), "--i-have-a-backup"])
    said = capsys.readouterr().out
    assert "will OVERWRITE 4 existing score(s)" in said
    assert "copied 4 score(s) into label_score/label_reason" in said


def test_scoring_writes_to_the_db_it_was_given(db, labels, tmp_path, monkeypatch,
                                               example_profile):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "must-not-be-touched.db")
    run(["--rescore", "--db", str(db), "--engine", "heuristic",
         "--labels", str(labels), "--i-have-a-backup"])
    assert not (tmp_path / "must-not-be-touched.db").exists()


# --- the row selection

def test_rescore_takes_every_still_open_row_by_default(db):
    with store.connect(db) as conn:
        conn.execute("UPDATE jobs SET still_open = 0 WHERE score = 2")
        rows = score.rescore_rows(conn, None)
    assert len(rows) == 4, "all still-open rows, scored or not, and no implicit LIMIT 100"


def test_rescore_honours_an_explicit_limit(db):
    with store.connect(db) as conn:
        rows = score.rescore_rows(conn, 2)
    assert len(rows) == 2
    assert [r["score"] for r in rows] == [9, 7], "highest-scored first"


def test_the_normal_path_still_defaults_to_a_hundred(db, example_profile, capsys):
    """Dropping --limit's default must not change plain `score.py`."""
    rc = run(["--db", str(db), "--engine", "heuristic"])
    assert rc == 0
    said = capsys.readouterr().out
    assert "1 job(s) to score" in said, "only the one unscored row"
