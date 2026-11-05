"""Learner configuration: who is learning, which dialect, by when, and the study plan.

Loaded from a TOML file (path in COACH_CONFIG, default ./config.toml, falling back to
config.example.toml so the demo runs out of the box).
"""
from __future__ import annotations

import datetime as dt
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Phase:
    start: dt.date
    end: dt.date
    daily_minutes: str
    focus: str


@dataclass(frozen=True)
class Milestone:
    date: dt.date
    label: str


@dataclass(frozen=True)
class Config:
    learner_name: str
    goal: str
    deadline: dt.date
    deadline_label: str
    timezone: ZoneInfo
    background: str = ""
    dialect: str = "Zürich German"
    dialect_local: str = "Züritüütsch"
    extra_dialects: str = ""
    focus_size: int = 4
    focus_max_sessions: int = 3
    benchmark_weekday: int = 6  # Monday=0 ... Sunday=6
    finish_words: tuple[str, ...] = ("fertig", "tschüss")
    mission_file: Path | None = None
    phases: tuple[Phase, ...] = field(default_factory=tuple)
    milestones: tuple[Milestone, ...] = field(default_factory=tuple)

    @property
    def mission_text(self) -> str | None:
        if self.mission_file and self.mission_file.is_file():
            return self.mission_file.read_text(encoding="utf-8")
        return None


def _date(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def default_path() -> Path:
    env = os.environ.get("COACH_CONFIG")
    if env:
        return Path(env).expanduser()
    local = ROOT / "config.toml"
    return local if local.is_file() else ROOT / "config.example.toml"


def load(path: str | Path | None = None) -> Config:
    p = Path(path).expanduser() if path else default_path()
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    learner, dialect, coach = raw.get("learner", {}), raw.get("dialect", {}), raw.get("coach", {})
    for key in ("name", "goal", "deadline"):
        if key not in learner:
            raise ValueError(f"{p}: [learner].{key} is required")
    mission = learner.get("mission_file")
    if mission:
        mission = Path(mission).expanduser()
        if not mission.is_absolute():
            mission = (p.parent / mission).resolve()
    weekday = int(coach.get("benchmark_weekday", 6))
    if not 0 <= weekday <= 6:
        raise ValueError("[coach].benchmark_weekday must be 0 (Mon) .. 6 (Sun)")
    phases = tuple(sorted(
        (Phase(_date(x["start"]), _date(x["end"]), str(x.get("daily_minutes", "")), x.get("focus", ""))
         for x in raw.get("phases", [])), key=lambda ph: ph.start))
    milestones = tuple(sorted(
        (Milestone(_date(x["date"]), x["label"]) for x in raw.get("milestones", [])), key=lambda m: m.date))
    return Config(
        learner_name=learner["name"],
        goal=learner["goal"],
        deadline=_date(learner["deadline"]),
        deadline_label=learner.get("deadline_label", "the deadline"),
        timezone=ZoneInfo(learner.get("timezone", "UTC")),
        background=learner.get("background", ""),
        dialect=dialect.get("name", "Zürich German"),
        dialect_local=dialect.get("local_name", dialect.get("name", "Züritüütsch")),
        extra_dialects=dialect.get("comprehension_later", ""),
        focus_size=max(1, int(coach.get("focus_size", 4))),
        focus_max_sessions=max(1, int(coach.get("focus_max_sessions", 3))),
        benchmark_weekday=weekday,
        finish_words=tuple(coach.get("finish_words", ["fertig", "tschüss"])),
        mission_file=mission,
        phases=phases,
        milestones=milestones,
    )
