"""Provider-independent contracts and public types."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def new_id() -> str:
    return str(uuid4())


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    label: str
    provider: Literal["ollama", "openai", "gemini", "demo"]
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    tool_calling: bool = False
    paid: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=32)
    timeout_seconds: float = Field(default=60, ge=1, le=300)
    context_chars: int = Field(default=24000, ge=1000, le=200000)


class Event(BaseModel):
    schema_version: int = 1
    type: str
    session_id: str
    turn_id: str | None = None
    generation_id: str | None = None
    config_revision: int
    sequence: int
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    data: dict[str, Any] = Field(default_factory=dict)


class RuntimeEvent(BaseModel):
    type: Literal["text", "tool_started", "tool_completed", "tool_failed"]
    data: dict[str, Any]


class TurnRequest(BaseModel):
    session_id: str
    generation_id: str
    text: str
    context: list[dict[str, str]]


class AgentRuntime(Protocol):
    def run(self, request: TurnRequest) -> AsyncIterator[RuntimeEvent]: ...


class AudioBackend(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...
    async def synthesize(self, text: str) -> bytes: ...
    def detector(self) -> Any: ...


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status
        super().__init__(message)
