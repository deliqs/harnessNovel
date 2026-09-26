"""The replayable event buffer behind one orchestrator run."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional


class RunBuffer:
    """The encoded SSE chunks of one run. Any number of readers can replay it from the start."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.finished = False
        self._changed = asyncio.Event()

    def append(self, chunk: str) -> None:
        self.chunks.append(chunk)
        self._wake()

    def finish(self) -> None:
        self.finished = True
        self._wake()

    def _wake(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def replay(self) -> AsyncIterator[str]:
        index = 0
        while True:
            # Taken before yielding, so a chunk appended while a reader is suspended still wakes it.
            changed = self._changed
            while index < len(self.chunks):
                index += 1
                yield self.chunks[index - 1]
            if self.finished:
                return
            await changed.wait()


class ThreadRun:
    """One run of a thread: its buffer, its task, and where its output starts in the transcript."""

    def __init__(self, content_type: str, transcript_offset: int) -> None:
        self.buffer = RunBuffer()
        self.content_type = content_type
        # Transcript messages before this index were written before the run streamed anything.
        self.transcript_offset = transcript_offset
        self.task: Optional[asyncio.Task] = None

    @property
    def active(self) -> bool:
        return not self.buffer.finished
