"""Gives orchestrator chat runs priority on the shared model over in-process job threads.

The local model serves one request at a time, first in first out. While any orchestrator run
is active, `LLMProvider` requests from background job threads in this process wait before
sending, so the chat's next request is never queued behind a job's. A job request already in
flight is not preempted. CLI subprocess tasks are separate processes and are not gated.

Invariant: nothing awaited inside an orchestrator run (a tool, a capability hook) may call
`LLMProvider`. The run holds priority while it waits, so that call would wait on the run itself
and hang until the author presses Stop. Orchestrator tools only start jobs in threads or read
files; the run's own model calls go through pydantic-ai, which is not gated.

Starvation: while any chat run is active, every in-process job waits before its next request.
A job therefore makes no progress while the author keeps the chat busy, by design.
"""

import threading
from contextlib import contextmanager

POLL_SECONDS = 0.1


class ModelGate:
    def __init__(self):
        self._condition = threading.Condition()
        self._active_runs = 0

    @contextmanager
    def priority(self):
        """Hold priority for one orchestrator run; released however the run ends."""
        with self._condition:
            self._active_runs += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_runs -= 1
                self._condition.notify_all()

    def busy(self):
        with self._condition:
            return self._active_runs > 0

    def wait_until_free(self, cancel_event=None):
        """Block until no orchestrator run is active; False if `cancel_event` is set first.

        The wait wakes every POLL_SECONDS to check `cancel_event`, so a job stopped while it
        waits here stops promptly.
        """
        with self._condition:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                if self._active_runs == 0:
                    return True
                self._condition.wait(POLL_SECONDS)


model_gate = ModelGate()
