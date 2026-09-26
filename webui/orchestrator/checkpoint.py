"""Saves a thread's model context right before each model request of a run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestContext


@dataclass
class ContextCheckpoint(AbstractCapability[Any]):
    """Hands the messages about to be sent to `save`.

    At that point the history holds every tool call with its return, so a job a tool just
    started is on disk before the follow-up request, which may wait behind the model.
    """

    save: Callable[[list[ModelMessage]], None]

    async def before_model_request(self, ctx: RunContext[Any], request_context: ModelRequestContext) -> ModelRequestContext:
        self.save(list(request_context.messages))
        return request_context
