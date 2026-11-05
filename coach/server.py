"""Swiss German speaking coach: an MCP tool server for ChatGPT Apps.

ChatGPT (text or voice) does the talking; this server is the coach's memory between chats:
words with spaced repetition, corrections, session debriefs, weekly benchmarks and a focus set
that rotates automatically. One SQLite file, no external services.

Environment:
  COACH_TOKEN       required: secret for the connector URL /mcp/<token> (>= 32 URL-safe chars)
  COACH_DASH_TOKEN  required: separate secret for the read-only page /progress/<dash token>
  COACH_HOSTS       comma-separated Host headers to accept (add your public hostname)
  COACH_RATE_LIMIT  max requests per minute per client IP (default 120)
  COACH_DB          SQLite path (default ./data/coach.db)
  COACH_CONFIG      learner config TOML (default ./config.toml, else config.example.toml)
  COACH_PORT        local port for uvicorn (default 8000; binds 127.0.0.1 only)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from contextlib import closing
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import config as _config

CFG = _config.load()
DB = os.path.expanduser(os.environ.get("COACH_DB", str(_config.ROOT / "data" / "coach.db")))
INTERVALS = [0, 1, 2, 4, 7, 14, 30, 60]  # Leitner box -> days until next review
STATUSES = ("new", "with_help", "used", "secure")
STATUS_LABEL = {"new": "new / not used yet", "with_help": "used with help",
                "used": "used freely, not stable", "secure": "secure"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_BATCH = 30          # max items per list argument (new words, corrections)
MAX_WORD_RESULTS = 40   # max word results in one finish_session
# String length limits for tool arguments (characters). Oversized input is rejected by schema validation
# before any work is done.
WORD_LEN, LABEL_LEN, TEXT_LEN, REPORT_LEN, DEBRIEF_LEN = 200, 100, 1000, 5000, 20000
MAX_BODY_BYTES = 512 * 1024  # HTTP request body limit (worst-case valid finish_session is well under this)
DASH_PAGE_SIZE = 100         # words per page on the progress page

Word = Annotated[str, Field(min_length=1, max_length=WORD_LEN)]
Label = Annotated[str, Field(max_length=LABEL_LEN)]
Text = Annotated[str, Field(max_length=TEXT_LEN)]


def coach_rules(cfg: _config.Config = CFG) -> str:
    n, bday = cfg.learner_name, WEEKDAYS[cfg.benchmark_weekday]
    finish = "/".join(f"'{w}'" for w in cfg.finish_words) or "'done'"
    extra = f"; {cfg.extra_dialects}" if cfg.extra_dialects else ""
    return (
        f"SOURCE OF TRUTH: this app is {n}'s only {cfg.dialect} record and your memory between chats. start_session "
        "returns the mission, the debriefs of the last sessions and TODAY'S PLAN: continue exactly where the last "
        "debrief left off. Do NOT create, update or offer tracker/Markdown files or notes elsewhere. "
        f"You are {n}'s {cfg.dialect} ({cfg.dialect_local}) speaking coach{extra}. Goal: {cfg.goal}, "
        f"by {cfg.deadline.isoformat()} ({cfg.deadline_label}). {cfg.background} "
        f"Ask ONE question at a time in {cfg.dialect_local} and let {n} finish. No scripts, no answers in advance. "
        "PROGRESSION: work on today's focus_words, INTRODUCE the new_to_introduce words (model once, then get "
        f"{n} to use them), and weave review words in lightly. Do not re-run topics or sentences from recent "
        "debriefs; pick fresh everyday topics. Words that were moved on from stay moved on. "
        f"If {n} says to move on from words (or wants a word as focus), call update_focus right away: that decision "
        "is saved for future sessions. "
        f"BENCHMARK DAY ({bday}, is_benchmark_day): after a short warm-up run 10 unfamiliar everyday questions, count "
        "first-try meaningful answers and switches to another language, then save with log_benchmark (leave anything "
        "unmeasured empty). Then free talk. "
        "Every 5-8 minutes: correct at most TWO consequential errors and give ONE useful expression, then ask for a "
        "NEW sentence using each. Record silently with tools as you go: log_mistake for each correction; review_word "
        "when a tracked word is used or missed (free = produced unprompted in a new sentence; with_help = after a "
        "hint/model; missed = could not); log_words for genuinely new useful words. "
        "Repeating after you is NOT free use. Never invent minutes, scores, pronunciation judgements or streaks; "
        "say 'not measured'. Swiss German spelling varies; flag uncertain dialect choices. "
        f"When {n} is done (or says {finish}), call finish_session with an honest short report, a FULL debrief for "
        "the next chat (what was talked about, details to follow up on, what improved, what is still shaky, what is "
        "done and should not be repeated, what to do next), max 3 next priorities, AND every correction, word "
        "result and new word from the session not already saved (in voice mode, save it all there in one call). "
        "Then say in one or two sentences what is saved and what's next."
    )


COACH_RULES = coach_rules()


def today() -> dt.date:
    return dt.datetime.now(CFG.timezone).date()


def now_iso() -> str:
    return dt.datetime.now(CFG.timezone).isoformat(timespec="seconds")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").strip().lower()
    return " ".join(s.replace("\u2019", "'").split())


SCHEMA = """
CREATE TABLE IF NOT EXISTS words(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, sg TEXT NOT NULL, de TEXT, en TEXT,
  status TEXT NOT NULL DEFAULT 'new', context TEXT, example TEXT, priority INTEGER NOT NULL DEFAULT 0,
  box INTEGER NOT NULL DEFAULT 0, due_on TEXT, free_days TEXT NOT NULL DEFAULT '[]',
  reps INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0,
  added_on TEXT NOT NULL, last_seen TEXT, note TEXT, focus_sessions INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY, word_id INTEGER NOT NULL, result TEXT NOT NULL, evidence TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mistakes(
  id INTEGER PRIMARY KEY, said TEXT NOT NULL, correct TEXT NOT NULL, category TEXT, note TEXT,
  on_date TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, on_date TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'practice', topics TEXT,
  minutes_observed REAL, report TEXT, debrief TEXT, next_priorities TEXT NOT NULL DEFAULT '[]', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS benchmarks(
  week_ending TEXT PRIMARY KEY, sessions_7 INTEGER, first_try_10 INTEGER, rescues_10min INTEGER,
  endurance TEXT, followups INTEGER, fluency_5 REAL, issue TEXT, evidence TEXT, at TEXT NOT NULL);
"""


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def rotate_focus(c: sqlite3.Connection) -> dict:
    """Graduate focus words used freely or that had their turn, cap the set, refill with shaky + new words."""
    size, max_s = CFG.focus_size, CFG.focus_max_sessions
    graduated, added = [], []
    for r in c.execute("SELECT id, sg, status, focus_sessions FROM words WHERE priority>0").fetchall():
        if r["status"] in ("used", "secure") or r["focus_sessions"] >= max_s:
            c.execute("UPDATE words SET priority=0 WHERE id=?", (r["id"],))
            graduated.append(r["sg"])
    cur = c.execute("SELECT id, sg FROM words WHERE priority>0 "
                    "ORDER BY priority DESC, focus_sessions DESC, id DESC").fetchall()
    for r in cur[size:]:
        c.execute("UPDATE words SET priority=0 WHERE id=?", (r["id"],))  # back to the pool, not graduated
    need = size - min(len(cur), size)
    if need:
        new = c.execute("SELECT id, sg FROM words WHERE priority=0 AND status='new' AND focus_sessions<? "
                        "ORDER BY id LIMIT ?", (max_s, need)).fetchall()
        shaky = c.execute("SELECT id, sg FROM words WHERE priority=0 AND status='with_help' AND focus_sessions<? "
                          "ORDER BY lapses DESC, id LIMIT ?", (max_s, need)).fetchall()
        picks, seen = [], set()
        for i in range(max(len(new), len(shaky))):  # alternate shaky / new
            for lst in (shaky, new):
                if i < len(lst) and lst[i]["id"] not in seen:
                    picks.append(lst[i])
                    seen.add(lst[i]["id"])
        for r in picks[:need]:
            c.execute("UPDATE words SET priority=1 WHERE id=?", (r["id"],))
            added.append(r["sg"])
    focus = [r["sg"] for r in c.execute("SELECT sg FROM words WHERE priority>0 ORDER BY priority DESC, id")]
    return {"moved_out_of_focus": graduated, "new_focus": added, "focus_now": focus}


def word_out(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in ("sg", "de", "en", "status", "context", "example") if r[k]} | {"due_on": r["due_on"]}


def phase(d: dt.date) -> dict:
    for p in CFG.phases:
        if p.start <= d <= p.end:
            return {"period": f"{p.start} to {p.end}", "daily_minutes": p.daily_minutes, "focus": p.focus}
    if CFG.phases and d < CFG.phases[0].start:
        return {"period": "before plan start", "daily_minutes": CFG.phases[0].daily_minutes, "focus": "Get started"}
    return {"period": "outside planned phases", "daily_minutes": "ongoing", "focus": "Use it with real people"}


def next_milestone(d: dt.date) -> dict | None:
    for m in CFG.milestones:
        if m.date >= d:
            return {"date": m.date.isoformat(), "label": m.label, "days_away": (m.date - d).days}
    if CFG.deadline >= d:
        return {"date": CFG.deadline.isoformat(), "label": CFG.deadline_label, "days_away": (CFG.deadline - d).days}
    return None


def week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


mcp = MCPServer(
    name="swiss-german-coach",
    title=f"{CFG.dialect_local} Coach",
    instructions=f"Call start_session first whenever {CFG.learner_name} wants to practise {CFG.dialect}. " + COACH_RULES,
)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WR = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


@mcp.tool(title="Start session", annotations=RO)
def start_session() -> dict:
    """Call FIRST when the learner wants to practise (voice or text). This is your memory: returns coaching rules,
    the mission, debriefs of the last sessions, and today's plan (focus words, new words to introduce, light
    review). Continue where the last debrief ended, then open with one natural Swiss German question."""
    d = today()
    with closing(db()) as c:
        last = c.execute("SELECT * FROM sessions ORDER BY on_date DESC, id DESC LIMIT 3").fetchall()
        focus = c.execute("SELECT * FROM words WHERE priority>0 ORDER BY priority DESC, id LIMIT ?",
                          (CFG.focus_size,)).fetchall()
        new = c.execute("SELECT * FROM words WHERE priority=0 AND status='new' AND focus_sessions<? "
                        "ORDER BY id LIMIT 2", (CFG.focus_max_sessions,)).fetchall()
        skip = [r["id"] for r in list(focus) + list(new)] or [0]
        due = c.execute(
            f"SELECT * FROM words WHERE (due_on IS NULL OR due_on<=?) AND status NOT IN ('secure','new') "
            f"AND id NOT IN ({','.join('?' * len(skip))}) ORDER BY due_on, COALESCE(last_seen,''), id LIMIT 5",
            (d.isoformat(), *skip)).fetchall()
        mistakes = c.execute("SELECT said, correct, category FROM mistakes ORDER BY id DESC LIMIT 6").fetchall()
        wk = c.execute("SELECT COUNT(DISTINCT on_date) FROM sessions WHERE on_date>=?",
                       (week_start(d).isoformat(),)).fetchone()[0]
    return {
        "coach_rules": COACH_RULES,
        "learner": CFG.learner_name,
        "dialect": CFG.dialect,
        "today": d.isoformat(),
        "deadline": CFG.deadline.isoformat(),
        "days_to_deadline": (CFG.deadline - d).days,
        "phase": phase(d),
        "next_milestone": next_milestone(d),
        "is_benchmark_day": d.weekday() == CFG.benchmark_weekday,
        "sessions_this_week": wk,
        "recent_sessions": [{
            "date": r["on_date"], "topics": r["topics"], "report": r["report"], "debrief": r["debrief"],
            "next_priorities": json.loads(r["next_priorities"])} for r in last],
        "todays_plan": {
            "focus_words": [word_out(r) for r in focus],
            "new_to_introduce": [word_out(r) for r in new],
            "review_lightly": [word_out(r) for r in due],
            "note": (f"Focus rotates automatically: a word leaves focus once used freely or after "
                     f"{CFG.focus_max_sessions} sessions, then stays in spaced review."),
        },
        "recent_corrections": [dict(r) for r in mistakes],
        "mission": CFG.mission_text,
    }


@mcp.tool(title="Move on / change focus words", annotations=WR)
def update_focus(move_on: Annotated[list[Word] | None, Field(max_length=20)] = None,
                 add: Annotated[list[Word] | None, Field(max_length=10)] = None,
                 reason: Text | None = None) -> dict:
    """Call when the learner says to move on from words (move_on) or wants specific words practised (add).
    Saved for all future sessions: moved-on words leave focus for good (still lightly reviewed); focus refills
    automatically."""
    d = today().isoformat()
    with closing(db()) as c, c:
        for w in move_on or []:
            c.execute("UPDATE words SET priority=0, focus_sessions=MAX(focus_sessions,?) WHERE key=?",
                      (CFG.focus_max_sessions, norm(w)))
        for w in (add or [])[:CFG.focus_size]:
            if not norm(w):
                continue
            if not c.execute("SELECT 1 FROM words WHERE key=?", (norm(w),)).fetchone():
                c.execute("INSERT INTO words(key,sg,status,added_on,due_on) VALUES(?,?,?,?,?)",
                          (norm(w), w.strip(), "new", d, d))
            c.execute("UPDATE words SET priority=2, focus_sessions=0 WHERE key=?", (norm(w),))
        out = rotate_focus(c)
    return out | {"saved": True}


class NewWord(BaseModel):
    sg: str = Field(description="Swiss German form (one plausible spelling for the configured dialect)",
                    min_length=1, max_length=WORD_LEN)
    de: str | None = Field(None, description="Standard German", max_length=WORD_LEN)
    en: str | None = Field(None, description="English", max_length=WORD_LEN)
    context: str | None = Field(None, description="Where/how it came up", max_length=TEXT_LEN)
    example: str | None = Field(None, description="Short Swiss German example sentence", max_length=TEXT_LEN)
    status: Literal["new", "with_help", "used"] = Field(
        "new", description="new = introduced only; with_help = used after a hint; used = produced unprompted")
    focus: bool = Field(False, description="True to make it a focus word for upcoming sessions")


@mcp.tool(title="Log new words", annotations=WR)
def log_words(words: Annotated[list[NewWord], Field(max_length=MAX_BATCH)]) -> dict:
    """Save genuinely new, useful Swiss German words or phrases from this conversation (batch them).
    Existing words are not duplicated; their missing fields are filled in."""
    d, added, updated = today(), [], []
    with closing(db()) as c, c:
        if len(words) > MAX_BATCH:  # also enforced by the schema; guard direct calls
            raise ValueError(f"at most {MAX_BATCH} words per call")
        for w in words:
            k = norm(w.sg)
            if not k:
                continue
            ex = c.execute("SELECT * FROM words WHERE key=?", (k,)).fetchone()
            if ex:
                c.execute(
                    "UPDATE words SET de=COALESCE(de,?), en=COALESCE(en,?), context=COALESCE(context,?), "
                    "example=COALESCE(example,?), priority=MAX(priority,?), last_seen=? WHERE id=?",
                    (w.de, w.en, w.context, w.example, 1 if w.focus else 0, d.isoformat(), ex["id"]))
                updated.append(w.sg)
            else:
                c.execute(
                    "INSERT INTO words(key,sg,de,en,status,context,example,priority,due_on,added_on,last_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (k, w.sg.strip(), w.de, w.en, w.status, w.context, w.example, 1 if w.focus else 0,
                     (d + dt.timedelta(days=1)).isoformat(), d.isoformat(), d.isoformat()))
                added.append(w.sg)
        total = c.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    return {"added": added, "already_tracked": updated, "total_words": total}


@mcp.tool(title="Record word use", annotations=WR)
def review_word(sg: Word, result: Literal["free", "with_help", "missed"], evidence: Text | None = None) -> dict:
    """Record how the learner handled a tracked word just now. free = produced it in a NEW sentence without a
    hint (repeating after you does NOT count); with_help = only after a hint/model; missed = could not produce or
    recall it. Updates spaced repetition and status. Unknown words are added."""
    d = today()
    k = norm(sg)
    if not k:
        return {"saved": False, "error": "empty word"}
    with closing(db()) as c, c:
        r = c.execute("SELECT * FROM words WHERE key=?", (k,)).fetchone()
        if not r:
            c.execute("INSERT INTO words(key,sg,status,added_on) VALUES(?,?,?,?)", (k, sg.strip(), "new", d.isoformat()))
            r = c.execute("SELECT * FROM words WHERE key=?", (k,)).fetchone()
        box, status, free_days, lapses = r["box"], r["status"], json.loads(r["free_days"]), r["lapses"]
        if result == "free":
            if d.isoformat() not in free_days:
                free_days.append(d.isoformat())
                box = min(box + 1, len(INTERVALS) - 1)
            status = "secure" if len(free_days) >= 3 and box >= 3 else "used"
        elif result == "with_help":
            box = max(1, min(box, 2))
            status = "used" if status in ("used", "secure") else "with_help"
        else:
            box, lapses = 1, lapses + 1
            if status == "secure":
                status = "used"
        due = d + dt.timedelta(days=max(1, INTERVALS[box]))
        c.execute(
            "UPDATE words SET box=?, status=?, free_days=?, lapses=?, reps=reps+1, due_on=?, last_seen=?, "
            "priority=CASE WHEN ?='secure' THEN 0 ELSE priority END WHERE id=?",
            (box, status, json.dumps(free_days), lapses, due.isoformat(), d.isoformat(), status, r["id"]))
        c.execute("INSERT INTO reviews(word_id,result,evidence,at) VALUES(?,?,?,?)", (r["id"], result, evidence, now_iso()))
    return {"sg": r["sg"], "status": STATUS_LABEL[status], "next_review": due.isoformat(),
            "free_use_days": len(free_days), "secure_rule": "secure after free use on 3 different days"}


@mcp.tool(title="Log a correction", annotations=WR)
def log_mistake(said: Text, correct: Text, category: Label | None = None, note: Text | None = None) -> dict:
    """Save a consequential error you corrected: what the learner said, the Swiss German correction, and a category
    (e.g. preposition, dative, past tense, word order, Standard German form, language switch, vocabulary)."""
    with closing(db()) as c, c:
        c.execute("INSERT INTO mistakes(said,correct,category,note,on_date,at) VALUES(?,?,?,?,?,?)",
                  (said, correct, category, note, today().isoformat(), now_iso()))
        n = c.execute("SELECT COUNT(*) FROM mistakes WHERE category IS ?", (category,)).fetchone()[0]
    return {"saved": True, "times_this_category": n}


@mcp.tool(title="Words due for review", annotations=RO)
def get_due_words(limit: Annotated[int, Field(ge=1, le=MAX_BATCH)] = 10) -> dict:
    """List tracked words due for review today (least stable first) to weave into the conversation."""
    with closing(db()) as c:
        rows = c.execute(
            "SELECT * FROM words WHERE (due_on IS NULL OR due_on<=?) AND status!='secure' "
            "ORDER BY priority DESC, CASE status WHEN 'with_help' THEN 0 WHEN 'used' THEN 1 ELSE 2 END, due_on LIMIT ?",
            (today().isoformat(), max(1, min(limit, MAX_BATCH)))).fetchall()
    return {"due": [word_out(r) for r in rows]}


class Correction(BaseModel):
    said: str = Field(description="What the learner said", max_length=TEXT_LEN)
    correct: str = Field(description="Swiss German correction", max_length=TEXT_LEN)
    category: str | None = Field(None, description="preposition, dative, past tense, word order, Standard German form, ...",
                                 max_length=LABEL_LEN)


class WordResult(BaseModel):
    sg: str = Field(description="Tracked word (Swiss German form)", min_length=1, max_length=WORD_LEN)
    result: Literal["free", "with_help", "missed"] = Field(
        description="free = unprompted in a NEW sentence; with_help = after a hint/model; missed = could not. "
                    "Repeating after you is NOT free.")
    evidence: str | None = Field(None, description="Short quote/context", max_length=TEXT_LEN)


@mcp.tool(title="Finish session", annotations=WR)
def finish_session(topics: Text,
                   report: Annotated[str, Field(max_length=REPORT_LEN)],
                   debrief: Annotated[str, Field(max_length=DEBRIEF_LEN)],
                   next_priorities: Annotated[list[Text], Field(max_length=10)],
                   minutes_observed: Annotated[float | None, Field(ge=0, le=1440)] = None,
                   kind: Literal["practice", "benchmark"] = "practice",
                   corrections: Annotated[list[Correction] | None, Field(max_length=MAX_BATCH)] = None,
                   word_results: Annotated[list[WordResult] | None, Field(max_length=MAX_WORD_RESULTS)] = None,
                   new_words: Annotated[list[NewWord] | None, Field(max_length=MAX_BATCH)] = None) -> dict:
    """Close a session with an honest short report (observed only), topics covered, at most 3 next priorities,
    and a FULL debrief that the next chat reads as its memory: what was talked about (details worth following
    up), what improved, what is still shaky, what is done and must not be repeated, and the plan for next time.
    minutes_observed: only if actually known, otherwise omit (not measured).
    ALSO pass everything not yet saved during the session, in one go: every correction you gave (corrections),
    every tracked word used or missed (word_results), and useful new words (new_words). In voice mode this is the
    main place data gets saved, so be complete."""
    if (len(corrections or []) > MAX_BATCH or len(new_words or []) > MAX_BATCH
            or len(word_results or []) > MAX_WORD_RESULTS):  # schema enforces this too; reject before any write
        raise ValueError("batch too large")
    saved = {"corrections": 0, "word_results": 0, "new_words": 0}
    for m in corrections or []:
        log_mistake(m.said, m.correct, m.category)
        saved["corrections"] += 1
    if new_words:
        log_words(new_words)
        saved["new_words"] = len(new_words)
    for w in word_results or []:
        review_word(w.sg, w.result, w.evidence)
        saved["word_results"] += 1
    d = today()
    with closing(db()) as c, c:
        c.execute("INSERT INTO sessions(on_date,kind,topics,minutes_observed,report,debrief,next_priorities,at) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (d.isoformat(), kind, topics, minutes_observed, report, debrief,
                   json.dumps(next_priorities[:3], ensure_ascii=False), now_iso()))
        c.execute("UPDATE words SET focus_sessions=focus_sessions+1 WHERE priority>0")
        focus = rotate_focus(c)
        wk = c.execute("SELECT COUNT(DISTINCT on_date) FROM sessions WHERE on_date>=?",
                       (week_start(d).isoformat(),)).fetchone()[0]
    return {"saved": True, "also_saved": saved, "focus_for_next_session": focus, "sessions_this_week": wk,
            "days_to_deadline": (CFG.deadline - d).days}


@mcp.tool(title="Save weekly benchmark", annotations=WR)
def log_benchmark(week_ending: Annotated[str, Field(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")],
                  first_try_10: Annotated[int | None, Field(ge=0, le=10)] = None,
                  rescues_10min: Annotated[int | None, Field(ge=0, le=1000)] = None,
                  endurance: Text | None = None,
                  followups: Annotated[int | None, Field(ge=0, le=1000)] = None,
                  fluency_5: Annotated[float | None, Field(ge=0, le=5)] = None,
                  issue: Text | None = None, evidence: Text | None = None) -> dict:
    """Save the weekly scorecard (unrehearsed prompts). Leave any metric empty if not measured.
    week_ending is the benchmark date YYYY-MM-DD. first_try_10 = meaningful first-try answers out of 10;
    rescues_10min = switches to another language per 10 minutes. Sessions in the 7 days up to week_ending are
    counted automatically. Saving the same week again replaces it."""
    we = dt.date.fromisoformat(week_ending)
    with closing(db()) as c, c:
        n = c.execute("SELECT COUNT(DISTINCT on_date) FROM sessions WHERE on_date BETWEEN ? AND ?",
                      ((we - dt.timedelta(days=6)).isoformat(), we.isoformat())).fetchone()[0]
        c.execute("INSERT OR REPLACE INTO benchmarks VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (we.isoformat(), n, first_try_10, rescues_10min, endurance, followups, fluency_5, issue, evidence,
                   now_iso()))
    return {"saved": True, "week_ending": we.isoformat(), "sessions_7": n}


@mcp.tool(title="Show progress", annotations=RO)
def get_progress() -> dict:
    """Progress: word counts by status, words added / free uses / corrections / sessions per week, weekly
    scorecard, milestones and days to the deadline."""
    d = today()
    with closing(db()) as c:
        by_status = {STATUS_LABEL[r["status"]]: r["n"]
                     for r in c.execute("SELECT status, COUNT(*) n FROM words GROUP BY status")}
        weeks = []
        for i in range(5, -1, -1):
            ws = week_start(d) - dt.timedelta(weeks=i)
            a, b = ws.isoformat(), (ws + dt.timedelta(days=6)).isoformat()
            q = lambda sql: c.execute(sql, (a, b)).fetchone()[0]  # noqa: E731
            weeks.append({
                "week": a,
                "sessions": q("SELECT COUNT(DISTINCT on_date) FROM sessions WHERE on_date BETWEEN ? AND ?"),
                "words_added": q("SELECT COUNT(*) FROM words WHERE added_on BETWEEN ? AND ?"),
                "free_uses": q("SELECT COUNT(*) FROM reviews WHERE result='free' AND substr(at,1,10) BETWEEN ? AND ?"),
                "corrections": q("SELECT COUNT(*) FROM mistakes WHERE on_date BETWEEN ? AND ?"),
            })
        cats = [dict(r) for r in c.execute(
            "SELECT category, COUNT(*) n FROM mistakes GROUP BY category ORDER BY n DESC LIMIT 5")]
        bench = [dict(r) for r in c.execute(
            "SELECT week_ending,sessions_7,first_try_10,rescues_10min,endurance,followups,fluency_5,issue "
            "FROM benchmarks ORDER BY week_ending DESC LIMIT 52")][::-1]  # last year of scorecards
        days = [r[0] for r in c.execute("SELECT DISTINCT on_date FROM sessions ORDER BY on_date DESC LIMIT 1000")]
    streak, cur = 0, d
    if days and days[0] != d.isoformat():
        cur = d - dt.timedelta(days=1)  # a streak survives until today is over
    for s in days:
        if s == cur.isoformat():
            streak += 1
            cur -= dt.timedelta(days=1)
        elif s < cur.isoformat():
            break
    return {"today": d.isoformat(), "days_to_deadline": (CFG.deadline - d).days, "phase": phase(d),
            "next_milestone": next_milestone(d), "words_by_status": by_status, "weeks": weeks,
            "top_error_categories": cats, "scorecard": bench,
            "practice_streak_days": streak, "note": "Streak = participation, not proficiency."}


def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def progress_html(page: int = 0) -> str:
    p = get_progress()
    page = max(0, min(page, 10_000))
    d = today()
    with closing(db()) as c:
        sessions = c.execute("SELECT on_date, topics, report, debrief, next_priorities FROM sessions "
                             "ORDER BY on_date DESC, id DESC LIMIT 5").fetchall()
        focus = c.execute("SELECT sg, de, en, status FROM words WHERE priority>0 ORDER BY priority DESC, id LIMIT 8").fetchall()
        due = c.execute("SELECT COUNT(*) FROM words WHERE (due_on IS NULL OR due_on<=?) AND status!='secure'",
                        (d.isoformat(),)).fetchone()[0]
        words = c.execute("SELECT sg, de, en, status FROM words ORDER BY CASE status WHEN 'secure' THEN 3 "
                          "WHEN 'used' THEN 2 WHEN 'with_help' THEN 1 ELSE 0 END DESC, sg LIMIT ? OFFSET ?",
                          (DASH_PAGE_SIZE + 1, page * DASH_PAGE_SIZE)).fetchall()
        mistakes = c.execute("SELECT said, correct, category, on_date FROM mistakes ORDER BY id DESC LIMIT 10").fetchall()
    total = sum(p["words_by_status"].values()) or 1
    bars = "".join(
        f'<div class="row"><span>{_esc(k)}</span><span class="bar"><i style="width:{100 * v / total:.0f}%"></i>'
        f'</span><b>{v}</b></div>' for k, v in p["words_by_status"].items())
    weeks = "".join(
        f'<tr><td>{w["week"][5:]}</td><td>{w["sessions"]}</td><td>{w["words_added"]}</td>'
        f'<td>{w["free_uses"]}</td><td>{w["corrections"]}</td></tr>' for w in p["weeks"])
    last = sessions[0] if sessions else None
    prios = "".join(f"<li>{_esc(x)}</li>" for x in (json.loads(last["next_priorities"]) if last else []))
    sess = "".join(
        f'<details><summary>{_esc(s["on_date"])} · {_esc(s["topics"])}</summary><p>{_esc(s["report"])}</p>'
        + (f'<p>{_esc(s["debrief"])}</p>' if s["debrief"] else "") + "</details>" for s in sessions)
    foc = "".join(f'<li><b>{_esc(w["sg"])}</b> <span class="m">{_esc(w["en"] or w["de"])} · '
                  f'{_esc(STATUS_LABEL[w["status"]])}</span></li>' for w in focus)
    mis = "".join(f'<li><s>{_esc(m["said"])}</s> → <b>{_esc(m["correct"])}</b> <span class="m">'
                  f'{_esc(m["category"])}</span></li>' for m in mistakes)
    cats = ", ".join(f'{_esc(x["category"])} ({x["n"]})' for x in p["top_error_categories"]) or "none yet"
    more = len(words) > DASH_PAGE_SIZE
    words = words[:DASH_PAGE_SIZE]
    nav = " ".join(x for x in (
        f'<a href="?page={page - 1}#all">previous</a>' if page else "",
        f'<a href="?page={page + 1}#all">next</a>' if more else "") if x)
    allw = "".join(f'<li><b>{_esc(w["sg"])}</b> <span class="m">{_esc(w["en"] or w["de"])} · '
                   f'{_esc(w["status"])}</span></li>' for w in words)
    ms = p["next_milestone"] or {}
    title = _esc(CFG.dialect_local)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title><style>
:root{{color-scheme:light dark;--fg:#111;--bg:#fff;--m:#777;--l:#e5e5e5}}@media(prefers-color-scheme:dark){{:root{{--fg:#eee;--bg:#000;--m:#888;--l:#262626}}}}
body{{font:16px/1.45 -apple-system,system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0 auto;padding:max(20px,env(safe-area-inset-top)) 20px 40px;max-width:720px}}
h1{{font-size:28px;margin:0 0 4px}}h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--m);margin:28px 0 8px;font-weight:600}}
.m{{color:var(--m)}}.big{{display:flex;gap:24px;margin:16px 0}}.big div b{{display:block;font-size:34px;line-height:1}}
.row{{display:grid;grid-template-columns:150px 1fr 32px;gap:10px;align-items:center;margin:6px 0;font-size:14px}}.bar{{height:8px;background:var(--l);border-radius:4px;overflow:hidden}}.bar i{{display:block;height:100%;background:var(--fg)}}
table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{text-align:left;padding:6px 4px;border-bottom:1px solid var(--l)}}th{{color:var(--m);font-weight:500}}
ul{{padding-left:18px;margin:0}}li{{margin:4px 0}}details{{border-bottom:1px solid var(--l);padding:8px 0}}summary{{cursor:pointer}}details p{{color:var(--m);font-size:14px}}
</style></head><body>
<h1>{title}</h1><div class="m">{_esc(CFG.learner_name)} · {d.strftime('%a %d %b %Y')} · {_esc(p['phase']['focus'])} · {_esc(p['phase']['daily_minutes'])} min/day</div>
<div class="big"><div><b>{p['days_to_deadline']}</b><span class="m">days to {_esc(CFG.deadline_label)}</span></div><div><b>{p['practice_streak_days']}</b><span class="m">day streak (participation)</span></div><div><b>{due}</b><span class="m">words due</span></div></div>
<div class="m">Next milestone: {_esc(ms.get('label'))} · {_esc(ms.get('date'))} ({_esc(ms.get('days_away'))} days)</div>
<h2>Next priorities</h2><ul>{prios or '<li class="m">No session yet</li>'}</ul>
<h2>Focus words</h2><ul>{foc or '<li class="m">none</li>'}</ul>
<h2>Words ({sum(p['words_by_status'].values())})</h2>{bars}
<h2>Weeks</h2><table><tr><th>Week</th><th>Sessions</th><th>New words</th><th>Free uses</th><th>Corrections</th></tr>{weeks}</table>
<h2>Recent corrections</h2><div class="m" style="font-size:14px;margin-bottom:6px">Most common: {cats}</div><ul>{mis or '<li class="m">none</li>'}</ul>
<h2>Sessions</h2>{sess or '<p class="m">none</p>'}
<h2 id="all">All words</h2><details{' open' if page else ''}><summary>Show {page * DASH_PAGE_SIZE + 1 if words else 0}-{page * DASH_PAGE_SIZE + len(words)}</summary><ul>{allw}</ul><p>{nav}</p></details>
<p class="m" style="font-size:12px;margin-top:32px">Read-only. Streak counts practice days, not proficiency. Unmeasured things stay unmeasured.</p>
</body></html>"""


PAGE_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}


