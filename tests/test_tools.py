import datetime as dt
import importlib
import json
import sqlite3

import pytest

from coach import config


def words(srv):
    c = sqlite3.connect(srv.DB)
    c.row_factory = sqlite3.Row
    return {r["sg"]: dict(r) for r in c.execute("SELECT * FROM words")}


def test_example_config_loads():
    cfg = config.load(config.ROOT / "config.example.toml")
    assert cfg.learner_name == "Alex"
    assert cfg.deadline == dt.date(2027, 6, 1)
    assert cfg.phases and cfg.milestones
    assert cfg.phases == tuple(sorted(cfg.phases, key=lambda p: p.start))


def test_config_requires_learner_fields(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[learner]\nname = "Kim"\n')
    with pytest.raises(ValueError, match="goal"):
        config.load(p)


def test_config_mission_file_relative(tmp_path):
    (tmp_path / "m.md").write_text("Order coffee in dialect.")
    p = tmp_path / "c.toml"
    p.write_text('[learner]\nname = "Kim"\ngoal = "chat"\ndeadline = 2027-01-01\nmission_file = "m.md"\n')
    assert config.load(p).mission_text == "Order coffee in dialect."


def test_rules_use_config(srv):
    assert "Alex" in srv.COACH_RULES and "Zürich German" in srv.COACH_RULES
    assert "Sunday" in srv.COACH_RULES and "'fertig'" in srv.COACH_RULES


def test_start_session_empty(srv):
    out = srv.start_session()
    assert out["today"] == "2026-11-10"
    assert out["days_to_deadline"] == (dt.date(2027, 6, 1) - dt.date(2026, 11, 10)).days
    assert out["phase"]["daily_minutes"] == "10-15"
    assert out["next_milestone"]["label"] == "4-week review"
    assert out["is_benchmark_day"] is False
    assert out["recent_sessions"] == [] and out["todays_plan"]["focus_words"] == []
    json.dumps(out)  # must be JSON-serialisable


def test_benchmark_day_flag(srv, clock):
    clock["d"] = dt.date(2026, 11, 15)  # Sunday
    assert srv.start_session()["is_benchmark_day"] is True


def test_log_words_dedupes_and_fills(srv):
    r = srv.log_words([srv.NewWord(sg="Grüezi"), srv.NewWord(sg="hüt", en="today")])
    assert r["added"] == ["Grüezi", "hüt"] and r["total_words"] == 2
    r = srv.log_words([srv.NewWord(sg="  grüezi ", de="Guten Tag", en="hello")])
    assert r["added"] == [] and r["already_tracked"] == ["  grüezi "] and r["total_words"] == 2
    w = words(srv)["Grüezi"]
    assert (w["de"], w["en"], w["due_on"]) == ("Guten Tag", "hello", "2026-11-11")


def test_review_word_secure_needs_three_days(srv, clock):
    srv.log_words([srv.NewWord(sg="morn")])
    r = srv.review_word("morn", "free")
    assert r["status"] == srv.STATUS_LABEL["used"]
    r = srv.review_word("morn", "free")  # same day: no extra credit
    assert r["free_use_days"] == 1
    for day in (11, 13):
        clock["d"] = dt.date(2026, 11, day)
        r = srv.review_word("morn", "free")
    assert r["free_use_days"] == 3 and r["status"] == "secure"
    r = srv.review_word("morn", "missed")
    assert r["status"] == srv.STATUS_LABEL["used"]
    w = words(srv)["morn"]
    assert w["lapses"] == 1 and w["box"] == 1 and w["reps"] == 5


def test_review_word_with_help_and_unknown(srv):
    r = srv.review_word("öppis", "with_help", "after a hint")
    assert r["status"] == srv.STATUS_LABEL["with_help"] and r["next_review"] == "2026-11-11"
    assert words(srv)["öppis"]["reps"] == 1


def test_log_mistake_counts_category(srv):
    srv.log_mistake("ich kann dich helfen", "ich cha dir hälfe", "dative")
    assert srv.log_mistake("mit die Leute", "mit de Lüüt", "dative")["times_this_category"] == 2
    assert srv.start_session()["recent_corrections"][0]["said"] == "mit die Leute"


def test_update_focus_add_and_move_on(srv):
    srv.log_words([srv.NewWord(sg=w) for w in ("a1", "a2", "a3", "a4", "a5", "a6")])
    out = srv.update_focus(add=["chasch"])
    assert out["focus_now"][0] == "chasch" and len(out["focus_now"]) == 4
    out = srv.update_focus(move_on=["chasch"])
    assert "chasch" not in out["focus_now"] and len(out["focus_now"]) == 4
    assert words(srv)["chasch"]["focus_sessions"] >= srv.CFG.focus_max_sessions
    # moved-on word does not come back on later rotations
    for _ in range(3):
        assert "chasch" not in srv.update_focus()["focus_now"]


def test_focus_rotates_after_max_sessions(srv):
    srv.log_words([srv.NewWord(sg=f"w{i}") for i in range(8)])
    first = srv.update_focus()["focus_now"]
    assert first == ["w0", "w1", "w2", "w3"]
    for _ in range(srv.CFG.focus_max_sessions):
        out = srv.finish_session("t", "r", "d", ["p"])
    assert out["focus_for_next_session"]["focus_now"] == ["w4", "w5", "w6", "w7"]
    assert set(out["focus_for_next_session"]["moved_out_of_focus"]) == set(first)


def test_focus_graduates_freely_used_word(srv):
    srv.log_words([srv.NewWord(sg=f"w{i}") for i in range(5)])
    srv.update_focus()
    srv.review_word("w0", "free")
    out = srv.finish_session("t", "r", "d", ["p"])["focus_for_next_session"]
    assert "w0" in out["moved_out_of_focus"] and "w4" in out["focus_now"]


def test_finish_session_saves_batch_and_debrief(srv):
    out = srv.finish_session(
        topics="Train station", report="Ordered a ticket.", debrief="Next: past tense.",
        next_priorities=["a", "b", "c", "d"],
        corrections=[srv.Correction(said="der Zug", correct="de Zug", category="article")],
        word_results=[srv.WordResult(sg="es Billett", result="free")],
        new_words=[srv.NewWord(sg="Gleis", en="platform")])
    assert out["saved"] and out["also_saved"] == {"corrections": 1, "word_results": 1, "new_words": 1}
    assert out["sessions_this_week"] == 1
    last = srv.start_session()["recent_sessions"][0]
    assert last["debrief"] == "Next: past tense." and last["next_priorities"] == ["a", "b", "c"]
    assert {"es Billett", "Gleis"} <= set(words(srv))


def test_minutes_not_invented(srv):
    srv.finish_session("t", "r", "d", [])
    c = sqlite3.connect(srv.DB)
    assert c.execute("SELECT minutes_observed FROM sessions").fetchone()[0] is None


def test_log_benchmark_counts_and_replaces(srv, clock):
    for day in (9, 10, 12):
        clock["d"] = dt.date(2026, 11, day)
        srv.finish_session("t", "r", "d", [])
    out = srv.log_benchmark("2026-11-15", first_try_10=6)
    assert out["sessions_7"] == 3
    srv.log_benchmark("2026-11-15", first_try_10=7, rescues_10min=2)
    sc = srv.get_progress()["scorecard"]
    assert len(sc) == 1 and sc[0]["first_try_10"] == 7 and sc[0]["fluency_5"] is None


def test_progress_streak_and_counts(srv, clock):
    for day in (7, 8, 9):
        clock["d"] = dt.date(2026, 11, day)
        srv.finish_session("t", "r", "d", [])
    clock["d"] = dt.date(2026, 11, 10)  # not practised yet today: streak still alive
    p = srv.get_progress()
    assert p["practice_streak_days"] == 3
    assert sum(w["sessions"] for w in p["weeks"]) == 3
    clock["d"] = dt.date(2026, 11, 12)
    assert srv.get_progress()["practice_streak_days"] == 0


def test_get_due_words(srv):
    srv.log_words([srv.NewWord(sg="hüt")])
    assert srv.get_due_words()["due"] == []  # new words are due tomorrow
    srv.review_word("hüt", "missed")
    assert srv.get_due_words()["due"] == []


def test_seed_demo_is_synthetic_and_idempotent(srv, capsys):
    import scripts.seed_demo as seed
    importlib.reload(seed)
    seed.main()
    seed.main()
    assert "already has words" in capsys.readouterr().out
    out = srv.start_session()
    assert len(out["todays_plan"]["focus_words"]) == 4
    assert len(out["recent_sessions"]) == 2
