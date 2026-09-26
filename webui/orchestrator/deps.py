"""Contracts shared by the orchestrator routes, runs and tools.

`OrchestratorDeps` is what every tool receives as `ctx.deps`. `JobRef` is what a tool returns
inside its result when it starts a long job, so the UI can poll the existing `/job` routes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import quote

PHASES = ("world", "design", "stage", "arcs", "chapters", "draft")
"""The wizard steps that each get one orchestrator thread per workspace."""

JobKind = Literal["world", "design", "arcs", "chapters", "drafts", "task"]

JOB_URLS = {
    "world": "/api/workspaces/{workspace}/world-knowledge/job",
    "design": "/api/workspaces/{workspace}/design/{scope}/job",
    "arcs": "/api/workspaces/{workspace}/arcs/{volume}/job",
    "chapters": "/api/workspaces/{workspace}/chapters/{volume}/{arc}/job",
    "drafts": "/api/workspaces/{workspace}/drafts/{volume}/{arc}/job",
    "task": "/api/tasks/{task_id}",
}
"""The existing status route each job kind maps to. `scope` is `concept` or `stage`.

Each route returns a JSON object with a `status` field. A client polls until it is terminal:

- world, design, arcs, chapters, drafts (the chat managers' `job_status`): `running`,
  `pausing`, `paused` and `stopping` while the job lives; `completed`, `failed` or `stopped`
  when it ends. `idle` means the manager holds no job for that key, as after a restart, and is
  terminal too.
- task (`TaskManager`, `/api/tasks/{task_id}`): `queued` and `running` while the task lives;
  `succeeded`, `succeeded_with_warnings` or `failed` when it ends. A task that was running when
  the server restarted reads `failed`.
"""

JOB_LOCATORS = {
    "world": (),
    "design": ("scope",),
    "arcs": ("volume",),
    "chapters": ("volume", "arc"),
    "drafts": ("volume", "arc"),
    "task": ("task_id",),
}
"""The `JobRef` fields each job kind needs to find its status route."""

DESIGN_SCOPES = ("concept", "stage")


@dataclass
class OrchestratorDeps:
    """Per-run dependencies. Tools reach the managers through `runtime`, never via `webui.app`.

    `ui_state` is the AG-UI `state` the client sent with the run. The selected volume and arc
    travel there as `{"volume": int, "arc": int}`; tools use them as argument defaults.
    """

    runtime: Any
    workspace: str
    phase: str
    ui_state: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> dict[str, Any]:
        """The AG-UI adapter's `StateHandler` view of `ui_state`."""
        return self.ui_state

    @state.setter
    def state(self, value: Optional[dict[str, Any]]) -> None:
        self.ui_state = dict(value or {})


@dataclass
class JobRef:
    """A reference to a long job a tool started; the UI polls `url()` until it finishes."""

    kind: JobKind
    workspace: str
    scope: Optional[str] = None
    volume: Optional[int] = None
    arc: Optional[int] = None
    task_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in JOB_LOCATORS:
            raise ValueError(f"Unknown job kind: {self.kind}.")
        missing = [name for name in JOB_LOCATORS[self.kind] if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(f"A {self.kind} job needs {', '.join(missing)}.")
        if self.kind == "design" and self.scope not in DESIGN_SCOPES:
            raise ValueError("A design job scope must be concept or stage.")

    def to_dict(self) -> dict[str, Any]:
        """The frozen wire shape: `kind` and `workspace`, plus whichever locators are set."""
        data = {"kind": self.kind, "workspace": self.workspace}
        optional = {"scope": self.scope, "volume": self.volume, "arc": self.arc, "task_id": self.task_id}
        data.update({key: value for key, value in optional.items() if value is not None})
        return data

    def url(self) -> str:
        """The existing status route for this job, from `JOB_URLS`, with each part URL-quoted."""
        parts = {key: quote(str(value), safe="") for key, value in self.to_dict().items()}
        return JOB_URLS[self.kind].format(**parts)
