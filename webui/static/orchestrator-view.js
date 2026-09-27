/* DOM view of the phase orchestrator chat. All untrusted text goes through textContent. */
(function (root) {
  "use strict";

  const chat = root.OrchestratorChat = root.OrchestratorChat || {};

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(className, label, onClick) {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  function argsSummary(args, limit) {
    const max = limit || 80;
    const data = chat.state.parseJson(args);
    const text = data
      ? Object.keys(data).map((key) => `${key}: ${typeof data[key] === "string" ? data[key] : JSON.stringify(data[key])}`).join(", ")
      : String(args || "").replace(/\s+/g, " ").trim();
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }

  function prettyArgs(args) {
    const data = chat.state.parseJson(args);
    return data ? JSON.stringify(data, null, 2) : String(args || "");
  }

  function userItem(item) {
    const node = el("li", "chat-message user");
    node.append(el("div", "chat-message-body", item.text));
    return node;
  }

  // The host's markdown renderer escapes the text before it adds markup; without one the reply
  // stays plain text.
  function assistantItem(item, entry, handlers) {
    const node = el("li", "chat-message assistant");
    const content = el("div", "chat-message-content");
    const html = handlers.renderMarkdown ? handlers.renderMarkdown(item.text) : null;
    const body = el("div", typeof html === "string" ? "chat-message-body review-document chat-markdown" : "chat-message-body");
    if (typeof html === "string") body.innerHTML = html;
    else body.textContent = item.text;
    content.append(body);
    node.append(el("div", "chat-message-avatar", "AI"), content);
    return node;
  }

  function disclosure(className, summary, body) {
    const details = el("details", className);
    const head = el("summary");
    head.append(...summary);
    details.append(head, ...body);
    return details;
  }

  function reasoningItem(item) {
    const node = el("li", "orch-item orch-reasoning");
    node.append(disclosure("orch-disclosure", [el("span", "orch-label", "Reasoning"), el("span", "orch-reasoning-tail")], [el("div", "orch-pre", item.text)]));
    return node;
  }

  // Rewriting the <li> restarts its fade-in, so a token stream flashes. Paint the same node.
  function paintReasoning(node, item, streaming) {
    const header = chat.state.reasoningHeader(item.text, streaming);
    node.querySelector(".orch-label").textContent = header.label;
    node.querySelector(".orch-reasoning-tail").textContent = header.tail;
    node.querySelector(".orch-pre").textContent = item.text;
    node.classList.toggle("is-streaming", streaming);
  }

  const TOOL_STATES = { started: "job started", done: "done", refused: "refused" };

  function toolState(item) {
    if (item.awaiting) return "awaiting approval";
    if (item.pending) return "running";
    if (item.denied) return "denied";
    return (item.data && TOOL_STATES[item.data.status]) || "done";
  }

  function toolBody(item) {
    const body = [el("h5", "orch-heading", "Arguments"), el("pre", "orch-pre", prettyArgs(item.args) || "(none)")];
    if (item.result === null || item.result === undefined) return body;
    const message = item.data && typeof item.data.message === "string" ? item.data.message : item.result;
    return body.concat([el("h5", "orch-heading", "Result"), el("div", "orch-pre", message)]);
  }

  function jobCard(item, entry) {
    const model = chat.state.jobCardModel(item, entry);
    const percent = model.percent;
    const card = el("div", `chat-job-progress orch-job${model.finished ? " is-finished" : ""}`);
    const main = el("div", "chat-job-progress-main");
    const copy = el("div", "chat-job-progress-copy");
    copy.append(el("strong", "", model.label), el("span", "", model.meta));
    const dot = el("span", "chat-job-status-dot");
    dot.setAttribute("aria-hidden", "true");
    main.append(dot, copy);
    const track = el("div", "chat-job-progress-track");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", `Progress of job ${item.job.kind}`);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(percent));
    const bar = el("i");
    bar.style.width = `${percent}%`;
    track.append(bar);
    card.append(main, track);
    return card;
  }

  function toolItem(item, entry) {
    const node = el("li", "orch-item orch-tool");
    const summary = [
      el("span", "orch-tool-name", item.name),
      el("span", "orch-tool-args", argsSummary(item.args)),
      el("span", `orch-tool-state is-${toolState(item).replace(/\s+/g, "-")}`, toolState(item)),
    ];
    node.append(disclosure("orch-disclosure orch-tool-card", summary, toolBody(item)));
    if (item.job && !item.jobSettled) node.append(jobCard(item, entry));
    return node;
  }

  function activityItem(item) {
    const tone = /error/.test(item.activityType || "") ? " is-error" : "";
    const node = el("li", `orch-item orch-activity${tone}${item.auto ? " is-auto" : ""}`, item.text);
    node.setAttribute("role", tone ? "alert" : "note");
    return node;
  }

  const APPROVAL_OUTCOMES = { approved: "Approved", resolved: "Answered" };

  function approvalItem(item, entry, handlers) {
    const node = el("li", "orch-item orch-approval");
    node.append(el("p", "orch-approval-message", item.message));
    if (item.status !== "pending") {
      const outcome = APPROVAL_OUTCOMES[item.status] || `Denied${item.reason ? `: ${item.reason}` : ""}`;
      node.append(el("p", `orch-approval-outcome is-${item.status}`, outcome));
      return node;
    }
    const reason = el("input", "orch-approval-reason");
    reason.type = "text";
    reason.placeholder = "Reason (optional)";
    reason.setAttribute("aria-label", "Reason for denying (optional)");
    const actions = el("div", "orch-approval-actions");
    actions.append(
      button("primary-button orch-approve", "Approve", () => handlers.onAnswer(item.id, true, "")),
      button("secondary-button orch-deny", "Deny", () => handlers.onAnswer(item.id, false, reason.value.trim())),
      reason,
    );
    node.append(actions);
    return node;
  }

  const ITEM_BUILDERS = {
    user: userItem, assistant: assistantItem, reasoning: reasoningItem,
    tool: toolItem, activity: activityItem, approval: approvalItem,
  };

  function createView(handlers) {
    const rendered = new Map();
    const openIds = new Set();
    let nodes = null;
    // The composer's selection while it has focus, so a re-attach can put the caret back.
    let caret = null;
    let restoreCaret = null;

    function composer() {
      const box = el("div", "chat-composer");
      const row = el("div", "chat-input-row");
      const input = el("textarea", "chat-input orch-input");
      input.rows = 1;
      input.placeholder = "Ask the orchestrator. Ctrl/⌘+Enter sends.";
      input.setAttribute("aria-label", "Message to the orchestrator");
      const remember = () => { caret = { start: input.selectionStart, end: input.selectionEnd }; };
      ["focus", "keyup", "mouseup", "select"].forEach((type) => input.addEventListener(type, remember));
      // Chrome blurs the composer just before a re-render removes it; only a real move of focus
      // (to another element, or elsewhere while the composer stays on the page) forgets the caret.
      input.addEventListener("blur", (event) => {
        if (event.relatedTarget) caret = null;
        else setTimeout(() => { if (input.isConnected) caret = null; }, 0);
      });
      input.addEventListener("input", () => {
        handlers.onDraft(input.value);
        input.style.height = "auto";
        input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
      });
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey) || event.isComposing) return;
        event.preventDefault();
        handlers.onSend(input.value);
      });
      const send = button("chat-send-btn orch-send", "Send", () => handlers.onSend(input.value));
      send.title = "Send (Ctrl/⌘+Enter)";
      const stop = button("chat-job-action stop orch-stop", "Stop", handlers.onStop);
      row.append(input, send, stop);
      box.append(row);
      return { box, input, send, stop };
    }

    function toolbar() {
      const bar = el("div", "orch-toolbar");
      const think = el("label", "orch-think");
      const toggle = el("input");
      toggle.type = "checkbox";
      toggle.addEventListener("change", () => handlers.onThink(toggle.checked));
      think.append(toggle, el("span", "", "Think"));
      think.title = "Let the model reason before it answers (slower)";
      bar.append(think, button("chat-icon-btn orch-clear", "Clear thread", handlers.onClear));
      return { bar, toggle };
    }

    function attach(container) {
      rendered.clear();
      restoreCaret = caret;
      const panel = el("section", "chat-panel orch-panel");
      panel.setAttribute("aria-label", "Orchestrator chat");
      const top = toolbar();
      const list = el("ol", "chat-scroll orch-list");
      list.setAttribute("role", "log");
      list.setAttribute("aria-label", "Conversation");
      list.addEventListener("toggle", (event) => {
        const id = event.target.closest("[data-item-id]")?.dataset.itemId;
        if (id) (event.target.open ? openIds.add(id) : openIds.delete(id));
      }, true);
      const status = el("p", "orch-status");
      status.setAttribute("role", "status");
      const box = composer();
      panel.append(top.bar, list, status, box.box);
      container.replaceChildren(panel);
      nodes = { container, list, status, toggle: top.toggle, input: box.input, send: box.send, stop: box.stop };
    }

    function itemNode(item, entry, streamingId) {
      const previous = rendered.get(item.id);
      const streaming = item.kind === "reasoning" && item.id === streamingId;
      if (previous && item.kind === "reasoning") {
        if (previous.item !== item || previous.streaming !== streaming) paintReasoning(previous.node, item, streaming);
        rendered.set(item.id, { item, entry, node: previous.node, streaming });
        return previous.node;
      }
      if (previous && previous.item === item && previous.entry === entry) return previous.node;
      const node = ITEM_BUILDERS[item.kind](item, entry, handlers);
      node.dataset.itemId = item.id;
      const details = node.querySelector("details");
      if (details && openIds.has(item.id)) details.open = true;
      if (streaming) paintReasoning(node, item, true);
      rendered.set(item.id, { item, entry, node, streaming });
      return node;
    }

    function renderList(items, jobs, streamingId) {
      const list = nodes.list;
      const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
      const wanted = items.filter((item) => ITEM_BUILDERS[item.kind]).map((item) => itemNode(item, jobs.get(item.id), streamingId));
      if (!wanted.length) wanted.push(el("li", "chat-empty orch-empty", "Ask the orchestrator to plan, check or run this step."));
      wanted.forEach((node, index) => {
        if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
      });
      while (list.children.length > wanted.length) list.lastElementChild.remove();
      if (nearBottom) list.scrollTop = list.scrollHeight;
    }

    function render(state, jobs, ui) {
      if (!nodes) return;
      renderList(state.items, jobs, state.running ? state.openReasoningId : null);
      nodes.list.setAttribute("aria-busy", ui.running ? "true" : "false");
      nodes.status.textContent = ui.status || "";
      nodes.status.classList.toggle("is-error", Boolean(ui.statusError));
      nodes.toggle.checked = ui.thinking;
      if (nodes.input.value !== ui.draft && document.activeElement !== nodes.input) nodes.input.value = ui.draft;
      if (restoreCaret) {
        nodes.input.focus();
        nodes.input.setSelectionRange(restoreCaret.start, restoreCaret.end);
        restoreCaret = null;
      }
      nodes.send.disabled = ui.running;
      nodes.stop.disabled = !ui.running;
    }

    function clearInput() {
      if (nodes) nodes.input.value = "";
    }

    function detach() {
      if (nodes) nodes.container.replaceChildren();
      nodes = null;
      rendered.clear();
    }

    return { attach, render, clearInput, detach };
  }

  chat.createView = createView;
})(window);