PLACEHOLDER_TOKENS = {
    "replace-with-a-long-random-secret", "replace-with-another-long-random-secret", "changeme", "change-me",
    "secret", "token", "password", "your-token", "your-secret", "example", "test", "xxx",
}
PLACEHOLDER_MARKERS = ("replace-with", "replace_with", "changeme", "change-me", "change_me", "placeholder",
                       "your-token", "your_token", "your-secret", "your_secret", "example")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MIN_TOKEN_LEN = 32
GEN_HINT = "python -c 'import secrets;print(secrets.token_urlsafe(32))'"


def validate_token(name: str, value: str | None) -> str:
    """Return the token if it looks like a securely generated secret, else exit with a clear message."""
    v = (value or "").strip()
    low = v.lower()
    problem = None
    if not v:
        problem = "is not set"
    elif low in PLACEHOLDER_TOKENS or any(m in low for m in PLACEHOLDER_MARKERS):
        problem = "is a placeholder value"
    elif len(v) < MIN_TOKEN_LEN:
        problem = f"is shorter than {MIN_TOKEN_LEN} characters"
    elif not TOKEN_RE.match(v):
        problem = "must be URL-safe (letters, digits, '-' and '_' only)"
    elif len(set(v)) < 10:
        problem = "is not random enough"
    if problem:
        raise SystemExit(f"{name} {problem}. Generate one with: {GEN_HINT}")
    return v


