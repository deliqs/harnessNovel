"""Builds the orchestrator agent for one phase from the shared and per-phase tool modules."""

from __future__ import annotations

from pydantic_ai import Agent, DeferredToolRequests

from webui.orchestrator.deps import OrchestratorDeps
from webui.orchestrator.tools import arcs, chapters, design, draft, stage, world
from webui.orchestrator.tools.shared import SHARED_TOOLS

PHASE_MODULES = {
    "world": world,
    "design": design,
    "stage": stage,
    "arcs": arcs,
    "chapters": chapters,
    "draft": draft,
}

SHARED_INSTRUCTIONS = (
    "You are the orchestrator for one step of a novel-writing workbench. You act through tools "
    "and answer the author briefly in plain text. Long jobs start in the background; tell the "
    "author a job has started rather than waiting for it. Never poll or wait on a job you "
    "started, or on one that is running: end your turn and wait for the automatic message. "
    "When a message says a job finished, "
    "call job_status for that job first: its result field holds the job's answer or outcome. "
    "Stay inside this step. The other steps have their own threads: do not plan their content "
    "or offer to move to them or run them. When this step's work is done, say so, or say what "
    "it still needs."
)
TOOL_RULE = (
    "If no tool can do what the author asks, say so instead of calling other tools. "
    "Tool results are summaries; never ask a tool for full chapter text."
)


class UnknownPhase(LookupError):
    """The phase is not one of the orchestrator's wizard steps."""


def require_phase(phase: str) -> str:
    if phase not in PHASE_MODULES:
        raise UnknownPhase(phase)
    return phase


def instructions_for(phase: str) -> str:
    return "\n\n".join([SHARED_INSTRUCTIONS, PHASE_MODULES[require_phase(phase)].INSTRUCTIONS, TOOL_RULE])


def build_agent(phase: str, model) -> Agent:
    """The orchestrator agent for `phase`: shared read-only tools plus the phase's own."""
    module = PHASE_MODULES[require_phase(phase)]
    return Agent(
        model,
        deps_type=OrchestratorDeps,
        instructions=instructions_for(phase),
        tools=[*SHARED_TOOLS, *module.TOOLS],
        output_type=[str, DeferredToolRequests],
    )
