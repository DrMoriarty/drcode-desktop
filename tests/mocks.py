"""Reusable model mocks for the GUI shell tests.

The minimal port of ``drcode/tests/network_mocks.py`` (its scripted
``FunctionModel`` layer only — the GUI tests need the *reaction*
semantics, not the transport): each model request consumes the next
scripted step, records its user-prompt text and never touches HTTP.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


def text_response(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


def tool_response(
    tool: str, args: dict[str, Any], call_id: str = "c1"
) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id=call_id)]
    )


def last_user_text(messages: list[ModelMessage]) -> str:
    """Concatenated user-prompt text of a request — lets tests assert
    on what the model actually received."""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    parts.append(part.content)
    return "\n".join(parts)


class ScriptedState:
    """A queue of model responses consumed in request order; every
    request records its user-prompt text in `calls`."""

    def __init__(self) -> None:
        self.steps: list[ModelResponse | Exception] = []
        self.calls: list[str] = []

    def fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(last_user_text(messages))
        if not self.steps:
            raise AssertionError("scripted model queue is empty")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def stream_fn(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        """Streamed variant: replays the scripted response as deltas."""
        response = self.fn(messages, info)
        for part in response.parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                args = (
                    part.args
                    if isinstance(part.args, str)
                    else json.dumps(part.args or {})
                )
                yield {
                    0: DeltaToolCall(
                        name=part.tool_name,
                        json_args=args,
                        tool_call_id=part.tool_call_id,
                    )
                }

    def model(self) -> FunctionModel:
        return FunctionModel(self.fn, stream_function=self.stream_fn)


async def wait_until(
    pred: Callable[[], bool], timeout: float = 30.0, what: str = "condition"
) -> None:
    """Poll `pred` (yielding to the event loop) until it holds or the
    deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def mock_env(monkeypatch: Any) -> None:
    """Point the config at a model that never answers (the run path is
    monkeypatched to the scripted model anyway — the env only makes
    `is_ready` True so the pipeline promotes tasks)."""
    monkeypatch.setenv("DRCODE_MODEL", "mock-model")
    monkeypatch.setenv("DRCODE_API_KEY", "mock-key")
    monkeypatch.setenv("DRCODE_BASE_URL", "http://127.0.0.1:1/v1")
    # Belt-and-braces: no real model file/config may leak in.
    monkeypatch.delenv("DRCODE_TASK_ID", raising=False)


def scripted_model(monkeypatch: Any) -> ScriptedState:
    """Replace the core's model construction with a scripted FunctionModel."""
    from drcode import agent_model

    state = ScriptedState()
    monkeypatch.setattr(agent_model, "build_model", lambda cfg: state.model())
    return state
