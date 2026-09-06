# scaffold — LangGraph agent: notebook development → FastAPI deployment

A minimal but complete example of the structure that keeps notebook-driven
LangGraph development from turning into copy-paste at deployment time.

**The one rule: the notebook is a REPL, not a source file.** All logic lives in
`src/agent/`. `notebooks/explore.ipynb` imports it and only runs, observes and
debugs. `app/main.py` imports the same modules and serves them over HTTP.

## Layout

```
src/agent/
  settings.py       env-driven config; nothing else reads os.environ
  state.py          AgentState — what flows between nodes
  tools.py          tools, independently importable and testable
  nodes.py          node functions: (state) -> partial update, no globals
  graph.py          build_graph() -> uncompiled StateGraph
  checkpointer.py   the dev/prod seam: MemorySaver vs AsyncPostgresSaver
  otel.py           Langfuse tracing over OTLP
app/
  main.py           lifespan, routes, SSE streaming, thread endpoints
  schemas.py        request/response models
notebooks/explore.ipynb
tests/test_graph.py
```

### Why `build_graph()` returns an *uncompiled* builder

Compiling requires a checkpointer, and the checkpointer is the one thing that
genuinely differs across environments. Returning the builder lets each caller
supply its own:

| caller | checkpointer |
|---|---|
| notebook | `MemorySaver()` |
| pytest | `MemorySaver()` per test |
| FastAPI | `AsyncPostgresSaver` opened in `lifespan` |

The graph definition itself is identical everywhere — that is the whole point.

## Setup

```bash
uv sync
cp .env.example .env
uv run python -m ipykernel install --user --name scaffold
```

The agent talks to an OpenAI-compatible endpoint. By default that is the
Triton-backed `inference-api` from `../langfuseproject/2.inference`:

```bash
cd ../langfuseproject/2.inference && docker compose up -d
```

Point `INFERENCE_BASE_URL` elsewhere to use any other OpenAI-compatible server.

## Develop

```bash
uv run jupyter lab notebooks/explore.ipynb
```

`%autoreload 2` is set in the first cell, so edits to `src/agent/*.py` take
effect on the next cell run without restarting the kernel. That removes the
incentive to write logic in cells.

The notebook walks through: graph visualization, calling nodes standalone,
`stream_mode="updates"` for per-node state transitions, token streaming, and
`get_state_history()` for checkpoint replay.

## Test

```bash
uv run pytest -q
```

Tests compile the same builder the API uses, with the model swapped for a
scripted fake — so routing, the tool loop, the iteration budget and thread
persistence are covered without a live inference server.

## Run locally

```bash
uv run uvicorn app.main:app --reload --port 8000
```

| endpoint | purpose |
|---|---|
| `POST /v1/chat` | single response; returns `thread_id` |
| `POST /v1/chat/stream` | SSE token stream |
| `GET /v1/threads/{id}` | persisted state of one thread |
| `GET /health` | liveness |

```bash
curl -s localhost:8000/v1/chat -H 'content-type: application/json' -d '{"user_prompt":"2 + 40?"}'
```

## Deploy

```bash
docker compose up -d --build
```

This starts Postgres and the agent on `llmops-net` (create it with
`docker network create llmops-net` if it does not exist yet), reachable on
`localhost:3100`. Setting `POSTGRES_URL` is what flips the checkpointer — no
code path changes.

## dev vs prod

| | dev | prod |
|---|---|---|
| checkpointer | `MemorySaver` | `AsyncPostgresSaver` |
| compile | every cell run | once, in `lifespan` |
| `thread_id` | typed by hand | from the request; **ownership must be verified** |
| streaming | cell output | SSE, cancelled on client disconnect |
| tracing | off unless keys set | Langfuse via OTLP |
| loop guards | — | `recursion_limit=25` + `max_tool_iterations` |
| schema DDL | `setup()` on start | separate migration job before rolling pods |

## Known gaps (deliberate)

Left out so the structure stays readable; each is a real requirement in
production:

- **Thread authorization.** `GET /v1/threads/{id}` returns any thread to any
  caller. Thread ids are identifiers, not credentials.
- **Checkpoint schema migration.** Changing `AgentState` can break threads that
  are mid-flight across a rolling deploy. Version the graph id, or drain first.
- **Long-running runs.** Everything here is request-scoped. Runs that outlive an
  HTTP request need a queue and a worker.
- **Human-in-the-loop.** No `interrupt_before` on the tool node; add one before
  wiring up any tool with side effects.

If those become the bulk of the work, that is the signal to move to LangGraph
Server — `langgraph.json` is already present and points at `build_graph`, so
`langgraph dev` / `langgraph build` work against the same code.
