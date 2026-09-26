function worldChatBase(name) {
  return `/api/workspaces/${encodeURIComponent(name)}/world-knowledge`;
}

function worldChatOn(name) {
  return wizardState.workspace === name;
}

const WORLD_SEND_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';

let worldJobPollTimer = null;

function worldJobActive(job) {
  return Boolean(job && ["running", "stopping"].includes(job.status));
}

function worldJobMarkup(job) {
  if (!worldJobActive(job)) return "";
  const total = Number(job.total || 0);
  const completed = Number(job.completed || 0);
  const percent = total ? Math.max(2, Math.min(100, Math.round(completed * 100 / total))) : 3;
  const stopping = job.status === "stopping";
  const meta = total ? `${completed} / ${total}` : escapeHtml(job.phase || "working");
  return `<div class="chat-job-progress ${stopping ? "is-stopping" : ""}" id="world-job-progress">
    <div class="chat-job-progress-main">
      <span class="chat-job-status-dot" aria-hidden="true"></span>
      <div class="chat-job-progress-copy">
        <strong>${escapeHtml(job.message || "Updating the world")}</strong>
        <span>${meta}</span>
      </div>
      <div class="chat-job-actions"><button id="stop-world-job" class="chat-job-action stop" type="button" ${stopping ? "disabled" : ""}><span aria-hidden="true">■</span>${stopping ? "Stopping" : "Stop"}</button></div>
    </div>
    <div class="chat-job-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><i style="width:${percent}%"></i></div>
  </div>`;
}

function worldChatPanelMarkup(conversation, job) {
  const turns = Array.isArray(conversation?.turns) ? conversation.turns : [];
  const guide = conversation?.guide || {};
  const busy = worldJobActive(job);
  const clearBtn = turns.length
    ? '<button id="clear-world-chat" class="chat-icon-btn" type="button" title="Clear conversation">Clear conversation</button>'
    : "";
  const resetGuide = guide.exists
    ? '<button id="reset-world-guide" class="chat-icon-btn" type="button">Restore default</button>'
    : "";
  const empty = '<div class="chat-empty"><div class="chat-empty-icon">🌍</div><p>Describe the world in chat, or answer follow-up questions. Sections appear in the review list when they are written.</p></div>';
  return `<section class="chat-panel" id="world-chat">
    <div class="writing-guide-bar"><div><strong>Chat guide</strong><span>${guide.exists ? "Using a custom chat guide" : "Using the built-in world-chat rules"}</span></div><div class="writing-guide-actions"><input id="world-guide-file" type="file" accept=".txt,.md" hidden><button id="upload-world-guide" class="chat-icon-btn" type="button">Upload guide</button>${resetGuide}</div></div>
    <div class="chat-scroll" id="chat-message-list">${turns.map(chatMessageMarkup).join("") || empty}</div>
    ${worldJobMarkup(job)}
    <div class="chat-composer">
      <div class="chat-input-row">
        <textarea id="world-chat-input" class="chat-input" rows="1" placeholder="Describe the target world, or ask to update a section" ${busy ? "disabled" : ""}></textarea>
        <button id="send-world-chat" class="chat-send-btn" type="button" title="Send (Ctrl/⌘+Enter)" aria-label="Send" ${busy ? "disabled" : ""}>${WORLD_SEND_ICON}</button>
      </div>
      <div class="chat-composer-meta">${clearBtn}</div>
    </div>
  </section>`;
}

function bindWorldJobProgress() {
  $("#stop-world-job")?.addEventListener("click", stopWorldJob);
}

function renderWorldChat(conversation, job) {
  const host = $("#world-chat-host");
  if (!host) return;
  host.innerHTML = worldChatPanelMarkup(conversation, job);
  const list = $("#chat-message-list");
  if (list) list.scrollTop = list.scrollHeight;
  const input = $("#world-chat-input");
  const grow = () => { if (input) { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; } };
  input?.addEventListener("input", grow);
  input?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      sendWorldMessage();
    }
  });
  $("#send-world-chat")?.addEventListener("click", sendWorldMessage);
  bindWorldJobProgress();
  $$("[data-artifact-path]").forEach((button) => {
    button.addEventListener("click", () => openReviewFile(button.dataset.artifactPath));
  });
  $("#clear-world-chat")?.addEventListener("click", clearWorldConversation);
  $("#upload-world-guide")?.addEventListener("click", () => $("#world-guide-file")?.click());
  $("#world-guide-file")?.addEventListener("change", saveWorldGuide);
  $("#reset-world-guide")?.addEventListener("click", resetWorldGuide);
  mountPhaseChat(host, "world");
}

