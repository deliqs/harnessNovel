# Phase orchestrator chat (AG-UI + pydantic-ai)

Six wizard steps (world, design, stage, arcs, chapters, draft) each get one chat thread with an orchestrator agent.
The agent calls tools, which wrap the existing harness operations, and later subagents. It auto-compacts, so a thread
can run indefinitely. It streams text, reasoning and tool calls, and it asks the author for approval mid-run.

## Decisions (Robin, 2026-09-26)
- The orchestrator chat **replaces** the current chat panels on those six steps. The existing chat pipelines stay and
  become tools.
- **AG-UI over SSE with pydantic-ai and pydantic-ai-harness**, served from the existing FastAPI app. The frontend stays
  vanilla JS with no bundler. CopilotKit is rejected.
- **Long jobs start the job and return quickly.** The chat polls the job, and when it finishes the client sends **one
  automatic continuation turn** per job.
- **One thread per (workspace, phase).** The selected volume/arc travels in AG-UI `state`. Tools take volume/arc as
  arguments, defaulting from that state.
- **Drop Python 3.9.** `python_requires>=3.10`, and the CI matrix becomes 3.10/3.12.
- **The Reference step gets no chat.** Its form stays as it is.
- **No recovery** of old-session items.

## Lead defaults (stated to Robin)
- The frontend uses a hand-written SSE reader, loaded as classic scripts in flat `static/orchestrator-*.js` files.
- The model is `ADAPTIVE_BUILDER_LITE`. There is no separate orchestrator slot in v1.
- Compaction targets about 10K tokens (see spike):
  - tool results are cleared first, then older turns are summarized;
  - reasoning is stripped from history;
  - an env setting can force compaction for testing.
- Destructive tools need approval. That covers resets, clearing a guide or lenses, a world rebuild, and setting
  finalized chapters.
- While a job runs, the composer stays enabled and shows that the turn is queued behind the job, since the model is
  shared and serial. Job controls (pause/resume/stop/continue/prompts) are plain buttons that call the existing routes
  directly, never through the agent.
- Not in v1: `ask_author` and `ctx.emit`. A question in plain text ends the turn, and polling covers progress. A critic
  subagent comes later as a background job (U6).

## Contracts, frozen in U1
- **`JobRef`** is `{kind: world|design|arcs|chapters|drafts|task, workspace, scope?, volume?, arc?, task_id?}`. U2 tools
  return it, and U3 maps it to the existing `/job` or `/api/tasks/{id}` URL.
- **Deps** is `{runtime (app.state.runtime), workspace, phase, ui_state}`. Tools never import `webui.app`.
- **Registry**: `webui/orchestrator/registry.py` imports one module per phase, `tools/<phase>.py`, each exporting
  `TOOLS` and `INSTRUCTIONS`. U1 ships stubs, and U2a/U2b fill only their own.
- **History store**:
  - an append-only **display transcript** (reload shows the full thread);
  - a separate compacted **model context**;
  - both are persisted after every model response and tool return.
- **Routes**: `webui/orchestrator/routes.py` as an APIRouter:
  - `POST /api/workspaces/{name}/orchestrator/{phase}` (AG-UI run);
  - `GET`/`DELETE …/{phase}/history`.

  `app.py` only gets `include_router`, plus hooks in `set_workspace_root` and `delete_workspace`.

## Constraints
- OrcaBonsai has a single cache slot and a single FIFO worker, processes prompts at about 380 tok/s, supports only
  auto/none tool_choice, and has no JSON mode. Therefore:
  - output is plain text;
  - toolsets are 5–10 tools per phase;
  - tool results are capped summaries plus paths, never chapter text.
- Every new file is 300 lines or less (a test enforces this), and complexity stays at 12 or below, checked with
  `uvx ruff check --select C901` at max-complexity 12 on new and changed files.
- `app.py` and `wizard-v0.js` get net-small diffs, and the `wizard-v0.js` diff is net-negative after U5.
- No npm. Don't fork CLI behaviour. Leave the `"openai>=1.0.0"` string in install_requires as it is, since a test
  asserts it; add pydantic-ai as a separate requirement.
