"""
The label export, and the label sources calibrate.py measures against.

DATA SAFETY. The thing under test here reads the live database by default, so every test
constructs its own SQLite file under tmp_path and passes it explicitly. Nothing here calls
calibration_labels.main() without --db, and nothing writes to a path named jobhunt.db.

The two properties worth the most care:

  1. --export must not mutate its source. It opens the DB through `file:...?mode=ro` rather
     than store.connect(), because store.connect() runs executescript(SCHEMA) and _migrate()
     against whatever it is handed — against the live file that would be a write. The test for
     it asserts the source DB still has no label_score column afterwards, which is the
     observable difference between the two ways of opening it.
  2. The provenance guard. A row counts as a Claude label only if the shipped heuristic does
     not produce its stored reason byte for byte. That is what stops a re-export, run after a
     rescore, from laundering the heuristic's own output into the ground truth file.
"""

from __future__ import annotations

import argparse
import hashlib
import json

import pytest

import calibrate
import calibration_labels as cl
import engine_heuristic as eh
import store

ROOT = cl.ROOT
EXAMPLE_PROFILE = ROOT / "config" / "profile.example.yaml"


def make_db(path, rows):
    """A real jobs table. `rows` is (req_id, score, reason, description)."""
    with store.connect(path) as conn:
        for req_id, score, reason, desc in rows:
            job = {"company": "Acme", "title": "Software Engineer", "req_id": req_id,
                   "url": f"https://example.test/{req_id}", "source": "greenhouse",
                   "description": desc}
            store.upsert_job(conn, job)
            key = store.job_key("Acme", job["title"], job["url"], req_id)
            if score is not None:
                store.set_score(conn, key, score, reason)
    return path


def key_for(req_id):
    return store.job_key("Acme", "Software Engineer", f"https://example.test/{req_id}", req_id)


def args(**kw):
    """The argparse namespace do_export/do_apply read, with the defaults they rely on."""
    base = {"db": None, "out": None, "profile": EXAMPLE_PROFILE, "force": False,
            "apply": None, "export": False}
    base.update(kw)
    return argparse.Namespace(**base)


JD = ("We are hiring. Responsibilities include building services. Qualifications: "
      "experience with Java and Spring Boot. " + "filler text to clear the length bar. " * 20)


# --- the export

def test_export_writes_every_scored_row_and_skips_the_unscored(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "good fit", JD), ("2", 3, "weak", None),
                                        ("3", None, None, JD)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))

    rows = cl.read_labels(out)["rows"]
    assert {r["key"] for r in rows} == {key_for("1"), key_for("2")}
    one = next(r for r in rows if r["key"] == key_for("1"))
    assert one["score"] == 7
    assert one["score_reason"] == "good fit"


def test_export_records_which_rows_had_a_jd(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD), ("2", 5, "r", "too short")])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))

    by_key = {r["key"]: r for r in cl.read_labels(out)["rows"]}
    assert by_key[key_for("1")]["has_jd"] is True
    assert by_key[key_for("1")]["desc_chars"] >= cl.fetch.JD_MIN_CHARS
    assert by_key[key_for("2")]["has_jd"] is False


def test_export_does_not_write_a_single_byte_to_its_source(tmp_path):
    """
    The source file is byte-identical afterwards. This is the property that makes it safe to
    point --export at the live database: store.connect() would have rewritten the schema.
    """
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    before = hashlib.sha256(src.read_bytes()).hexdigest()

    cl.do_export(args(db=src, out=tmp_path / "labels.json"))

    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_export_refuses_to_overwrite_an_existing_file(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))
    first = out.read_text()

    with pytest.raises(SystemExit) as e:
        cl.do_export(args(db=src, out=out))
    assert "--force" in str(e.value)
    assert out.read_text() == first


def test_force_overwrites_the_file(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))
    make_db(src, [("2", 4, "r2", JD)])

    cl.do_export(args(db=src, out=out, force=True))
    assert len(cl.read_labels(out)["rows"]) == 2


def test_export_refuses_a_db_that_does_not_exist(tmp_path):
    with pytest.raises(SystemExit) as e:
        cl.do_export(args(db=tmp_path / "nope.db", out=tmp_path / "l.json"))
    assert "--db" in str(e.value)


