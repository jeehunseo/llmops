"""Graph topology.

build_graph() deliberately returns an *uncompiled* StateGraph. The caller
supplies the checkpointer, which is the one thing that genuinely differs
between a notebook (MemorySaver), a test (none) and the API (Postgres).
"""

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.nodes import call_model, should_continue
from agent.state import AgentState
from agent.tools import TOOLS


def build_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("agent", call_model)
    builder.add_node("tools", ToolNode(TOOLS))

    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "end": END},
    )
    builder.add_edge("tools", "agent")

    return builder
