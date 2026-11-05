import json
import logging
import logging.config
import secrets
from contextlib import closing
from pathlib import Path

import pytest
from starlette.testclient import TestClient

TOKEN = "Tk3vQ9xLm2Rb7NcW5pYz8HdF4sJa6GeU1oKi0Xq_-Vt"
DASH = "Dq8wE2rT5yU7iO9pA1sD3fG6hJ4kL0zX_-cVbNmQwEr"
HDRS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def client(srv):
    with TestClient(srv.app(token=TOKEN, dash_token=DASH, hosts=["testserver"]), base_url="http://testserver") as c:
        yield c


def rpc(client, method, params=None, id=1, token=TOKEN):
    body = {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    return client.post(f"/mcp/{token}", headers=HDRS, content=json.dumps(body))


def call(client, name, arguments, id=3):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments}, id=id)


def count(srv, table):
    with closing(srv.db()) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- token validation --------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "short",
    "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P",                # 31 chars: too short
    "replace-with-a-long-random-secret",              # the old .env.example values
    "replace-with-another-long-random-secret",
    "changeme-Q9xLm2Rb7NcW5pYz8HdF4sJa6GeU1oK",
    "your-token-Q9xLm2Rb7NcW5pYz8HdF4sJa6GeU1o",
    "x" * 40,                                         # not random
    "abc def ghi jkl mno pqr stu vwx yz0123456",      # not URL-safe
    "Tk3vQ9xLm2Rb7NcW5pYz8HdF4sJa6GeU1oKi/Xq+V=",     # base64, not URL-safe
    "",
    None,
])
def test_bad_mcp_token_refused(srv, monkeypatch, bad):
    monkeypatch.delenv("COACH_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        srv.app(token=bad, dash_token=DASH, hosts=["testserver"])


def test_short_generated_token_refused(srv):
    with pytest.raises(SystemExit, match="shorter than 32"):
        srv.app(token=secrets.token_urlsafe(16), dash_token=DASH, hosts=["testserver"])


def test_placeholder_token_refused(srv):
    with pytest.raises(SystemExit, match="placeholder"):
        srv.app(token="replace-with-a-long-random-secret", dash_token=DASH, hosts=["testserver"])


@pytest.mark.parametrize("bad", ["short", "replace-with-another-long-random-secret", "", None, "y" * 50])
def test_bad_dash_token_refused_independently(srv, monkeypatch, bad):
    monkeypatch.delenv("COACH_DASH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="COACH_DASH_TOKEN"):
        srv.app(token=TOKEN, dash_token=bad, hosts=["testserver"])


def test_same_token_for_both_refused(srv):
    with pytest.raises(SystemExit):
        srv.app(token=TOKEN, dash_token=TOKEN, hosts=["testserver"])


def test_env_example_ships_empty_tokens_and_server_refuses_it(srv, monkeypatch):
    env = dict(line.split("=", 1) for line in (Path(__file__).parent.parent / ".env.example").read_text().splitlines()
               if line and not line.startswith("#"))
    assert env["COACH_TOKEN"] == "" and env["COACH_DASH_TOKEN"] == ""
    monkeypatch.setenv("COACH_TOKEN", env["COACH_TOKEN"])
    monkeypatch.setenv("COACH_DASH_TOKEN", env["COACH_DASH_TOKEN"])
    with pytest.raises(SystemExit):
        srv.app()


def test_generated_tokens_accepted(srv):
    srv.app(token=secrets.token_urlsafe(32), dash_token=secrets.token_urlsafe(32), hosts=["testserver"])


# --- MCP over HTTP -----------------------------------------------------------------------------------------

def test_initialize_and_list_tools(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert "Alex" in res["instructions"]
    r = rpc(client, "tools/list", id=2)
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    expected = {"start_session", "finish_session", "update_focus", "log_mistake", "review_word",
                "log_words", "log_benchmark", "get_progress", "get_due_words"}
    assert expected == set(tools)
    assert tools["start_session"]["annotations"]["readOnlyHint"] is True
    assert tools["finish_session"]["annotations"]["readOnlyHint"] is False
    assert "debrief" in tools["finish_session"]["inputSchema"]["required"]
    # limits are advertised to the client in the schema
    assert tools["log_words"]["inputSchema"]["properties"]["words"]["maxItems"] == 30
    assert "maxLength" in json.dumps(tools["finish_session"]["inputSchema"])


def test_call_tool_over_http(client):
    r = call(client, "log_words", {"words": [{"sg": "Grüezi"}]})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False
    r = call(client, "start_session", {}, id=4)
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["learner"] == "Alex"


def test_wrong_token_is_404(client):
    assert rpc(client, "tools/list", token="wrong-token").status_code == 404
    assert rpc(client, "tools/list", token=TOKEN[:-1]).status_code == 404
    assert rpc(client, "tools/list", token=TOKEN + "x").status_code == 404
    assert rpc(client, "tools/list", token=DASH).status_code == 404       # page secret doesn't open MCP
    assert client.post("/mcp", headers=HDRS, content="{}").status_code == 404
    assert client.post("/mcp/", headers=HDRS, content="{}").status_code == 404
    assert client.post(f"/mcp/{TOKEN}/x", headers=HDRS, content="{}").status_code == 404
    assert client.get(f"/progress/{TOKEN}").status_code == 404           # dashboard has its own secret
    assert client.get("/progress/wrong").status_code == 404
    assert client.post(f"/progress/{DASH}").status_code == 404
    assert client.get("/").status_code == 404


def test_unknown_host_rejected(client):
    r = client.post(f"/mcp/{TOKEN}", headers=HDRS | {"host": "evil.example"}, content="{}")
    assert r.status_code == 421


def test_unknown_host_rejected_on_dashboard_and_other_paths(client):
    assert client.get(f"/progress/{DASH}", headers={"host": "evil.example"}).status_code == 421
    assert client.get(f"/progress/{DASH}", headers={"host": "testserver.evil.example"}).status_code == 421
    assert client.get("/anything", headers={"host": "evil.example"}).status_code == 421
    assert client.get(f"/progress/{DASH}").status_code == 200


def test_progress_page_escapes_and_headers(client, srv):
    srv.log_mistake("<script>alert(1)</script>", "ok", "test")
    r = client.get(f"/progress/{DASH}")
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-robots-tag"] == "noindex"


def test_progress_page_is_paginated(client, srv):
    for i in range(0, 250, 25):
        srv.log_words([srv.NewWord(sg=f"wort{j:03d}") for j in range(i, i + 25)])
    page0 = client.get(f"/progress/{DASH}").text
    assert "wort249" not in page0 and "next</a>" in page0
    last = client.get(f"/progress/{DASH}?page=2").text
    assert "wort249" in last and "next</a>" not in last
    assert client.get(f"/progress/{DASH}?page=abc").status_code == 200


# --- DoS limits --------------------------------------------------------------------------------------------

def is_rejected(r):
    if r.status_code != 200:
        return True
    body = r.json()
    return "error" in body or body["result"]["isError"] is True


def test_oversized_inputs_rejected(client, srv):
    assert is_rejected(call(client, "log_words", {"words": [{"sg": f"w{i}"} for i in range(31)]}))
    assert is_rejected(call(client, "log_words", {"words": [{"sg": "x" * 201}]}))
    assert is_rejected(call(client, "log_mistake", {"said": "a" * 1001, "correct": "b"}))
    assert is_rejected(call(client, "review_word", {"sg": "", "result": "free"}))
    assert is_rejected(call(client, "get_due_words", {"limit": 100000}))
    assert is_rejected(call(client, "update_focus", {"move_on": ["w"] * 21}))
    assert is_rejected(call(client, "log_benchmark", {"week_ending": "x" * 50}))
    base = {"topics": "t", "report": "r", "debrief": "d", "next_priorities": ["a"]}
    assert is_rejected(call(client, "finish_session", base | {"debrief": "d" * 20001}))
    assert is_rejected(call(client, "finish_session", base | {"next_priorities": ["a"] * 11}))
    assert is_rejected(call(client, "finish_session", base | {"corrections": [{"said": "a", "correct": "b"}] * 31}))
    assert is_rejected(call(client, "finish_session", base | {"word_results": [{"sg": "a", "result": "free"}] * 41,
                                                              "new_words": [{"sg": "neu"}]}))
    # nothing was written by the rejected calls
    assert count(srv, "words") == count(srv, "mistakes") == count(srv, "sessions") == 0
    # within limits still works
    assert not is_rejected(call(client, "log_words", {"words": [{"sg": f"w{i}"} for i in range(30)]}))
    assert count(srv, "words") == 30


def test_oversized_batch_rejected_before_work_on_direct_call(srv):
    with pytest.raises(ValueError):
        srv.finish_session("t", "r", "d", ["a"], word_results=[srv.WordResult(sg="a", result="free")] * 41,
                           new_words=[srv.NewWord(sg="neu")])
    with pytest.raises(ValueError):
        srv.log_words([srv.NewWord(sg=f"w{i}") for i in range(31)])
    assert count(srv, "words") == count(srv, "sessions") == 0


def test_request_body_limit(client, srv):
    big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "pad": "x" * (srv.MAX_BODY_BYTES + 1)})
    assert client.post(f"/mcp/{TOKEN}", headers=HDRS, content=big).status_code == 413


def test_rate_limit(srv):
    with TestClient(srv.app(token=TOKEN, dash_token=DASH, hosts=["testserver"], rate_limit=5),
                    base_url="http://testserver") as c:
        codes = [c.get("/nope").status_code for _ in range(7)]
    assert codes == [404] * 5 + [429, 429]


# --- logging -----------------------------------------------------------------------------------------------

def test_access_log_disabled(srv, monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setenv("COACH_TOKEN", TOKEN)
    monkeypatch.setenv("COACH_DASH_TOKEN", DASH)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    srv.main()
    assert seen["access_log"] is False
    assert seen["host"] == "127.0.0.1"
    cfg = seen["log_config"]
    assert any("RedactSecrets" in str(f) for f in cfg["filters"].values())
    assert all("redact_secrets" in h["filters"] for h in cfg["handlers"].values())
    logging.config.dictConfig(cfg)  # the config is valid


def test_log_redaction(srv, monkeypatch):
    monkeypatch.setenv("COACH_TOKEN", TOKEN)
    monkeypatch.setenv("COACH_DASH_TOKEN", DASH)
    rec = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "failed %s, %s, token=%s",
                            (f"POST /mcp/{TOKEN} HTTP/1.1", f"/progress/{DASH}?page=1", TOKEN), None)
    assert srv.RedactSecrets().filter(rec) is True
    msg = rec.getMessage()
    assert TOKEN not in msg and DASH not in msg
    assert "/mcp/<redacted>" in msg and "/progress/<redacted>" in msg


def test_log_redaction_covers_tracebacks_and_stack(srv, monkeypatch):
    monkeypatch.setenv("COACH_TOKEN", TOKEN)
    monkeypatch.setenv("COACH_DASH_TOKEN", DASH)
    try:
        raise RuntimeError(f"boom at /mcp/{TOKEN} and {DASH}")
    except RuntimeError:
        import sys
        exc = sys.exc_info()
    rec = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI app", (), exc)
    rec.stack_info = f"Stack: /progress/{DASH} {TOKEN}"
    assert srv.RedactSecrets().filter(rec) is True
    out = logging.Formatter().format(rec)
    assert TOKEN not in out and DASH not in out
    assert rec.exc_info is None and "<redacted>" in rec.exc_text
