/* Puts the phase orchestrator chat into the wizard's step panels. Each panel keeps its plain
   controls (selectors, guide bars, job banners, resets); only its conversation list and text input
   give way to the chat. With localStorage "harnessNovel.legacyChat" set to "1" the old chat stays.
   Uses the wizard's globals (wizardState, $, uploadFile, the load*Chat functions) at call time. */
(function (root) {
  "use strict";

  const LEGACY_KEY = "harnessNovel.legacyChat";
  const MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024;
  const OPTION_TAGS = [["#use-new-reference", "[use_new_reference]"], ["#sync-stage-design", "[sync_updated_design]"]];
  const SCOPES = { design: "concept", stage: "stage" };
  const latestOpen = {};

  function legacyChat() {
    try {
      return root.localStorage.getItem(LEGACY_KEY) === "1";
    } catch (error) {
      return false;
    }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  // The volume and arc the panel's selectors picked, as the tools' defaults.
  const SELECTIONS = {
    arcs: () => ({ volume: wizardState.arcsChatVolume }),
    chapters: () => ({ volume: wizardState.chaptersChatVolume, arc: wizardState.chaptersChatArc }),
    draft: () => ({ volume: wizardState.draftChatVolume, arc: wizardState.draftChatArc }),
  };

  function uiState(phase) {
    const picked = SELECTIONS[phase] ? SELECTIONS[phase]() : {};
    const state = {};
    Object.keys(picked).forEach((key) => {
      if (Number(picked[key]) > 0) state[key] = Number(picked[key]);
    });
    return state;
  }

  // A job the orchestrator started on another volume or arc moves the selectors there, so the
  // panel shows that job's banner and its review follows the job.
  const FOLLOW = {
    arcs: (job) => { if (job.volume) wizardState.arcsChatVolume = job.volume; },
    chapters: (job) => {
      if (job.volume) wizardState.chaptersChatVolume = job.volume;
      if (job.arc) wizardState.chaptersChatArc = job.arc;
    },
    draft: (job) => {
      if (job.volume) wizardState.draftChatVolume = job.volume;
      if (job.arc) wizardState.draftChatArc = job.arc;
    },
  };

  const RELOAD = {
    world: () => loadWorldChat(),
    design: () => loadDesignChat("concept"),
    stage: () => loadDesignChat("stage"),
    arcs: () => loadArcsChat(wizardState.arcsChatVolume || 1),
    chapters: () => loadChaptersChat(wizardState.chaptersChatVolume, wizardState.chaptersChatArc),
    draft: () => loadDraftChat(wizardState.draftChatVolume, wizardState.draftChatArc),
  };

  function onStep(workspace, phase) {
    return wizardState.workspace === workspace && wizardState.activeStep === phase;
  }

  // Reloading the panel shows the job banner (pause/resume/stop, prompts) and starts its poller.
  function jobStarted(workspace, phase, job) {
    if (!onStep(workspace, phase)) return;
    if (FOLLOW[phase] && job) FOLLOW[phase](job);
    RELOAD[phase]();
  }

  // After a job or a settings change: the step's form, review list and panel all re-render.
  function refreshWorkspace(workspace) {
    if (wizardState.workspace === workspace) refreshWorkspaceArtifacts();
  }

  // Checked design options and attached uploads become tags in front of the author's message;
  // the design and stage instructions tell the model what they mean.
  function designTags(scope) {
    const tags = OPTION_TAGS.filter(([selector]) => $(selector)?.checked).map(([, tag]) => tag);
    OPTION_TAGS.forEach(([selector]) => {
      const box = $(selector);
      if (box) box.checked = false;
    });
    const files = chatAttachments(scope).splice(0).filter((file) => file.upload_id);
    renderChatAttachments(scope);
    return tags.concat(files.map((file) => `[attached upload ${file.upload_id}: ${file.name}]`));
  }

  function draftTags() {
    return $("#draft-chat-humanize")?.checked === false ? ["[humanize: off]"] : [];
  }

  function composeMessage(phase, text) {
    const tags = SCOPES[phase] ? designTags(SCOPES[phase]) : phase === "draft" ? draftTags() : [];
    return tags.length ? `${tags.join(" ")}\n${text}` : text;
  }

  function hooksFor(phase) {
    const workspace = wizardState.workspace;
    return {
      workspace,
      phase,
      escapeHtml,
      renderMarkdown: markdownPreview,
      getUiState: () => uiState(phase),
      onJobStarted: (job) => jobStarted(workspace, phase, job),
      onJobFinished: () => refreshWorkspace(workspace),
      onToolDone: () => refreshWorkspace(workspace),
      composeMessage: (text) => composeMessage(phase, text),
    };
  }

  async function uploadPicked(picker, scope) {
    const files = [...(picker.files || [])];
    picker.value = "";
    for (const file of files) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        showToast(`“${file.name}” is over 2MB and was not loaded (trim it and retry).`, true);
        continue;
      }
      try {
        const upload = await uploadFile(file);
        chatAttachments(scope).push({ name: upload.name || file.name, upload_id: upload.id, size: upload.size });
      } catch (error) {
        showToast(error.message || `Could not upload “${file.name}”.`, true);
      }
    }
    renderChatAttachments(scope);
  }

  // Design attachments go through /api/uploads; the chat only sees their ids and names.
  function bindUploads(panel, scope) {
    const old = panel.querySelector("#chat-attach-input");
    const meta = panel.querySelector(".chat-composer-meta");
    if (!old || !meta) return;
    const picker = old.cloneNode(false);
    old.replaceWith(picker);
    picker.addEventListener("change", () => uploadPicked(picker, scope));
    const button = el("button", "chat-icon-btn orch-attach", "Attach file");
    button.type = "button";
    button.title = "Attach a text file to your next message";
    button.addEventListener("click", () => picker.click());
    meta.prepend(button);
  }

  // The pipeline's last reply keeps its artifact cards and Copy button, below the chat.
  function keepLatestResult(list, host, phase) {
    const replies = list.querySelectorAll(".chat-message.assistant");
    if (!replies.length) return;
    const details = el("details", "orch-latest");
    const body = el("ol", "orch-latest-body");
    body.append(replies[replies.length - 1]);
    details.append(el("summary", "", "Latest step result"), body);
    details.open = Boolean(latestOpen[phase]);
    details.addEventListener("toggle", () => { latestOpen[phase] = details.open; });
    host.after(details);
  }

  function trimComposer(panel) {
    const composer = panel.querySelector(".chat-composer");
    if (!composer) return;
    composer.querySelector(".chat-input-row")?.remove();
    composer.hidden = !composer.querySelector("button, label");
  }

  // Swaps the panel's conversation list and text input for the phase's orchestrator chat.
  // The chat controller outlives re-renders, so calling this after every render is cheap.
  function mountPhaseChat(node, phase) {
    if (legacyChat() || !node || !wizardState.workspace) return null;
    const list = node.querySelector("#chat-message-list");
    if (!list) return null;
    const host = el("div", "orch-host");
    list.replaceWith(host);
    keepLatestResult(list, host, phase);
    if (SCOPES[phase]) bindUploads(node, SCOPES[phase]);
    trimComposer(node);
    return root.OrchestratorChat.mount(host, hooksFor(phase));
  }

  // On a workspace switch or delete, the chats of every other workspace stop polling and streaming.
  function closeOtherPhaseChats(workspace) {
    if (root.OrchestratorChat.destroyOtherWorkspaces) root.OrchestratorChat.destroyOtherWorkspaces(workspace || null);
  }

  root.mountPhaseChat = mountPhaseChat;
  root.closeOtherPhaseChats = closeOtherPhaseChats;
})(window);
