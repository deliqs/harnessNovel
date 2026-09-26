/* Controller of the phase orchestrator chat. One controller per (workspace, phase) lives in a
   module registry, so a wizard re-render re-attaches the DOM while a stream keeps running. */
(function (root) {
  "use strict";

  const chat = root.OrchestratorChat = root.OrchestratorChat || {};
  // The step panel often polls the same job for its banner, so the job card polls less often.
  const POLL_MS = 3000;
  const DUPLICATE = "DUPLICATE_AUTO_CONTINUE";
  const THINK_KEY = "harnessNovel.orchestrator.think";
  const CONTINUED_KEY = "harnessNovel.orchestrator.continued:";
  const QUEUED_TEXT = "Queued behind a running job — the model is shared";
  const CLEAR_CONFIRM = "Clear this chat thread? The orchestrator forgets the conversation. Files on disk stay as they are.";
  const registry = new Map();

  function createController(options) {
    const S = chat.state;
    const IO = chat.stream;
    const { workspace, phase } = options;
    const continuedKey = `${CONTINUED_KEY}${workspace}:${phase}`;
    let hooks = options;
    let state = S.initialState();
    let cont = S.initialContinuations(IO.readStorage(continuedKey, []));
    let thinking = Boolean(IO.readStorage(THINK_KEY, false));
    let draft = "";
    let busy = false;
    let queuedBehindJob = false;
    let holdQueue = false; // after the server refused an automatic turn, wait for the next turn to end
    let status = { text: "", error: false };
    let abort = null;
    let renderPending = false;
    let destroyed = false;
    const jobs = new Map();
    const view = chat.createView({
      onSend: send, onStop: stop, onClear: clear, onThink: setThinking, onAnswer: answer, onDraft: (text) => { draft = text; },
      renderMarkdown: (text) => callHook("renderMarkdown", text),
    });

    function running() {
      return busy || state.running;
    }

    // An automatic continuation waits for the run and for any open approval to finish.
    function blocked() {
      return running() || state.items.some((item) => item.kind === "approval" && item.status === "pending");
    }

    function statusText() {
      if (status.text) return status.text;
      if (!running()) return "";
      if (queuedBehindJob) return QUEUED_TEXT;
      return thinking ? "Thinking…" : "Working…";
    }

    function render() {
      renderPending = false;
      if (destroyed) return;
      view.render(state, jobs, { running: running(), status: statusText(), statusError: status.error, thinking, draft });
    }

    function scheduleRender() {
      if (renderPending) return;
      renderPending = true;
      (root.requestAnimationFrame || ((fn) => setTimeout(fn, 16)))(render);
    }

    function setStatus(text, error) {
      status = { text: text || "", error: Boolean(error) };
      scheduleRender();
    }

    function dispatch(event) {
      if (destroyed) return;
      state = S.reduce(state, event);
      if (event.type === "TOOL_CALL_RESULT") toolFinished(event.toolCallId);
      if (event.type === "local.history") syncJobs(false);
      scheduleRender();
    }

    // Host hooks are optional, and a failing one must not break the chat.
    function callHook(name, ...args) {
      try {
        return hooks[name] ? hooks[name](...args) : undefined;
      } catch (error) {
        return undefined;
      }
    }

    async function runTurn(input, sentUser) {
      busy = true;
      setStatus("");
      abort = new AbortController();
      const turn = Object.assign({ workspace, phase, thinking, state: callHook("getUiState") || {} }, input);
      try {
        await IO.postRun(turn, dispatch, abort.signal);
      } catch (error) {
        turnFailed(error, sentUser);
      } finally {
        turnEnded();
      }
    }

    function turnFailed(error, sentUser) {
      if (error && error.name === "AbortError") return;
      if (error && error.status === 409 && sentUser) {
        dispatch({ type: "local.remove", id: sentUser.id });
        if (sentUser.auto) {
          // A duplicate means another tab or an earlier page already continued this job.
          if (error.message === DUPLICATE) return;
          cont = S.requeue(cont, sentUser.auto);
          holdQueue = true;
          return;
        }
        draft = sentUser.text;
      }
      setStatus((error && error.message) || "The orchestrator could not be reached.", true);
    }

    function turnEnded() {
      busy = false;
      abort = null;
      queuedBehindJob = false;
      if (state.running) dispatch({ type: "RUN_FINISHED" });
      scheduleRender();
      const held = holdQueue;
      holdQueue = false;
      if (destroyed || held || blocked()) return;
      const next = S.takeQueued(cont);
      cont = next.cont;
      if (next.post) send(next.post.text, next.post);
    }

    // `auto` is the continuation ({text, toolCallId, jobKey}) of an automatic post-job turn.
    // The host may add tags (attachments, options) to what the author typed.
    function send(text, auto) {
      if (running() || destroyed) return;
      const typed = auto ? text : callHook("composeMessage", String(text || "")) ?? text;
      const message = String(typed || "").trim();
      if (!message) return;
      const id = IO.newId();
      queuedBehindJob = !auto && [...jobs.values()].some((entry) => !entry.terminal && !entry.gone);
      if (!auto) {
        draft = "";
        view.clearInput();
      }
      const autoContinue = auto ? { toolCallId: auto.toolCallId, jobKey: auto.jobKey } : null;
      dispatch({ type: "local.user", id, text: message, auto: autoContinue });
      runTurn({ messages: [{ id, role: "user", content: message }], autoContinue }, { id, text: message, auto: auto || null });
    }

    function answer(id, approved, reason) {
      if (running()) return;
      dispatch({ type: "local.answer", id, approved, reason });
      const resume = S.pendingResume(state);
      if (!resume.length) return;
      dispatch({ type: "local.resumeSent", ids: resume.map((entry) => entry.interruptId) });
      runTurn({ messages: [], resume }, null);
    }

    function stop() {
      IO.stopRun(workspace, phase).catch((error) => setStatus(error.message, true));
    }

    async function clear() {
      if (running()) return setStatus(IO.BUSY_MESSAGE, true);
      if (!root.confirm(CLEAR_CONFIRM)) return;
      try {
        await IO.clearHistory(workspace, phase);
        stopJobs();
        state = S.initialState();
        setStatus("");
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    function setThinking(value) {
      thinking = Boolean(value);
      IO.writeStorage(THINK_KEY, thinking);
      scheduleRender();
    }

    async function load() {
      try {
        const history = await IO.loadHistory(workspace, phase);
        dispatch({ type: "local.history", history });
        if (history.running) await replay();
      } catch (error) {
        setStatus(error.message || "Could not load the orchestrator chat.", true);
      }
    }

    async function replay() {
      busy = true;
      abort = new AbortController();
      try {
        await IO.replayRun(workspace, phase, dispatch, abort.signal);
      } catch (error) {
        turnFailed(error, null);
      } finally {
        turnEnded();
      }
    }

    // A tool that changed a setting (e.g. the system-panel mode) lets the host refresh its panel.
    function toolFinished(id) {
      syncJobs(true);
      const item = state.items.find((entry) => entry.id === id);
      if (S.refreshesHost(item)) callHook("onToolDone", item);
    }

    function syncJobs(started) {
      S.trackableJobs(state).forEach((item) => {
        if (jobs.has(item.id)) return;
        jobs.set(item.id, { job: item.job, record: null, terminal: false, gone: false, timer: null });
        if (started) callHook("onJobStarted", item.job);
        pollJob(item.id);
      });
    }

    function updateJob(id, change) {
      const entry = Object.assign({}, jobs.get(id), change);
      jobs.set(id, entry);
      scheduleRender();
      return entry;
    }

    async function pollJob(id) {
      const current = jobs.get(id);
      if (!current || destroyed) return;
      try {
        const record = await IO.pollJob(current.job.url);
        if (!jobs.has(id) || destroyed) return;
        const entry = updateJob(id, { record, terminal: S.isTerminalJobStatus(record.status), gone: record.status === "idle" });
        if (entry.terminal) return jobFinished(id, entry);
        if (!entry.gone) updateJob(id, { timer: setTimeout(() => pollJob(id), POLL_MS) });
      } catch (error) {
        if (!jobs.has(id)) return;
        if (error.status === 404) return updateJob(id, { gone: true });
        updateJob(id, { timer: setTimeout(() => pollJob(id), POLL_MS * 2) });
      }
    }

    function jobFinished(id, entry) {
      callHook("onJobFinished", entry.job, entry.record.status);
      const key = S.jobKey(entry.job, entry.record, id);
      if (state.items.some((item) => item.auto && item.jobKey === key)) return;
      const turn = { text: S.continuationText(entry.job, entry.record), toolCallId: id, jobKey: key };
      const claim = S.claimContinuation(cont, key, turn, blocked());
      cont = claim.cont;
      IO.writeStorage(continuedKey, cont.sent.slice(-50));
      if (claim.post) send(claim.post.text, claim.post);
    }

    function stopJobs() {
      jobs.forEach((entry) => clearTimeout(entry.timer));
      jobs.clear();
    }

    function remount(container) {
      if (destroyed || !container) return;
      view.attach(container);
      render();
    }

    function destroy() {
      destroyed = true;
      if (abort) abort.abort();
      stopJobs();
      view.detach();
      registry.delete(`${workspace}:${phase}`);
    }

    return { workspace, remount, destroy, load, setHooks: (next) => { hooks = next; }, send: (text) => send(text, false) };
  }

  // Mounting the same (workspace, phase) again re-attaches the existing controller.
  function mount(container, options) {
    const key = `${options.workspace}:${options.phase}`;
    const existing = registry.get(key);
    if (existing) {
      existing.setHooks(options);
      existing.remount(container);
      return existing;
    }
    const controller = createController(options);
    registry.set(key, controller);
    controller.remount(container);
    controller.load();
    return controller;
  }

  // Stops the chats of every other workspace, so their streams and job polls end.
  function destroyOtherWorkspaces(workspace) {
    [...registry.values()].filter((controller) => controller.workspace !== workspace).forEach((controller) => controller.destroy());
  }

  chat.mount = mount;
  chat.destroyOtherWorkspaces = destroyOtherWorkspaces;
})(window);
