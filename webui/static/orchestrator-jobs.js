/* Pure job rules for the phase orchestrator chat: terminal statuses, job keys, the automatic
   continuation queue and the job card. No DOM and no I/O, so it also runs under node for the tests.
   Load it before orchestrator-reducer.js, which re-exports these functions. */
(function (root) {
  "use strict";

  // Chat managers end with completed/failed/stopped; CLI tasks end with succeeded/succeeded_with_warnings/failed.
  const TERMINAL_JOB_STATUSES = [
    "completed", "done", "succeeded", "succeeded_with_warnings",
    "failed", "stopped", "cancelled", "canceled", "error", "interrupted",
  ];
  const MAX_DETAIL_CHARS = 300;

  function isTerminalJobStatus(status) {
    return TERMINAL_JOB_STATUSES.indexOf(String(status || "").toLowerCase()) !== -1;
  }

  // Identifies one run of a job: its status URL plus when that run started.
  function jobKey(job, record, fallback) {
    const data = record || {};
    return `${job.url}@${data.started_at || data.created_at || data.id || fallback || ""}`;
  }

  function continuationText(job, record) {
    const data = record || {};
    let detail = String(data.error || data.message || "no details").replace(/[.\s]+$/, "");
    if (detail.length > MAX_DETAIL_CHARS) detail = `${detail.slice(0, MAX_DETAIL_CHARS - 1)}…`;
    return `[auto] The job "${job.kind}" finished with status ${data.status}: ${detail}. Review the result and tell me what's next.`;
  }

  function initialContinuations(sent) {
    return { sent: Array.isArray(sent) ? sent.slice() : [], queue: [] };
  }

  // One automatic continuation per job key; queued while a run is active. `turn` is whatever the
  // controller sends later: the continuation text with the ids the server deduplicates by.
  function claimContinuation(cont, key, turn, running) {
    if (cont.sent.indexOf(key) !== -1) return { cont, post: null };
    const sent = cont.sent.concat([key]);
    if (running) return { cont: { sent, queue: cont.queue.concat([turn]) }, post: null };
    return { cont: { sent, queue: cont.queue }, post: turn };
  }

  function takeQueued(cont) {
    if (!cont.queue.length) return { cont, post: null };
    return { cont: { sent: cont.sent, queue: cont.queue.slice(1) }, post: cont.queue[0] };
  }

  // A continuation the server refused while busy (409) goes back to the front of the queue.
  function requeue(cont, turn) {
    return { sent: cont.sent, queue: [turn].concat(cont.queue) };
  }

  // What the job card shows, for chat-manager jobs and CLI tasks alike.
  function jobCardModel(item, entry) {
    const record = (entry && entry.record) || {};
    const total = Number(record.total || 0);
    const completed = Number(record.completed || 0);
    return {
      label: record.message || (item.data && item.data.message) || `Job "${item.job.kind}"`,
      meta: total ? `${completed} / ${total}` : (record.status || record.phase || "starting"),
      percent: total ? Math.max(2, Math.min(100, Math.round(completed * 100 / total))) : 3,
      finished: Boolean(entry && (entry.terminal || entry.gone)),
    };
  }

  const api = {
    isTerminalJobStatus, jobKey, continuationText, initialContinuations, claimContinuation,
    takeQueued, requeue, jobCardModel,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) {
    root.OrchestratorChat = root.OrchestratorChat || {};
    root.OrchestratorChat.jobs = api;
  }
})(typeof window !== "undefined" ? window : null);
