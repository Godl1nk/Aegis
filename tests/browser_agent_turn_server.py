"""Offline browser QA: python -m tests.browser_agent_turn_server (port 8767).

Serves real markup/renderers with fixture data only. No app DB, model calls,
external searches, or persisted user state. Visit / for the test controls.
"""
import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

root = Path(__file__).resolve().parents[1]
app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def page():
    html = (root / "static/index.html").read_text(encoding="utf-8")
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html)
    return html.replace("</body>", '<script type="module" src="/tests/agent_turn_rendering.browser.js"></script></body>')


@app.post("/api/chat_stream")
async def stream(request: Request):
    body = await request.form()
    approval = "approval" in str(dict(body)).lower()

    async def chunks():
        def event(data):
            return f"data: {json.dumps(data)}\n\n"

        for n in range(4):
            yield event({"type": "context_usage", "data": {"model": "Qwen-QA", "used_tokens": 2000 + n * 1500, "basis": "request", "usage_source": "estimated"}})
            if n:
                yield event({"type": "agent_step", "step": n + 1})
            yield event({"delta": f"<think>Checking fixture {n + 1}.</think>Intermediate update {n + 1}."})
            await asyncio.sleep(0.15)
            yield event({"type": "tool_start", "tool": "web_search", "command": f"Search fixture {n + 1}"})
            if approval and n == 0:
                yield event({"type": "approval_request", "approval_id": "qa-only", "description": "Offline approval visibility test", "command": "QA fixture — no command will execute"})
                await asyncio.sleep(15)
            await asyncio.sleep(0.3)
            yield event({"type": "tool_output", "tool": "web_search", "output": "Fixture search output", "exit_code": 1 if n == 1 else 0})
        yield event({"type": "agent_step", "step": 5})
        yield event({"type": "web_sources", "data": [{"title": "Fixture source", "url": "https://example.org/source"}]})
        yield event({"delta": "Final answer: all four source checks are complete."})
        yield event({"type": "metrics", "data": {"model": "Qwen-QA", "tokens_per_second": 25, "response_time": 2, "input_tokens": 40000, "context_tokens": 8000, "context_output_tokens": 500, "usage_source": "real"}})
        yield event({"type": "message_saved", "id": "qa-live-response"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")


@app.get("/context", response_class=HTMLResponse)
def context_page():
    return page().replace("agent_turn_rendering.browser.js", "context_usage.browser.js")


@app.get("/api/session/{session_id}/context_info")
async def context_info(session_id: str):
    if session_id == "slow":
        await asyncio.sleep(0.3)
    return {
        "model": "Qwen-QA", "used_tokens": 1000 if session_id == "compacted" else 2000,
        "context_length": None if session_id == "unknown" else 10000,
        "context_length_known": session_id != "unknown", "basis": "history",
        "usage_source": "estimated", "compacted": session_id == "compacted", "compact_threshold": 0.85,
    }


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PATCH"])
async def empty_api(path: str):
    if path == "sessions":
        return []
    if path == "tools":
        return {"tools": [{"id": "web_search"}]}
    if path.startswith("chat/status"):
        return {"status": "done", "running": False}
    return {}


app.mount("/static", StaticFiles(directory=root / "static"), name="static")
app.mount("/tests", StaticFiles(directory=root / "tests"), name="tests")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8767)
