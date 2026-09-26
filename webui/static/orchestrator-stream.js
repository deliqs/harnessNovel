/* AG-UI over SSE for the phase orchestrator chat: run, replay and plain JSON requests, plus the
   chat's small localStorage settings. No DOM. */
(function (root) {
  "use strict";

  const BUSY_MESSAGE = "This chat is still answering. Wait for it to finish first.";

  function threadBase(workspace, phase) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/orchestrator/${encodeURIComponent(phase)}`;
  }

  function newId() {
    if (root && root.crypto && typeof root.crypto.randomUUID === "function") return root.crypto.randomUUID();
    return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  // Splits a buffer into complete `data: {json}` frames; the unfinished tail is returned as `rest`.
  function parseFrames(buffer) {
    const normalized = buffer.replace(/\r\n/g, "\n");
    const frames = normalized.split("\n\n");
    const rest = frames.pop();
    const events = [];
    frames.forEach((frame) => {
      const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).replace(/^ /, "")).join("\n");
      if (!data) return;
      try {
        events.push(JSON.parse(data));
      } catch (error) {
        // A malformed frame is skipped rather than ending the stream.
      }
    });
    return { events, rest };
  }

  async function requestError(response) {
    const data = await response.json().catch(() => ({}));
    const fallback = response.status === 409 ? BUSY_MESSAGE : "Request failed. Please retry shortly.";
    const error = new Error(typeof data.detail === "string" ? data.detail : fallback);
    error.status = response.status;
    return error;
  }

  async function readEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const parsed = parseFrames(buffer + decoder.decode(value, { stream: true }));
      buffer = parsed.rest;
      parsed.events.forEach(onEvent);
    }
    parseFrames(`${buffer}${decoder.decode()}\n\n`).events.forEach(onEvent);
  }

  function runInput(options) {
    const body = {
      threadId: `${options.workspace}:${options.phase}`,
      runId: newId(),
      state: options.state || {},
      messages: options.messages || [],
      tools: [],
      context: [],
      forwardedProps: { thinking: Boolean(options.thinking) },
    };
    // An automatic post-job turn names its job ({toolCallId, jobKey}); the server records it as an
    // activity, never lets it deny approvals, and refuses a second one for the same job.
    if (options.autoContinue) body.forwardedProps.autoContinue = options.autoContinue;
    if (options.resume) body.resume = options.resume;
    return body;
  }

  // POSTs one AG-UI run and feeds every event to `onEvent` until the stream ends.
  async function postRun(options, onEvent, signal) {
    const response = await fetch(threadBase(options.workspace, options.phase), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(runInput(options)),
      signal,
    });
    if (!response.ok) throw await requestError(response);
    await readEvents(response, onEvent);
  }

  // Replays the active or last run from its start. Resolves false when the thread has no run.
  async function replayRun(workspace, phase, onEvent, signal) {
    const response = await fetch(`${threadBase(workspace, phase)}/stream`, { headers: { Accept: "text/event-stream" }, signal });
    if (response.status === 404) return false;
    if (!response.ok) throw await requestError(response);
    await readEvents(response, onEvent);
    return true;
  }

  async function requestJson(url, options) {
    const response = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    if (!response.ok) throw await requestError(response);
    return response.json().catch(() => ({}));
  }

  function readStorage(key, fallback) {
    try {
      const value = root.localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch (error) {
      return fallback;
    }
  }

  function writeStorage(key, value) {
    try {
      root.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {
      // Storage can be unavailable (private mode); the setting then lasts for this page only.
    }
  }

  const api = {
    BUSY_MESSAGE, threadBase, newId, parseFrames, postRun, replayRun, readStorage, writeStorage,
    loadHistory: (workspace, phase) => requestJson(`${threadBase(workspace, phase)}/history`),
    clearHistory: (workspace, phase) => requestJson(`${threadBase(workspace, phase)}/history`, { method: "DELETE" }),
    stopRun: (workspace, phase) => requestJson(`${threadBase(workspace, phase)}/stop`, { method: "POST" }),
    pollJob: (url) => requestJson(url),
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) {
    root.OrchestratorChat = root.OrchestratorChat || {};
    root.OrchestratorChat.stream = api;
  }
})(typeof window !== "undefined" ? window : null);
