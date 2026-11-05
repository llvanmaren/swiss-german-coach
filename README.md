# Swiss German Coach for ChatGPT

A small, self-hosted [MCP](https://modelcontextprotocol.io) server that turns ChatGPT (text or voice mode) into a
Swiss German (Züritüütsch by default) speaking coach that **remembers you between chats**.

## The problem

ChatGPT is a good conversation partner for a dialect with few learning resources, but every new chat starts from
zero. It forgets which words you can already use, repeats the same role-plays, can't tell you whether you're
improving, and tends to make up progress ("great, 15 minutes today!"). Keeping a Markdown tracker by hand works
for about a week.

## How it works

ChatGPT does the talking. This server is the coach's memory, exposed as tools in a ChatGPT App (connector):

| Tool | What it does |
|---|---|
| `start_session` | Called first. Returns coaching rules, your mission, the last 3 session debriefs and **today's plan**: focus words, new words to introduce, and words due for light review. |
| `finish_session` | Closes a session with an honest report, a full **debrief** the next chat continues from, max 3 next priorities, and (in one batch) all corrections, word results and new words not yet saved. |
| `update_focus` | "Let's move on from these words" or "practise this one": saved for all future sessions. |
| `log_mistake` | Saves a correction (what you said → correct form, category). |
| `review_word` | Records a word use: `free` (unprompted, in a new sentence), `with_help`, or `missed`. Drives spaced repetition. |
| `log_words` | Saves genuinely new useful words, deduplicated. |
| `log_benchmark` | Weekly scorecard (first-try answers out of 10, language switches per 10 min, ...). Unmeasured fields stay empty. |
| `get_progress`, `get_due_words` | Read-only summaries. |

Design choices:

- **Spaced repetition (Leitner boxes).** Intervals 1/2/4/7/14/30/60 days. A word only becomes *secure* after
  free use on **3 different days**; repeating after the coach does not count.
- **Focus rotation.** A small set of focus words (default 4). A word leaves focus once you use it freely or after
  3 sessions, then stays in spaced review; the set refills automatically, alternating shaky and new words.
  Words you moved on from don't come back into focus.
- **Debrief as memory.** Each `finish_session` writes what was talked about, what improved, what's shaky, what's
  done and shouldn't be repeated, and what to do next. `start_session` hands this to the next chat.
- **Honesty rules.** The server instructions tell the model never to invent minutes, scores, pronunciation
  judgements or streaks. The streak on the progress page counts participation, not proficiency.
- **Phases, milestones, benchmark day** from your config, so the plan knows where you are in it.
- **Progress page**: a read-only HTML page (no JavaScript, strict CSP, `noindex`) at a separate secret URL.

Everything is one Python module, Python's built-in SQLite, and the official `mcp` SDK. No external services.

## Setup

Requirements: Python 3.11+, a Linux box that's always on (a small VPS, home server or Raspberry Pi), and a way to
expose HTTPS (Caddy with a domain, or a Cloudflare Tunnel).

```bash
git clone <this repo> ~/swiss-german-coach && cd ~/swiss-german-coach
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # exact pins; or: pip install --require-hashes -r requirements.lock

cp config.example.toml config.toml   # your name, goal, deadline, timezone, phases (gitignored)
cp .env.example .env && chmod 600 .env
python -c 'import secrets;print(secrets.token_urlsafe(32))'   # paste as COACH_TOKEN
python -c 'import secrets;print(secrets.token_urlsafe(32))'   # run again, paste as COACH_DASH_TOKEN
# set COACH_HOSTS to include your public hostname

.venv/bin/python -m pytest -q        # tests
```

Optional demo data (a fictional learner, invented sessions) to look around first:

```bash
COACH_DB=data/demo.db .venv/bin/python scripts/seed_demo.py
```

### Configuration (`config.toml`)

See [`config.example.toml`](config.example.toml). `[learner]` holds name, goal, deadline, timezone and an optional
`mission_file` (a longer Markdown plan returned verbatim by `start_session`; keep it in `data/`, which is
gitignored). `[dialect]` sets the dialect names used in the coaching rules, `[coach]` the focus size, rotation
length, benchmark weekday and "I'm done" words. `[[phases]]` and `[[milestones]]` describe your plan.

### Authentication

There are no accounts. The MCP endpoint lives at a **secret path**: `https://<host>/mcp/<COACH_TOKEN>`. The
progress page uses its own secret, `COACH_DASH_TOKEN` (`https://<host>/progress/<COACH_DASH_TOKEN>`), so you can
bookmark it without exposing the connector URL. Both tokens are required and validated independently: the server
refuses to start if either is missing, shorter than 32 characters, not URL-safe, a placeholder, or the same as the
other. `.env.example` ships them empty on purpose. Generate each with
`python -c 'import secrets;print(secrets.token_urlsafe(32))'`.

The token in the path is compared with `secrets.compare_digest`. A wrong token, `/mcp` itself, and every other path
return 404. The `Host` header is checked against `COACH_HOSTS` for **every** request, including the progress page
(DNS-rebinding protection; unknown hosts get 421), and the server only binds `127.0.0.1`.

Limits against abuse: tool arguments have maximum lengths and item counts in their schemas (oversized calls are
rejected before anything is written), request bodies are capped at 512 KiB, there is an in-process rate limit of
`COACH_RATE_LIMIT` requests per minute per client IP (default 120) plus a cap on concurrent requests, and the
progress page is paginated. Behind Cloudflare Tunnel every request comes from `127.0.0.1`, so the rate limit is
effectively global, which is fine for one user.

Treat the connector URL like a password: anyone with it can read and write your learning data. To rotate, change
`COACH_TOKEN`, restart, and update the connector in ChatGPT. If you ran an earlier version of this template (which
left uvicorn's access log on), rotate both tokens: they may be in your journal. This is deliberately simple single-user auth; if you
need multiple users, put OAuth in front (the `mcp` SDK supports it).

### Run it with systemd

```bash
mkdir -p ~/.config/systemd/user
cp deploy/swiss-german-coach.service ~/.config/systemd/user/   # adjust paths if not ~/swiss-german-coach
systemctl --user daemon-reload
systemctl --user enable --now swiss-german-coach
loginctl enable-linger "$USER"                                  # keep running after logout
journalctl --user -u swiss-german-coach -f
```

### Expose it over HTTPS

**Option A: Caddy** (you have a domain pointing at the server, ports 80/443 open). Use
[`deploy/Caddyfile.example`](deploy/Caddyfile.example); Caddy fetches certificates automatically. Its access
log is discarded so the secret path doesn't end up in log files.

**Option B: Cloudflare Tunnel** (no open ports, works behind NAT). Use
[`deploy/cloudflared.example.yml`](deploy/cloudflared.example.yml) and
[`deploy/cloudflared.service`](deploy/cloudflared.service):

```bash
cloudflared tunnel login
cloudflared tunnel create swiss-german-coach
cloudflared tunnel route dns swiss-german-coach coach.example.com
systemctl --user enable --now cloudflared
```

Check it end to end (read-only):

```bash
.venv/bin/python scripts/smoke.py "https://coach.example.com/mcp/$COACH_TOKEN"
```

## Connecting in ChatGPT Apps

1. In ChatGPT, open **Settings → Apps** (formerly *Connectors*) and enable **Developer mode** under *Advanced*.
   Availability depends on your plan and workspace settings.
2. **Create app**: name it e.g. *Züritüütsch Coach*, paste `https://<host>/mcp/<COACH_TOKEN>` as the MCP server URL,
   choose **No authentication** (the secret path is the auth), and confirm.
3. In a new chat, add the app and say something like *"Let's practise Swiss German"*. The model calls
   `start_session`, then opens with a question in dialect. Say *"fertig"* when done so it calls `finish_session`.
4. Voice mode: start in a chat that has the app enabled. Tool calls may happen less often in voice, which is why
   `finish_session` accepts everything in one batch.

After changing tool definitions, refresh the app in ChatGPT's settings so it picks up the new schema.

## Privacy

- All data stays in one SQLite file on your server (`data/coach.db` by default). Nothing is sent anywhere except
  what ChatGPT itself receives through the tool calls, under OpenAI's terms for your account.
- `config.toml`, `.env`, `data/` and `*.db` are gitignored. Don't commit them, and don't put real data in issues.
- The progress page is read-only, unindexed, uncached, no-referrer, and runs no scripts.
- The server runs uvicorn with its access log disabled (`access_log=False`), so request lines containing the secret
  path are never logged. As a backstop, a logging filter replaces anything that looks like `/mcp/<token>` or
  `/progress/<token>`, and the configured tokens themselves, with `<redacted>` in every log record. Startup and
  error messages still go to journald. Keep your reverse proxy from logging the path too (the Caddy example
  discards its access log; Cloudflare does not log request paths to your machine).
- Delete everything by deleting the database file.

## Development

```bash
.venv/bin/python -m pytest -q
```

Tests run against a temporary database with the example config and a fixed fake date. They cover config loading,
spaced repetition, focus rotation, the batch `finish_session`, benchmarks, streaks, the HTTP transport, token
validation (missing, short, placeholder, non-URL-safe), secret-path auth (wrong token is 404), Host checks on every
path, input-size limits, the rate limit, pagination, disabled access log, log redaction and HTML escaping.

Dependencies: `requirements.txt` pins the direct dependencies exactly; `requirements.lock` is a `pip freeze` of the
tested environment with sha256 hashes (`pip install --require-hashes -r requirements.lock` for a reproducible
deployment). Check them with `pip install pip-audit && pip-audit -r requirements.lock`.

## License

[MIT](LICENSE) © Luka van Maren