async function loadWorldChat() {
  const workspace = wizardState.workspace;
  if (!workspace || !$("#world-chat-host")) return;
  const base = worldChatBase(workspace);
  try {
    const [conversation, job] = await Promise.all([api(`${base}/conversation`), api(`${base}/job`)]);
    if (!worldChatOn(workspace)) return;
    renderWorldChat(conversation, job);
    if (worldJobActive(job)) pollWorldJob();
  } catch (error) {
    if (!worldChatOn(workspace)) return;
    showToast(error.message || "Could not load the world chat.", true);
  }
}

async function sendWorldMessage() {
  const input = $("#world-chat-input");
  const message = (input?.value || "").trim();
  if (!message || input?.disabled) return;
  const workspace = wizardState.workspace;
  const base = worldChatBase(workspace);
  const button = $("#send-world-chat");
  if (button) button.disabled = true;
  if (input) input.disabled = true;
  try {
    const job = await api(`${base}/chat`, { method: "POST", body: JSON.stringify({ message }) });
    if (!worldChatOn(workspace)) return;
    const conversation = await api(`${base}/conversation`);
    if (!worldChatOn(workspace)) return;
    renderWorldChat(conversation, job);
    pollWorldJob();
  } catch (error) {
    if (!worldChatOn(workspace)) return;
    showToast(error.message || "Could not start world chat.", true);
    loadWorldChat();
  }
}

async function stopWorldJob() {
  const workspace = wizardState.workspace;
  const base = worldChatBase(workspace);
  try {
    const job = await api(`${base}/stop`, { method: "POST", body: JSON.stringify({}) });
    if (!worldChatOn(workspace)) return;
    const conversation = await api(`${base}/conversation`);
    if (!worldChatOn(workspace)) return;
    renderWorldChat(conversation, job);
    pollWorldJob();
  } catch (error) {
    if (!worldChatOn(workspace)) return;
    showToast(error.message || "Could not stop world chat.", true);
  }
}

async function clearWorldConversation() {
  if (!confirm("Clear the world-chat conversation? The seven section files are kept.")) return;
  const workspace = wizardState.workspace;
  const base = worldChatBase(workspace);
  try {
    await api(`${base}/conversation`, { method: "DELETE" });
    if (!worldChatOn(workspace)) return;
    showToast("World-chat conversation cleared.");
    loadWorldChat();
  } catch (error) {
    if (!worldChatOn(workspace)) return;
    showToast(error.message || "Could not clear the conversation.", true);
  }
}

async function saveWorldGuide() {
  const file = $("#world-guide-file")?.files?.[0];
  if (!file) return;
  const workspace = wizardState.workspace;
  const base = worldChatBase(workspace);
  try {
    const upload = await uploadFile(file);
    if (!worldChatOn(workspace)) return;
    await api(`${base}/guide`, { method: "POST", body: JSON.stringify({ upload_id: upload.id }) });
    if (!worldChatOn(workspace)) return;
    showToast("Custom chat guide saved.");
    loadWorldChat();
  } catch (error) {
    if (!worldChatOn(workspace)) return;
    showToast(error.message || "Could not save the chat guide.", true);
  }
}

async function resetWorldGuide() {
  const workspace = wizardState.workspace;
  const base = worldChatBase(workspace);
  try {
    await api(`${base}/guide`, { method: "DELETE" });
    if (!worldChatOn(workspace)) return;
    showToast("Restored the built-in world-chat rules.");
    loadWorldChat();
  } catch (error) {
    if (!worldChatOn(workspace)) return;
    showToast(error.message || "Could not reset the chat guide.", true);
  }
}

function pollWorldJob() {
  if (worldJobPollTimer) clearTimeout(worldJobPollTimer);
  const workspace = wizardState.workspace;
  const base = worldChatBase(workspace);
  const poll = async () => {
    if (!worldChatOn(workspace) || wizardState.activeStep !== "world") return;
    try {
      const job = await api(`${base}/job`);
      if (!worldChatOn(workspace) || wizardState.activeStep !== "world") return;
      if (worldJobActive(job)) {
        const progress = $("#world-job-progress");
        if (progress) {
          const holder = document.createElement("div");
          holder.innerHTML = worldJobMarkup(job);
          if (holder.firstElementChild) progress.replaceWith(holder.firstElementChild);
          bindWorldJobProgress();
        } else {
          const conversation = await api(`${base}/conversation`);
          if (!worldChatOn(workspace) || wizardState.activeStep !== "world") return;
          renderWorldChat(conversation, job);
        }
        worldJobPollTimer = setTimeout(poll, 1000);
        return;
      }
      await refreshWorkspaceArtifacts();
      if (!worldChatOn(workspace) || wizardState.activeStep !== "world") return;
      renderWorldChat(await api(`${base}/conversation`), job);
      if (job.status === "failed") showToast(job.error || "World chat failed.", true);
      else if (job.status === "stopped") showToast("This world-chat round has ended.");
      else if (job.status === "completed") showToast("World chat completed.");
    } catch (_) {
      worldJobPollTimer = setTimeout(poll, 1500);
    }
  };
  poll();
}
