from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    user_prompt: str = Field(min_length=1)
    # One conversation. Omit to start a fresh, unresumable thread.
    thread_id: str | None = None


class ChatResponse(BaseModel):
    thread_id: str
    content: str


class StateResponse(BaseModel):
    thread_id: str
    messages: list[dict]
    next: list[str]
