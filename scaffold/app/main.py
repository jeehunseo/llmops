"""FastAPI deployment of the graph.

Nothing about the agent is defined here. This module owns only what a
notebook does not have to care about: process lifecycle, request/response
shapes, streaming, cancellation and thread identity.
"""

import json
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage

from agent.checkpointer import open_checkpointer
from agent.graph import build_graph
from agent.otel import setup_tracing
from agent.settings import get_settings
from app.schemas import ChatRequest, ChatResponse, StateResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_tracing(app, settings)

    # Compile once. Compiling per request would rebuild the graph and, with
    # Postgres, open a new pool on every call.
    async with open_checkpointer(settings) as checkpointer:
        app.state.graph = build_graph().compile(checkpointer=checkpointer)
        app.state.settings = settings
        yield


app = FastAPI(title="scaffold-agent", lifespan=lifespan)


def _config(thread_id: str) -> dict:
    return {
        "configurable": {"thread_id": thread_id},
        # Backstop against a cyclic graph that never routes to END.
        "recursion_limit": 25,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest):
    thread_id = body.thread_id or str(uuid.uuid4())
    result = await request.app.state.graph.ainvoke(
        {"messages": [HumanMessage(content=body.user_prompt)], "tool_iterations": 0},
        _config(thread_id),
    )
    return ChatResponse(thread_id=thread_id, content=result["messages"][-1].content)


@app.post("/v1/chat/stream")
async def chat_stream(request: Request, body: ChatRequest):
    """Token-level SSE stream.

    stream_mode="messages" yields (chunk, metadata) per LLM token. The
    metadata carries langgraph_node, so tokens produced inside a tool-calling
    turn can be filtered out of the user-visible stream if desired.
    """
    thread_id = body.thread_id or str(uuid.uuid4())
    graph = request.app.state.graph

    async def events() -> AsyncIterator[bytes]:
        yield f"data: {json.dumps({'thread_id': thread_id})}\n\n".encode()
        try:
            async for chunk, _meta in graph.astream(
                {
                    "messages": [HumanMessage(content=body.user_prompt)],
                    "tool_iterations": 0,
                },
                _config(thread_id),
                stream_mode="messages",
            ):
                # A client that disconnects mid-run should not keep burning
                # tokens; raising here cancels the graph task.
                if await request.is_disconnected():
                    break
                if chunk.content:
                    payload = json.dumps({"delta": chunk.content}, ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode()
        except Exception as exc:  # surfaced to the client, logged by the ASGI server
            payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
            yield f"data: {payload}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/threads/{thread_id}", response_model=StateResponse)
async def get_thread(request: Request, thread_id: str):
    """Read a thread's persisted state.

    NOTE: in a real deployment, verify the caller owns this thread_id before
    returning it -- thread ids are guessable identifiers, not credentials.
    """
    snapshot = await request.app.state.graph.aget_state(_config(thread_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="thread not found")
    return StateResponse(
        thread_id=thread_id,
        messages=[
            {"type": m.type, "content": m.content} for m in snapshot.values["messages"]
        ],
        next=list(snapshot.next),
    )