def test_export_refuses_a_db_with_no_scored_rows(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", None, None, JD)])
    with pytest.raises(SystemExit) as e:
        cl.do_export(args(db=src, out=tmp_path / "l.json"))
    assert "no scored rows" in str(e.value)


# --- the provenance guard

def test_the_guard_drops_a_row_whose_reason_the_heuristic_itself_produced(tmp_path):
    ctx = eh.build_context({}, None)
    src = make_db(tmp_path / "src.db", [("1", 7, "claude said so", JD), ("2", 6, "", JD)])

    # Store, as row 3's reason, exactly what the heuristic says about row 3.
    with store.connect(src) as conn:
        job = {"company": "Acme", "title": "Software Engineer", "req_id": "3",
               "url": "https://example.test/3", "source": "greenhouse", "description": JD}
        store.upsert_job(conn, job)
        row = conn.execute("SELECT * FROM jobs WHERE key = ?", (key_for("3"),)).fetchone()
        store.set_score(conn, key_for("3"), 5, eh.score_one(row, ctx).reason)

    with store.connect(src) as conn:
        kept, rejected = cl.collect(conn, ctx)

    assert rejected == [key_for("3")]
    assert {r["key"] for r in kept} == {key_for("1"), key_for("2")}


def test_the_guard_keeps_a_row_with_no_stored_reason(tmp_path):
    """An empty reason is not evidence of the heuristic; the heuristic always writes one."""
    ctx = eh.build_context({}, None)
    src = make_db(tmp_path / "src.db", [("1", 7, None, JD)])
    with store.connect(src) as conn:
        kept, rejected = cl.collect(conn, ctx)
    assert rejected == []
    assert kept[0]["score_reason"] == ""


def test_no_context_means_no_guard(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    with store.connect(src) as conn:
        kept, rejected = cl.collect(conn, None)
    assert len(kept) == 1 and rejected == []


# --- --apply

def test_apply_writes_the_labels_into_a_copy(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "good fit", JD), ("2", 3, "weak", None)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))

    dest = make_db(tmp_path / "copy.db", [("1", 4, "rescored", JD), ("2", 4, "rescored", None)])
    cl.do_apply(args(apply=dest, out=out))

    with store.connect(dest) as conn:
        got = {r["key"]: (r["score"], r["label_score"], r["label_reason"])
               for r in conn.execute("SELECT * FROM jobs")}
    assert got[key_for("1")] == (4, 7, "good fit")
    assert got[key_for("2")] == (4, 3, "weak")


def test_apply_refuses_the_live_database_by_name(tmp_path):
    out = tmp_path / "labels.json"
    out.write_text(json.dumps({"rows": [{"key": "k", "score": 7, "score_reason": "r"}]}))
    live = tmp_path / "jobhunt.db"
    make_db(live, [("1", 7, "r", JD)])

    with pytest.raises(SystemExit) as e:
        cl.do_apply(args(apply=live, out=out))
    assert "refusing to write" in str(e.value)

    with store.connect(live) as conn:
        assert conn.execute("SELECT label_score FROM jobs").fetchone()["label_score"] is None


def test_apply_without_an_export_says_how_to_make_one(tmp_path):
    dest = make_db(tmp_path / "copy.db", [("1", 7, "r", JD)])
    with pytest.raises(SystemExit) as e:
        cl.do_apply(args(apply=dest, out=tmp_path / "missing.json"))
    assert "calibration_labels.py --export" in str(e.value)


def test_apply_reports_labels_with_no_matching_row(tmp_path, capsys):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD), ("2", 2, "r", JD)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))
    dest = make_db(tmp_path / "copy.db", [("1", 7, "r", JD)])

    cl.do_apply(args(apply=dest, out=out))
    said = capsys.readouterr().out
    assert "applied 1 label(s)" in said
    assert "1 label(s) in the export have no row" in said


# --- the file format

def test_read_labels_rejects_a_file_that_is_not_an_export(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"key": "k", "score": 7}]))
    with pytest.raises(ValueError, match="not a label export"):
        cl.read_labels(bad)


def test_read_labels_rejects_a_row_with_no_score(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"rows": [{"key": "k"}]}))
    with pytest.raises(ValueError, match="no key or no score"):
        cl.read_labels(bad)


