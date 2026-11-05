# Result

Public, MIT-licensed template of a Swiss German (Züritüütsch) speaking coach for ChatGPT Apps, built as an MCP
tool server.

## Summary

- `coach/server.py`: MCP server (official `mcp` SDK, streamable HTTP, stateless JSON) with the tools
  `start_session`, `finish_session` (debrief plus batch save of corrections, word results and new words),
  `update_focus`, `log_mistake`, `review_word`, `log_words`, `log_benchmark`, `get_progress` and `get_due_words`.
  Leitner spaced repetition ("secure" only after free use on 3 different days), automatic focus rotation, and a
  read-only progress page with a strict CSP.
- `coach/config.py` + `config.example.toml`: the learner's name, goal, deadline, timezone, dialect, optional
  mission file, phases, milestones, benchmark weekday and focus settings all come from TOML. Nothing about the
  learner is hard-coded.
- Auth: a secret path token from `COACH_TOKEN` (`/mcp/<token>`) and a required, separate `COACH_DASH_TOKEN` for
  the progress page, both validated at startup (>= 32 URL-safe chars, no placeholders) and compared with
  `secrets.compare_digest` (wrong token is 404); a Host allow-list on every path; binding to 127.0.0.1 only.
- Hardening: uvicorn access log disabled plus a log redaction filter, schema length/item limits, a 512 KiB body
  cap, an in-process rate limit, a paginated progress page, and exactly pinned dependencies (`requirements.lock`).
- `scripts/seed_demo.py`: synthetic demo data (a fictional learner and invented sessions). `scripts/smoke.py`:
  an end-to-end HTTP check.
- `deploy/`: a systemd user unit, a Caddyfile, and a Cloudflare Tunnel config plus its unit.
- README: the problem, how it works, setup, auth, systemd, Caddy/Cloudflare, connecting in ChatGPT Apps, privacy,
  and the MIT license.
- I also ran a live check: seeded a scratch DB, started the server, and ran `smoke.py --write`. All tools
  responded, a wrong token returned 404 and the progress page returned 200.

## Test output

```
test_http.py::test_bad_mcp_token_refused[short] PASSED             [  1%]
test_http.py::test_bad_mcp_token_refused[a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P] PASSED [  3%]
test_http.py::test_bad_mcp_token_refused[replace-with-a-long-random-secret] PASSED [  5%]
test_http.py::test_bad_mcp_token_refused[replace-with-another-long-random-secret] PASSED [  7%]
test_http.py::test_bad_mcp_token_refused[changeme-Q9xLm2Rb7NcW5pYz8HdF4sJa6GeU1oK] PASSED [  9%]
test_http.py::test_bad_mcp_token_refused[your-token-Q9xLm2Rb7NcW5pYz8HdF4sJa6GeU1o] PASSED [ 11%]
test_http.py::test_bad_mcp_token_refused[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx] PASSED [ 13%]
test_http.py::test_bad_mcp_token_refused[abc def ghi jkl mno pqr stu vwx yz0123456] PASSED [ 15%]
test_http.py::test_bad_mcp_token_refused[Tk3vQ9xLm2Rb7NcW5pYz8HdF4sJa6GeU1oKi/Xq+V=] PASSED [ 16%]
test_http.py::test_bad_mcp_token_refused[] PASSED                  [ 18%]
test_http.py::test_bad_mcp_token_refused[None] PASSED              [ 20%]
test_http.py::test_short_generated_token_refused PASSED            [ 22%]
test_http.py::test_placeholder_token_refused PASSED                [ 24%]
test_http.py::test_bad_dash_token_refused_independently[short] PASSED [ 26%]
test_http.py::test_bad_dash_token_refused_independently[replace-with-another-long-random-secret] PASSED [ 28%]
test_http.py::test_bad_dash_token_refused_independently[] PASSED   [ 30%]
test_http.py::test_bad_dash_token_refused_independently[None] PASSED [ 32%]
test_http.py::test_bad_dash_token_refused_independently[yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy] PASSED [ 33%]
test_http.py::test_same_token_for_both_refused PASSED              [ 35%]
test_http.py::test_env_example_ships_empty_tokens_and_server_refuses_it PASSED [ 37%]
test_http.py::test_generated_tokens_accepted PASSED                [ 39%]
test_http.py::test_initialize_and_list_tools PASSED                [ 41%]
test_http.py::test_call_tool_over_http PASSED                      [ 43%]
test_http.py::test_wrong_token_is_404 PASSED                       [ 45%]
test_http.py::test_unknown_host_rejected PASSED                    [ 47%]
test_http.py::test_unknown_host_rejected_on_dashboard_and_other_paths PASSED [ 49%]
test_http.py::test_progress_page_escapes_and_headers PASSED        [ 50%]
test_http.py::test_progress_page_is_paginated PASSED               [ 52%]
test_http.py::test_oversized_inputs_rejected PASSED                [ 54%]
test_http.py::test_oversized_batch_rejected_before_work_on_direct_call PASSED [ 56%]
test_http.py::test_request_body_limit PASSED                       [ 58%]
test_http.py::test_rate_limit PASSED                               [ 60%]
test_http.py::test_access_log_disabled PASSED                      [ 62%]
test_http.py::test_log_redaction PASSED                            [ 64%]
test_tools.py::test_example_config_loads PASSED                    [ 66%]
test_tools.py::test_config_requires_learner_fields PASSED          [ 67%]
test_tools.py::test_config_mission_file_relative PASSED            [ 69%]
test_tools.py::test_rules_use_config PASSED                        [ 71%]
test_tools.py::test_start_session_empty PASSED                     [ 73%]
test_tools.py::test_benchmark_day_flag PASSED                      [ 75%]
test_tools.py::test_log_words_dedupes_and_fills PASSED             [ 77%]
test_tools.py::test_review_word_secure_needs_three_days PASSED     [ 79%]
test_tools.py::test_review_word_with_help_and_unknown PASSED       [ 81%]
test_tools.py::test_log_mistake_counts_category PASSED             [ 83%]
test_tools.py::test_update_focus_add_and_move_on PASSED            [ 84%]
test_tools.py::test_focus_rotates_after_max_sessions PASSED        [ 86%]
test_tools.py::test_focus_graduates_freely_used_word PASSED        [ 88%]
test_tools.py::test_finish_session_saves_batch_and_debrief PASSED  [ 90%]
test_tools.py::test_minutes_not_invented PASSED                    [ 92%]
test_tools.py::test_log_benchmark_counts_and_replaces PASSED       [ 94%]
test_tools.py::test_progress_streak_and_counts PASSED              [ 96%]
test_tools.py::test_get_due_words PASSED                           [ 98%]
test_tools.py::test_seed_demo_is_synthetic_and_idempotent PASSED   [100%]
============================== 53 passed in 2.56s ==============================
```

## Dependency audit

`pip-audit -r requirements.lock --require-hashes --disable-pip` (pip-audit 2.10.1): No known vulnerabilities found.
