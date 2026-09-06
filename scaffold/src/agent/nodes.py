"""Node functions.

Each node is an ordinary async function of (state) -> partial state update.
It takes no globals, so it can be called directly from a notebook cell or a
test without building the graph:

    await call_model({"messages": [HumanMessage("hi")], "tool_iterations": 0})
"""

from langchain_core.messages import AIMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.settings import Settings, get_settings
from agent.state import AgentState
from agent.tools import TOOLS

SYSTEM_PROMPT = (
    "You are a concise assistant. Use the provided tools when they can answer "
    "the question more reliably than you can. Do not guess at facts a tool "
    "could confirm. Answer in the language the user used."
)


def build_model(settings: Settings | None = None) -> ChatOpenAI:
    """Chat model bound to the tools, pointed at an OpenAI-compatible endpoint."""
    settings = settings or get_settings()
    llm = ChatOpenAI(
        model=settings.model_name,
        base_url=settings.inference_base_url,
        api_key=settings.inference_api_key,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        timeout=60,
        max_retries=2,
    )
    return llm.bind_tools(TOOLS)


async def call_model(state: AgentState) -> dict:
    """Single LLM turn. May emit tool calls."""
    settings = get_settings()
    iterations = state.get("tool_iterations", 0)

    if iterations >= settings.max_tool_iterations:
        # Budget spent. Return a terminal message instead of letting the
        # agent<->tools cycle run until recursion_limit raises.
        return {
            "messages": [
                AIMessage(
                    content="Tool budget exhausted before reaching an answer. "
                    "Please narrow the question."
                )
            ]
        }

    model = build_model(settings)
    response = await model.ainvoke(
        [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
    )
    return {"messages": [response], "tool_iterations": iterations + 1}


def should_continue(state: AgentState) -> str:
    """Route to the tool node while the model keeps requesting tools."""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "end"
