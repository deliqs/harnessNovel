/* Pure state for the phase orchestrator chat: an AG-UI event reducer and the history loader. It
   re-exports the job rules of orchestrator-jobs.js. No DOM and no I/O, so it also runs under node. */
(function (root) {
  "use strict";

  const AUTO_TEXT = "Job finished — asked the orchestrator to review";

  function initialState() {
    return { items: [], running: false, openTextId: null, openReasoningId: null };
  }

  function indexOf(items, id) {
    return items.findIndex((item) => item.id === id);
  }

  function withItems(state, items) {
    return Object.assign({}, state, { items });
  }

  function upsert(state, item) {
    const items = state.items.slice();
    const index = indexOf(items, item.id);
    if (index === -1) items.push(item);
    else items[index] = Object.assign({}, items[index], item);
    return withItems(state, items);
  }

  function patch(state, id, change) {
    const index = indexOf(state.items, id);
    if (index === -1) return state;
    const items = state.items.slice();
    items[index] = Object.assign({}, items[index], change);
    return withItems(state, items);
  }

  // Text arrives in deltas; an item only appears once it has content, so the empty
  // TEXT_MESSAGE_START/END pair before every tool call leaves no trace.
  function appendText(state, kind, id, delta) {
    if (!delta || !id) return state;
    const index = indexOf(state.items, id);
    if (index === -1) return upsert(state, { kind, id, text: delta });
    return patch(state, id, { text: state.items[index].text + delta });
  }

  function parseJson(text) {
    try {
      const value = JSON.parse(text);
      return value && typeof value === "object" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function jobOf(data) {
    if (!data || data.status !== "started" || !data.job || !data.job.url) return null;
    return data.job;
  }

  // Results are JSON ({status, message, job?}) except denials, which are the plain-text reason;
  // so a plain-text result of a call that needed approval means the call was denied.
  function toolResult(state, toolCallId, content) {
    const data = parseJson(content);
    let next = state;
    if (indexOf(next.items, toolCallId) === -1) next = toolStart(next, toolCallId, "tool", "");
    const approval = next.items.find((item) => item.kind === "approval" && item.toolCallId === toolCallId);
    const known = approval && (approval.status === "approved" || approval.status === "denied");
    const answer = known ? approval.status : data ? "approved" : "denied";
    next = patch(next, toolCallId, { result: String(content ?? ""), data, job: jobOf(data), pending: false, awaiting: false, denied: Boolean(approval) && answer === "denied" });
    return approval ? patch(next, approval.id, { status: answer, sent: true }) : next;
  }

  function toolStart(state, id, name, args) {
    return upsert(state, { kind: "tool", id, name: name || "tool", args: args || "", result: null, data: null, job: null, pending: true });
  }

  function toolArgs(state, id, delta) {
    const next = indexOf(state.items, id) === -1 ? toolStart(state, id, "tool", "") : state;
    const item = next.items[indexOf(next.items, id)];
    return patch(next, id, { args: item.args + (delta || "") });
  }

  // A tool card waits for approval ("awaiting") until the author answers it.
  function approvals(state, interrupts, status) {
    return (interrupts || []).reduce((next, interrupt) => patch(upsert(next, {
      kind: "approval", id: interrupt.id, toolCallId: interrupt.toolCallId || "",
      message: interrupt.message || "Approve this action?", status, sent: status !== "pending",
    }), interrupt.toolCallId, { awaiting: status === "pending" }), state);
  }

  function answer(state, action) {
    const approval = state.items.find((item) => item.id === action.id) || {};
    const next = patch(state, action.id, { status: action.approved ? "approved" : "denied", reason: action.reason || "" });
    return patch(next, approval.toolCallId, { awaiting: false });
  }

  // A recorded approval decision restores the outcome and reason a live answer showed.
  function decision(state, content) {
    const found = state.items.find((item) => item.kind === "approval" && item.toolCallId === content.toolCallId);
    const outcome = { status: content.approved ? "approved" : "denied", reason: content.reason || "", sent: true };
    const next = found ? patch(state, found.id, outcome) : upsert(state, Object.assign({
      kind: "approval", id: `int-${content.toolCallId}`, toolCallId: content.toolCallId, message: "Approve this action?",
    }, outcome));
    return patch(next, content.toolCallId, { awaiting: false, denied: !content.approved });
  }

  function notice(state, id, activityType, text) {
    return upsert(state, { kind: "activity", id: id || `activity-${state.items.length}`, activityType, text });
  }

  // An automatic post-job turn shows as a short line, not as a message from the author. It names
  // the job's tool call, so a reload knows which job it continued.
  function autoTurn(state, id, ref) {
    const job = ref || {};
    return upsert(state, { kind: "activity", id, activityType: "auto_continue", text: AUTO_TEXT, auto: true, toolCallId: job.toolCallId || null, jobKey: job.jobKey || null });
  }

  function activityText(content) {
    if (!content || typeof content !== "object") return String(content ?? "");
    return String(content.message || content.text || content.summary || JSON.stringify(content));
  }

  function runFinished(state, event) {
    const next = Object.assign({}, state, { running: false, openTextId: null, openReasoningId: null });
    const outcome = event.outcome || {};
    return outcome.type === "interrupt" ? approvals(next, outcome.interrupts, "pending") : next;
  }

  // A stop (code "stopped") shows as the same neutral notice the transcript records for it.
  function runError(state, event) {
    const next = Object.assign({}, state, { running: false, openTextId: null, openReasoningId: null });
    return notice(next, null, event.code === "stopped" ? "stopped" : "run_error", event.message || "The run failed.");
  }

  function reasoningStart(state, event) {
    return Object.assign({}, state, { openReasoningId: event.messageId || `reasoning-${state.items.length}` });
  }

  function reasoningContent(state, event) {
    const id = event.messageId || state.openReasoningId || `reasoning-${state.items.length}`;
    return appendText(Object.assign({}, state, { openReasoningId: id }), "reasoning", id, event.delta);
  }

  // Live until the block ends or the answer starts, which is before the run finishes.
  function endReasoning(state) {
    return Object.assign({}, state, { openReasoningId: null });
  }

  function groupedCount(count) {
    return String(count).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function reasoningTail(text) {
    const line = String(text || "").replace(/\s+/g, " ").trim();
    if (line.length <= 80) return line;
    return `…${line.slice(-79)}`;
  }

  function reasoningHeader(text, streaming) {
    if (!streaming) return { label: "Reasoning", tail: "" };
    const body = String(text || "");
    const noun = body.length === 1 ? "character" : "characters";
    return { label: `Reasoning… ${groupedCount(body.length)} ${noun}`, tail: reasoningTail(body) };
  }

  function textContent(state, event) {
    const id = event.messageId || state.openTextId;
    return appendText(Object.assign({}, state, { openTextId: id }), "assistant", id, event.delta);
  }

  const EVENT_HANDLERS = {
    RUN_STARTED: (state) => Object.assign({}, state, { running: true }),
    RUN_FINISHED: runFinished,
    RUN_ERROR: runError,
    TEXT_MESSAGE_START: (state, event) => Object.assign({}, endReasoning(state), { openTextId: event.messageId }),
    TEXT_MESSAGE_CONTENT: textContent,
    TEXT_MESSAGE_CHUNK: textContent,
    TEXT_MESSAGE_END: (state) => Object.assign({}, state, { openTextId: null }),
    TOOL_CALL_START: (state, event) => toolStart(endReasoning(state), event.toolCallId, event.toolCallName, ""),
    TOOL_CALL_ARGS: (state, event) => toolArgs(state, event.toolCallId, event.delta),
    TOOL_CALL_RESULT: (state, event) => toolResult(state, event.toolCallId, event.content),
    REASONING_MESSAGE_START: reasoningStart,
    REASONING_MESSAGE_CONTENT: reasoningContent,
    REASONING_MESSAGE_CHUNK: reasoningContent,
    REASONING_MESSAGE_END: endReasoning,
    REASONING_END: endReasoning,
    THINKING_TEXT_MESSAGE_START: reasoningStart,
    THINKING_TEXT_MESSAGE_CONTENT: reasoningContent,
    THINKING_TEXT_MESSAGE_END: endReasoning,
    ACTIVITY_SNAPSHOT: (state, event) => notice(state, event.messageId, event.activityType, activityText(event.content)),
    "local.user": (state, action) => (action.auto ? autoTurn(state, action.id, action.auto) : upsert(state, { kind: "user", id: action.id, text: action.text })),
    "local.remove": (state, action) => withItems(state, state.items.filter((item) => item.id !== action.id)),
    "local.notice": (state, action) => notice(state, action.id, action.activityType || "notice", action.text),
    "local.answer": answer,
    "local.resumeSent": (state, action) => action.ids.reduce((next, id) => patch(next, id, { sent: true }), state),
    "local.history": (state, action) => fromHistory(action.history),
  };

  // REASONING_ENCRYPTED_VALUE, CUSTOM and any other event type leave the state unchanged.
  function reduce(state, event) {
    const handler = event && EVENT_HANDLERS[event.type];
    return handler ? handler(state, event) : state;
  }

  function contentText(content) {
    if (Array.isArray(content)) return content.map((part) => (part && part.text) || "").join("");
    return String(content ?? "");
  }

  function historyAssistant(state, message) {
    let next = message.content ? upsert(state, { kind: "assistant", id: message.id, text: contentText(message.content) }) : state;
    (message.toolCalls || []).forEach((call) => {
      const fn = call.function || {};
      next = toolStart(next, call.id, fn.name, fn.arguments);
    });
    return next;
  }

  function historyActivity(state, message) {
    const content = message.content || {};
    if (message.activityType === "interrupt") return approvals(state, content.interrupts, "resolved");
    if (message.activityType === "auto_continue") return autoTurn(state, message.id, content);
    if (message.activityType === "approval_decision") return decision(state, content);
    return notice(state, message.id, message.activityType, activityText(content));
  }

  const HISTORY_HANDLERS = {
    user: (state, message) => {
      const text = contentText(message.content);
      return text.startsWith("[auto]") ? autoTurn(state, message.id) : upsert(state, { kind: "user", id: message.id, text });
    },
    assistant: historyAssistant,
    tool: (state, message) => toolResult(state, message.toolCallId, message.content),
    activity: historyActivity,
    reasoning: (state, message) => upsert(state, { kind: "reasoning", id: message.id, text: contentText(message.content) }),
  };

  // A restored job is settled (never polled, never auto-continued) when a stored automatic
  // continuation names its tool call, or when a later job reuses its status URL.
  function settleRestoredJobs(state) {
    let next = state;
    state.items.forEach((item, index) => {
      if (item.kind !== "tool" || !item.job) return;
      const later = state.items.slice(index + 1);
      const superseded = later.some((other) => other.kind === "tool" && other.job && other.job.url === item.job.url);
      const continued = state.items.some((other) => other.auto && other.toolCallId === item.id);
      if (superseded || continued) next = patch(next, item.id, { jobSettled: true });
    });
    return next;
  }

  // While a run is active only the transcript before it is rendered; the /stream replay adds the rest.
  function fromHistory(history) {
    const data = history || {};
    const all = Array.isArray(data.messages) ? data.messages : [];
    const offset = Number.isInteger(data.run_offset) ? data.run_offset : all.length;
    const messages = data.running ? all.slice(0, offset) : all;
    let state = messages.reduce((next, message) => {
      const handler = message && HISTORY_HANDLERS[message.role];
      return handler ? handler(next, message) : next;
    }, initialState());
    state = approvals(settleRestoredJobs(state), data.pending_interrupts, "pending");
    return Object.assign({}, state, { running: Boolean(data.running) });
  }

  // The answers to send as one resume run, once every open approval has been answered.
  function pendingResume(state) {
    const open = state.items.filter((item) => item.kind === "approval" && !item.sent);
    if (!open.length || open.some((item) => item.status === "pending")) return [];
    return open.map((item) => ({
      interruptId: item.id,
      status: "resolved",
      payload: item.status === "approved" ? { approved: true } : Object.assign({ approved: false }, item.reason ? { reason: item.reason } : {}),
    }));
  }

  const READ_ONLY_TOOL = /^(list_artifacts|read_artifact|job_status)$|_(overview|status|status_summary)$/;

  // A "done" result of a tool that is not read-only may have changed what the step panel shows,
  // such as the system-panel mode, so the host refreshes it.
  function refreshesHost(item) {
    return Boolean(item && item.data && item.data.status === "done" && !READ_ONLY_TOOL.test(item.name || ""));
  }

  function trackableJobs(state) {
    return state.items.filter((item) => item.kind === "tool" && item.job && !item.jobSettled);
  }

  const jobs = typeof module !== "undefined" && module.exports
    ? require("./orchestrator-jobs.js")
    : (root && root.OrchestratorChat && root.OrchestratorChat.jobs) || {};
  const api = Object.assign({ initialState, reduce, fromHistory, parseJson, pendingResume, trackableJobs, refreshesHost, reasoningHeader, AUTO_TEXT }, jobs);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) {
    root.OrchestratorChat = root.OrchestratorChat || {};
    root.OrchestratorChat.state = api;
  }
})(typeof window !== "undefined" ? window : null);
