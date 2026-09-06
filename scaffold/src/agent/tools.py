"""Tools available to the agent.

Kept side-effect free and independently importable, so they can be unit
tested in a notebook cell or pytest without constructing the graph.
"""

from langchain_core.tools import tool

# Stand-in for a real retrieval backend (vector store, search API, ...).
_DOCS: dict[str, str] = {
    "checkpointer": (
        "A checkpointer persists graph state per thread_id after every super-step, "
        "which is what makes interrupt/resume and time travel possible."
    ),
    "thread": (
        "A thread is one conversation. Pass it as "
        'config={"configurable": {"thread_id": "..."}} on every invoke.'
    ),
    "reducer": (
        "A reducer merges a node's returned value into existing state. "
        "Without one, the returned value replaces the field."
    ),
}


@tool
def lookup_doc(topic: str) -> str:
    """Look up an internal document about a LangGraph topic.

    Args:
        topic: The topic to look up, e.g. "checkpointer", "thread", "reducer".
    """
    key = topic.strip().lower()
    for name, body in _DOCS.items():
        if name in key:
            return body
    return f"No document found for {topic!r}. Known topics: {', '.join(_DOCS)}."


@tool
def add_numbers(a: float, b: float) -> float:
    """Add two numbers and return the sum.

    Args:
        a: First number.
        b: Second number.
    """
    return a + b


TOOLS = [lookup_doc, add_numbers]
