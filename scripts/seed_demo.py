"""Fill an EMPTY database with synthetic demo data (fictional learner, invented sessions).

    COACH_DB=data/demo.db python scripts/seed_demo.py

Idempotent: does nothing if the database already has words. Never point it at real data you care about.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from coach import server as s  # noqa: E402

# (sg, de, en, status, context, focus)
WORDS = [
    ("Grüezi", "Guten Tag", "hello (formal)", "used", "Greetings", 0),
    ("Merci vilmal", "Vielen Dank", "thanks a lot", "used", "Shop / café", 0),
    ("hüt", "heute", "today", "used", "Daily routine", 0),
    ("morn", "morgen", "tomorrow", "with_help", "Plans", 1),
    ("geschter", "gestern", "yesterday", "with_help", "Telling what happened", 1),
    ("öppis", "etwas", "something", "with_help", "General", 0),
    ("nöd", "nicht", "not", "used", "General", 0),
    ("chasch", "kannst du", "can you", "new", "Asking for help", 1),
    ("es Billett", "eine Fahrkarte", "a ticket", "new", "Train station", 0),
    ("de Znüni", "Pausenbrot (9 Uhr)", "mid-morning snack", "new", "Work / school", 0),
    ("ich ha kei Ahnig", "ich habe keine Ahnung", "I have no idea", "new", "Buying time", 1),
    ("gang mer go poschte", "gehen wir einkaufen", "let's go shopping", "new", "Weekend plans", 0),
    ("lueg emal", "schau mal", "look", "with_help", "Pointing things out", 0),
    ("chunsch au?", "kommst du auch?", "are you coming too?", "new", "Invitations", 0),
]
MISTAKES = [
    ("ich bin gestern gegangen", "ich bi geschter gange", "Standard German form", 3),
    ("ich kann dich helfen", "ich cha dir hälfe", "dative", 3),
    ("am Wochenende", "am Wuchenänd", "vocabulary", 1),
]
SESSIONS = [
    (4, "Morning routine, the weather", "Answered in short sentences; switched to English twice when stuck.",
     "Talked about breakfast and the commute. Shaky: past tense with «gsi». Done: greetings. Next: weekend plans.",
     ["Use «geschter» in a new sentence", "Say «ich ha kei Ahnig» instead of switching language"]),
    (1, "Weekend plans, a café visit", "Longer answers; used «hüt» and «nöd» unprompted.",
     "Planned a fictional hike and ordered at a café. Improved: «hüt», «nöd». Still shaky: «morn». "
     "Do not repeat the café role-play. Next: train station and tickets.",
     ["Introduce «es Billett»", "One short story in the past", "Ask a follow-up question back"]),
]


def main() -> None:
    d = s.today()
    with closing(s.db()) as c, c:
        if c.execute("SELECT COUNT(*) FROM words").fetchone()[0]:
            print("database already has words; nothing seeded")
            return
        added = (d - dt.timedelta(days=7)).isoformat()
        for sg, de, en, status, ctx, focus in WORDS:
            c.execute("INSERT INTO words(key,sg,de,en,status,context,priority,due_on,added_on) VALUES(?,?,?,?,?,?,?,?,?)",
                      (s.norm(sg), sg, de, en, status, ctx, focus, d.isoformat(), added))
        for said, correct, cat, ago in MISTAKES:
            day = (d - dt.timedelta(days=ago)).isoformat()
            c.execute("INSERT INTO mistakes(said,correct,category,on_date,at) VALUES(?,?,?,?,?)",
                      (said, correct, cat, day, day + "T19:00:00"))
        for ago, topics, report, debrief, prios in SESSIONS:
            day = (d - dt.timedelta(days=ago)).isoformat()
            c.execute("INSERT INTO sessions(on_date,topics,report,debrief,next_priorities,at) VALUES(?,?,?,?,?,?)",
                      (day, topics, report, debrief, json.dumps(prios, ensure_ascii=False), day + "T19:30:00"))
    print(f"seeded {len(WORDS)} words, {len(MISTAKES)} corrections, {len(SESSIONS)} sessions into {s.DB}")


if __name__ == "__main__":
    main()