- These stay as plain UI next to the chat:
  - lens bar, world chat-guide bar, writing-guide upload;
  - system-panel mode, humanize toggle, volume/arc selectors;
  - continue banners, the Prompt viewer, review refresh, artifact cards, copy-markdown.
- Design attachments go through `/api/uploads`. The agent only sees `{name, upload_id, size}`.
- The design tool gets an explicit mode parameter (`chat|extend|question|critique`) and flags for `use_new_reference`
  and `sync_updated_design`. That way an agent paraphrase can't accidentally trigger the keyword-based extend path.
- The legacy `conversation*.json` files stay as pipeline memory. Clearing a thread does not clear them.

## Spike results (U0: GO)
- Tool calls on OrcaBonsai were 20/20 correct across 6 tools, with thinking both on and off. The real risk is
  **flailing** when no tool fits. Mitigations:
  - `UsageLimits(request_limit=15)`;
  - the instruction "if no tool fits, say so";
  - `ModelRetry` validation;
  - `Literal` argument types;
  - approval on side-effecting and long-job tools.
- **Latency on a cold turn:** 12 s at 7.5K, 33 s at 15K and 59 s at 23K. A warm turn takes about 1.5 s. So the
  **compaction target is about 10K**, with `ClearToolResults` first and `SummarizingCompaction` second.
- **Required settings:**
  - `AGUIAdapter.from_request(..., manage_system_prompt="client")`, with a server-side `new_turn_only` filter and
    `instructions=`. Otherwise the compaction summary is silently deleted.
  - Map frontend tool messages into `DeferredToolResults.calls`.
  - Profile: `openai_chat_send_back_thinking_parts=False`, `supports_multiple_system_messages=False`,
    `strict_tool_definition=False`, `tool_choice_required=False`. Use an `httpx2` client.
  - Thinking is off by default and toggled per turn via `forwardedProps.thinking`.
- **A disconnect cancels the run and persists nothing.**
  - Fix: run each turn as a background asyncio task per thread that writes into an event buffer. SSE subscribes to the
    buffer and replays it on reconnect.
  - Persist the user turn at start, and persist again on success, error and cancel.
- **Pins:**
  - pydantic-ai-slim[openai,ag-ui] 2.51.0 and pydantic-ai-harness 0.36.0;
  - ag-ui-protocol 0.1.22 (capped below 1);
  - openai>=3.19.

  These resolve against fastapi 0.141.1 and starlette 1.7.0.
- Spike code: `scratchpad/spike/{common,app,orchestrator,history_store}.py`.

## Units
- [x] **U0 Spike**: a live pydantic-ai run against OrcaBonsai, in a scratch venv. It adds go/no-go on:
  - tool-call validity;
  - approval resume with server-held history;
  - the role of the compaction summary;
  - latency at 8/16/24K;
  - disconnect persistence;
  - dependency resolution against the installed fastapi/starlette/openai.
- [x] **U1 Backend core**:
  - `webui/orchestrator/` with the model factory, history store, compaction, deps, registry and stubs, and routes;
  - `setup.py` (deps and python_requires), the CI matrix, and the app.py hooks;
  - a line-count test.

  Tests: TestClient plus FunctionModel assert the SSE sequence, including interrupt and resume.
- [x] **U2a Tools: world, design, stage.** Each tool is tested for:
  - a guard violation producing an error string;
  - results capped in length;
  - approval gating;
  - path containment.
- [x] **U2b Tools: arcs, chapters, draft**, with the same tests.
- [x] **U3a Frontend module**: `static/orchestrator-{stream,view,chat}.js` plus CSS. It has:
  - a pure event reducer and a JS-held state that survives `renderActiveStep()` re-renders;
  - the job card and auto-continuation.
- [x] **U3b Integration**: mount the chat on the six steps in `wizard-v0.js` and `index.html`. The old panels stay
  behind the switch until U4 passes.