_SECRET_PATH_RE = re.compile(r"/(mcp|progress)/[^\s/?#\"']+")


def redact(text: str, extra: tuple[str, ...] = ()) -> str:
    text = _SECRET_PATH_RE.sub(r"/\1/<redacted>", text)
    for t in extra:
        if t:
            text = text.replace(t, "<redacted>")
    return text


class RedactSecrets(logging.Filter):
    """Logging filter that strips secret path segments (and the configured tokens) from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        tokens = tuple(os.environ.get(k, "") for k in ("COACH_TOKEN", "COACH_DASH_TOKEN"))
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the server
            return True
        clean = redact(msg, tokens)
        if clean != msg:
            record.msg, record.args = clean, None
        # Tracebacks and stack text bypass getMessage(): render them now, redact, and drop the raw exc_info.
        if record.exc_info:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001
                record.exc_text = "<traceback unavailable>"
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text, tokens)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, tokens)
        return True


def host_allowed(host: str | None, allowed: list[str]) -> bool:
    """Same matching rules as the MCP SDK: exact match, or 'name:*' for any port."""
    if not host:
        return False
    host = host.lower()
    for a in allowed:
        a = a.lower()
        if host == a or (a.endswith(":*") and host.startswith(a[:-1])):
            return True
    return False


class RateLimiter:
    """In-process sliding-window limit per client plus a cap on concurrent requests. Single process only."""

    def __init__(self, per_minute: int, max_concurrent: int = 8, window: float = 60.0):
        self.per_minute, self.max_concurrent, self.window = per_minute, max_concurrent, window
        self.hits: dict[str, deque] = {}
        self.inflight = 0
        self.lock = threading.Lock()

    def acquire(self, key: str) -> str | None:
        """Return None if allowed (caller must release()), else the reason for refusing."""
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 10_000:  # drop idle clients so the table can't grow without bound
                self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - self.window}
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.per_minute:
                return "rate"
            if self.inflight >= self.max_concurrent:
                return "busy"
            q.append(now)
            self.inflight += 1
            return None

    def release(self) -> None:
        with self.lock:
            self.inflight -= 1


def app(token: str | None = None, dash_token: str | None = None, hosts: list[str] | None = None,
        rate_limit: int | None = None):
    """Build the ASGI app.

    Routes: /mcp/{token} (MCP) and /progress/{token} (read-only page). The path parameter is compared to the
    configured secret with secrets.compare_digest; a wrong token, and every other path, is 404. The Host
    allowlist and the rate limit apply to every request, before routing.
    """
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, PlainTextResponse
    from starlette.routing import Route, Router

    token = validate_token("COACH_TOKEN", token if token is not None else os.environ.get("COACH_TOKEN"))
    dash = validate_token("COACH_DASH_TOKEN",
                          dash_token if dash_token is not None else os.environ.get("COACH_DASH_TOKEN"))
    if secrets.compare_digest(token.encode(), dash.encode()):
        raise SystemExit("COACH_DASH_TOKEN must differ from COACH_TOKEN, so the page URL can't be used as the "
                         "connector URL.")
    if hosts is None:
        hosts = [h.strip() for h in os.environ.get("COACH_HOSTS", "127.0.0.1:*,localhost:*").split(",") if h.strip()]
    if rate_limit is None:
        rate_limit = int(os.environ.get("COACH_RATE_LIMIT", "120"))
    limiter = RateLimiter(max(1, rate_limit))
    not_found = PlainTextResponse("Not Found", status_code=404)

    def ok(given: str, secret: str) -> bool:
        return secrets.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))

    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, max_request_body_size=MAX_BODY_BYTES,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                     allowed_origins=[]),
    )

    class MCPEndpoint:  # a class instance, so Starlette passes raw ASGI through for every HTTP method
        async def __call__(self, scope, receive, send):
            if not ok(scope["path_params"].get("token", ""), token):
                return await not_found(scope, receive, send)
            # hand the request to the SDK app at its internal path; the secret never reaches it
            inner_scope = dict(scope, path="/mcp", raw_path=b"/mcp", root_path="")
            for k in ("path_params", "endpoint", "route", "router", "app"):
                inner_scope.pop(k, None)
            await inner(inner_scope, receive, send)

    async def progress(request: Request):
        if request.method != "GET" or not ok(request.path_params.get("token", ""), dash):
            return not_found
        try:
            page = int(request.query_params.get("page", "0"))
        except ValueError:
            page = 0
        return HTMLResponse(await run_in_threadpool(progress_html, page), headers=PAGE_HEADERS)

    router = Router(
        routes=[Route("/mcp/{token}", endpoint=MCPEndpoint()),
                Route("/progress/{token}", endpoint=progress, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])],
        redirect_slashes=False,
        lifespan=lambda _app: inner.router.lifespan_context(inner),
    )

    async def asgi(scope, receive, send):
        if scope["type"] != "http":  # lifespan; websockets are not served
            if scope["type"] == "lifespan":
                return await router(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1")
        if not host_allowed(host, hosts):
            return await PlainTextResponse("Invalid Host header", status_code=421)(scope, receive, send)
        client = (scope.get("client") or ("?", 0))[0]
        refused = limiter.acquire(client)
        if refused:
            resp = PlainTextResponse("Too Many Requests", status_code=429, headers={"Retry-After": "60"}) \
                if refused == "rate" else PlainTextResponse("Busy", status_code=503, headers={"Retry-After": "5"})
            return await resp(scope, receive, send)
        try:
            await router(scope, receive, send)
        finally:
            limiter.release()

    return asgi


def log_config() -> dict:
    """uvicorn's default logging config plus the RedactSecrets filter on every handler (root included)."""
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_secrets"] = {"()": "coach.server.RedactSecrets"}
    for h in cfg["handlers"].values():
        h.setdefault("filters", []).append("redact_secrets")
    cfg["root"] = {"handlers": ["default"], "level": "WARNING"}
    return cfg


def main() -> None:
    import uvicorn

    # access_log=False: request lines contain the secret path. log_config adds a redaction filter as a backstop.
    uvicorn.run(app(), host="127.0.0.1", port=int(os.environ.get("COACH_PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False, log_config=log_config(),
                server_header=False)


if __name__ == "__main__":
    main()
