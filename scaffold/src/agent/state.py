"""The single source of truth for what flows between nodes."""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Graph state.

    `messages` uses the add_messages reducer: nodes return only the messages
    they produced and the reducer appends them (matching by id, so a node can
    also overwrite an earlier message).

    `tool_iterations` is a plain int, so the last write wins -- each pass
    through the agent node sets the incremented value explicitly.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    tool_iterations: int