- [x] **U4 Live E2E** in the browser pane with OrcaBonsai:
  - a tool call;
  - a job whose progress completes and triggers auto-continue;
  - an approval;
  - a reload that restores the thread;
  - a forced compaction that keeps the thread working;
  - a job finishing mid-stream (the re-render hazard).
- [ ] **U5 Remove the old chat panels.** Update the tests that assert the old wiring: test_world_chat_frontend,
  test_design_chat_critique (frontend parts) and test_world_chat_web.
- [ ] **U6 (later)**: a critic subagent as a background job.

## Definition of done
- `.venv/bin/python -m unittest discover -s tests` passes, except the 5 failures that already exist at HEAD:
  - test_packaging_runtime_assets;
  - 3 in test_prompt_trace_privacy;
  - test_story_arc_context average_chars.
- The U4 live checks are recorded below.
- The line-count test and the ruff C901 check pass.

## Review

The work ran on 2026-09-26 using /lead claude, with Opus workers and a co-lead doing three rounds of review.

**Offline:** `python -m unittest discover -s tests` ran 442 tests. The only failures are the 5 that were already
failing before this work. `ruff --select C901` (max 12) is clean on `webui/orchestrator`, `core/model_gate.py` and the
orchestrator tests, and every orchestrator file is 300 lines or fewer.

**Live E2E (U4)** ran in the in-app browser against OrcaBonsai, with model traffic captured through a logging proxy.

First run: 10 checks.
- 7 passed:
  - a tool turn (3.6 s);
  - approve → job → exactly one auto-continue. The reply started 0.39 s after approval, and the job's first model
    call waited until 11 ms after the chat run ended, so the gate works;
  - question mode without an approval card;
  - a reload during and after a run, with no duplicates;
  - 70 re-renders while a job finished, with no duplicate turns;
  - stop (5 ms);
  - chat priority while a job runs;
  - no console errors and no tracebacks.
- 2 were partial and exposed bugs:
  - B1: compaction summaries piled up (`preserve_first_user_message`);
  - B2: denial reasons reached the model unframed.
- A third gap, B3: the orchestrator could not read question or critique results.

Re-check after the fixes: all six points passed.
- B1:
  - 12 turns, 17 requests;
  - never more than one summary;
  - 3 summarization calls in total;
  - recall held after 3 compactions.
- B2: the request body carries "The author denied this call. Their reason: …".
- B3: the model read the result through `job_status.result` in 2 requests.
- Attachments reach the job through server-side tag parsing even when the model drops the id.
- Ctrl/⌘+Enter sends.
- No errors.
- B5, found in the re-check: `markdownPreview` italicised underscores inside snake_case paths. Fixed and tested.

**Residual risks** (unit-tested or accepted, but never exercised live):
- two browser tabs and duplicate continuations. The server dedupe and the node tests cover this.
- several concurrent jobs in one thread, which is covered by node tests only.
- destroying controllers on a workspace switch.
- server restart mid-job.
- the `harnessNovel.legacyChat=1` fallback.
- running with localStorage disabled.
- starvation, which is accepted by design: while any chat run is active, every in-process job waits.
- CLI subprocess tasks (world rebuild, title/synopsis), which are not gated, so the follow-up request can queue
  behind their first model call.

**Out of scope, but it will surface:** on OrcaBonsai, the design pipeline's refine step fails with truncated JSON on
large prompts (a 47K-character prompt). This is a limit of the existing pipeline, not of the chat.

**Open:** U5 (removing the legacy panels and the fallback flag) and U6 (a critic subagent).

**Smoke test of the remaining steps** (stage, arcs, chapters, draft): passed.
- The chat mounts next to each step's own controls.
- Overview and status tools pick up the volume and arc selected in the UI.
- `[humanize: off]` reaches `draft_generate` as `humanize=False`.
- The system-panel mode changes without an approval card.
- Two minor bugs were found, fixed and tested:
  - B6: a control could still show the old value after a tool changed its setting. A `done` result from a tool that
    changes something now refreshes the workspace artifacts.
  - B7: the tag line could leak into the draft message.
