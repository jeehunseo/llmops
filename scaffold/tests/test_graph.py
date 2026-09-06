"""Tests run against the same builder the API compiles.

The model is replaced with a scripted fake, so the graph's routing is tested
without a live inference server.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from agent.graph import build_graph
from agent.state import AgentState
from agent.tools import add_numbers, lookup_doc


class FakeModel:
    """Returns the queued responses in order, one per invocation.

    Each response must be a *distinct* message object: add_messages stamps an
    id onto the object it receives, so handing the reducer the same instance
    twice makes it overwrite the earlier entry instead of appending.
    """

    def __init__(self, responses):
        self._responses = list(responses)

    async def ainvoke(self, messages):
        return self._responses.pop(0)


def scripted(*responses):
    """Patch target: one FakeModel shared across every call in a run."""
    model = FakeModel(responses)
    return lambda settings=None: model


def tool_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "add_numbers", "args": {"a": 1, "b": 2}, "id": call_id}],
    )


@pytest.fixture
def compiled():
    return build_graph().compile(checkpointer=MemorySaver())


async def test_tools_are_callable_in_isolation():
    assert await add_numbers.ainvoke({"a": 2, "b": 3}) == 5
    assert "thread_id" in await lookup_doc.ainvoke({"topic": "thread"})


async def test_graph_ends_when_model_emits_no_tool_calls(compiled, monkeypatch):
    monkeypatch.setattr(
        "agent.nodes.build_model", scripted(AIMessage(content="done"))
    )
    result = await compiled.ainvoke(
        {"messages": [HumanMessage(content="hi")], "tool_iterations": 0},
        {"configurable": {"thread_id": "t-end"}},
    )
    assert result["messages"][-1].content == "done"


async def test_graph_routes_through_tool_node(compiled, monkeypatch):
    monkeypatch.setattr(
        "agent.nodes.build_model",
        scripted(tool_call("c1"), AIMessage(content="the sum is 3")),
    )
    result = await compiled.ainvoke(
        {"messages": [HumanMessage(content="1+2?")], "tool_iterations": 0},
        {"configurable": {"thread_id": "t-tools"}},
    )
    types = [m.type for m in result["messages"]]
    assert types == ["human", "ai", "tool", "ai"]
    assert result["messages"][2].content == "3.0"  # ToolMessage payload
    assert result["messages"][-1].content == "the sum is 3"


async def test_tool_budget_stops_the_loop(compiled, monkeypatch):
    """A model that always calls tools must not spin until recursion_limit."""
    monkeypatch.setattr(
        "agent.nodes.build_model",
        scripted(*(tool_call(f"c{i}") for i in range(20))),
    )
    result = await compiled.ainvoke(
        {"messages": [HumanMessage(content="loop")], "tool_iterations": 0},
        {"configurable": {"thread_id": "t-budget"}},
    )
    assert "budget exhausted" in result["messages"][-1].content


async def test_thread_state_survives_between_invocations(compiled, monkeypatch):
    config = {"configurable": {"thread_id": "t-memory"}}

    monkeypatch.setattr("agent.nodes.build_model", scripted(AIMessage(content="first")))
    await compiled.ainvoke(
        {"messages": [HumanMessage(content="one")], "tool_iterations": 0}, config
    )

    monkeypatch.setattr("agent.nodes.build_model", scripted(AIMessage(content="second")))
    result = await compiled.ainvoke(
        {"messages": [HumanMessage(content="two")], "tool_iterations": 0}, config
    )
    # Both turns are present: the checkpointer restored the earlier messages.
    assert [m.content for m in result["messages"]] == ["one", "first", "two", "second"]


def test_state_declares_expected_keys():
    assert set(AgentState.__annotations__) == {"messages", "tool_iterations"}