def test_the_digest_ignores_the_export_timestamp(tmp_path):
    """Two exports of the same corpus are the same labels, and must hash the same."""
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    cl.do_export(args(db=src, out=a))
    cl.do_export(args(db=src, out=b))
    first, second = cl.read_labels(a), cl.read_labels(b)
    assert first["labels_digest"] == second["labels_digest"]


def test_label_map_is_key_to_score(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD), ("2", 2, "r", None)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))
    assert cl.label_map(out) == {key_for("1"): 7, key_for("2"): 2}


def test_main_will_not_export_and_apply_at_once(tmp_path):
    with pytest.raises(SystemExit) as e:
        cl.main(["--export", "--apply", str(tmp_path / "x.db")])
    assert "one at a time" in str(e.value)


def test_the_default_source_is_a_jobhunt_db(tmp_path):
    assert cl.default_source_db().name == "jobhunt.db"


# --- calibrate.py's two label sources

def test_column_labels_reads_the_column(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD), ("2", None, None, JD)])
    conn = calibrate.open_ro(src)
    assert calibrate.column_labels(conn, "score") == {key_for("1"): 7}


def test_column_labels_rejects_a_column_that_is_not_there(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    conn = calibrate.open_ro(src)
    with pytest.raises(SystemExit) as e:
        calibrate.column_labels(conn, "clade_score")
    assert "not a column on jobs" in str(e.value)


def test_agreeing_sources_say_so(capsys):
    calibrate.compare_labels({"a": 7}, {"a": 7}, "score", ROOT / "x.json")
    assert "labels agree" in capsys.readouterr().out


def test_disagreeing_sources_print_the_delta(capsys):
    calibrate.compare_labels({"a": 7, "b": 3}, {"a": 4, "c": 9}, "score", ROOT / "x.json")
    said = capsys.readouterr().out
    assert "LABEL DISAGREEMENT" in said
    assert "in the file only : 1" in said
    assert "in the column only: 1" in said
    assert "same key, different label: 1" in said
    assert "file 7  jobs.score 4" in said


def test_calibrate_measures_against_the_file_when_asked(tmp_path, capsys):
    src = make_db(tmp_path / "src.db",
                  [(str(i), 7 if i % 2 else 2, "r", JD) for i in range(1, 9)])
    out = tmp_path / "labels.json"
    cl.do_export(args(db=src, out=out))

    rc = calibrate.main(["--db", str(src), "--labels", str(out),
                              "--profile", str(EXAMPLE_PROFILE)])
    said = capsys.readouterr().out
    assert rc == 0
    assert f"labels: {out}" in said
    assert "rows: 8" in said


def test_calibrate_defaults_to_the_score_column(tmp_path, capsys, monkeypatch):
    # LABELS points at a gitignored real export on the owner's machine and at nothing on a
    # fresh clone. Point it somewhere empty so this test measures the same thing either way.
    monkeypatch.setattr(calibrate, "LABELS", tmp_path / "absent.json")
    src = make_db(tmp_path / "src.db",
                  [(str(i), 7 if i % 2 else 2, "r", JD) for i in range(1, 9)])
    rc = calibrate.main(["--db", str(src), "--profile", str(EXAMPLE_PROFILE)])
    said = capsys.readouterr().out
    assert rc == 0
    assert "labels: jobs.score" in said


def test_calibrate_says_how_to_make_a_label_file_that_is_missing(tmp_path):
    src = make_db(tmp_path / "src.db", [("1", 7, "r", JD)])
    with pytest.raises(SystemExit) as e:
        calibrate.main(["--db", str(src), "--labels", str(tmp_path / "nope.json"),
                             "--profile", str(EXAMPLE_PROFILE)])
    assert "calibration_labels.py --export" in str(e.value)


def test_calibrate_still_refuses_the_live_database(tmp_path, monkeypatch):
    monkeypatch.setattr(calibrate, "LABELS", tmp_path / "absent.json")
    live = tmp_path / "jobhunt.db"
    make_db(live, [("1", 7, "r", JD)])
    monkeypatch.setattr(calibrate, "LIVE_DB", live)
    with pytest.raises(SystemExit) as e:
        calibrate.main(["--db", str(live), "--profile", str(EXAMPLE_PROFILE)])
    assert "copy it first" in str(e.value)
