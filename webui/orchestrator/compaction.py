"""History compaction for orchestrator threads, so a thread can run indefinitely.

Old tool results are cleared first; only if the history is still over target are older turns
summarised. The target is small because OrcaBonsai prefills at about 380 tokens per second.
"""

from __future__ import annotations

import os

from pydantic_ai_harness.compaction import ClearToolResults, SummarizingCompaction, TieredCompaction

from webui.orchestrator.model import THINK_OFF

DEFAULT_TARGET_TOKENS = 10000
TARGET_ENV = "HARNESS_NOVEL_ORCHESTRATOR_COMPACT_TARGET"


def target_tokens() -> int:
    """The compaction target, overridable through the environment to force compaction in tests."""
    raw = os.environ.get(TARGET_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TARGET_TOKENS
    return value if value > 0 else DEFAULT_TARGET_TOKENS


def compaction_capability(model) -> TieredCompaction:
    """Clear old tool results, then summarise older turns with reasoning off."""
    return TieredCompaction(
        tiers=[
            # The tiers' own triggers are bypassed; TieredCompaction decides when each runs.
            ClearToolResults(max_tokens=1, keep_pairs=1),
            # Not keeping the first user message: the next run merges it with the summary request
            # before it, so keeping it would keep every earlier summary too, and the history
            # would never get back under target.
            SummarizingCompaction(
                model=model, model_settings=THINK_OFF, max_tokens=1, keep_messages=4,
                preserve_first_user_message=False,
            ),
        ],
        target_tokens=target_tokens(),
    )
