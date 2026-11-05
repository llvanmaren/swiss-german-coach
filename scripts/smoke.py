"""Smoke-test a running server over HTTP with the MCP client (read-only unless --write).

    python scripts/smoke.py https://coach.example.com/mcp/<token>

--write calls the write tools with obviously fake values: only use it against a scratch database.
"""
import asyncio
import json
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, write: bool) -> None:
    async with streamable_http_client(url) as (r, w, *_):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("server:", init.server_info.name, "| instructions:", bool(init.instructions))
            tools = await s.list_tools()
            print("tools:", sorted(t.name for t in tools.tools))
            d = json.loads((await s.call_tool("start_session", {})).content[0].text)
            print("start:", d["today"], "| days to deadline:", d["days_to_deadline"],
                  "| focus:", [x["sg"] for x in d["todays_plan"]["focus_words"]])
            if write:
                for name, args in [("log_words", {"words": [{"sg": "zz-smoke-test", "en": "test"}]}),
                                   ("review_word", {"sg": "zz-smoke-test", "result": "free"}),
                                   ("log_mistake", {"said": "TEST", "correct": "TEST", "category": "TEST"})]:
                    out = await s.call_tool(name, args)
                    print(name, "->", out.content[0].text[:120].replace("\n", " "), "| error:", out.is_error)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], "--write" in sys.argv))
