import datetime as dt
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TODAY = dt.date(2026, 11, 10)  # a Tuesday inside the first example phase


@pytest.fixture
def clock():
    """Mutable fake 'today' so tests can move between days."""
    return {"d": TODAY}


@pytest.fixture
def srv(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("COACH_DB", str(tmp_path / "coach.db"))
    monkeypatch.setenv("COACH_CONFIG", str(ROOT / "config.example.toml"))
    import coach.server
    s = importlib.reload(coach.server)
    monkeypatch.setattr(s, "today", lambda: clock["d"])
    return s
