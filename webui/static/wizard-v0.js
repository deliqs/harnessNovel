const WIZARD_STEPS = [
  { id: "reference", title: "Reference novel", short: "Deconstruct structure", optional: false, heading: "Reference novel deconstruction", lead: "The book outline, volume outlines, and story arcs are stored as structured assets.", decision: "Review book structure, volume boundaries, and story arcs before designing the new novel.", reviewPrefixes: ["reference/outlines"], reviewHint: "Inspect the book outline, volume outlines, and story arcs." },
  { id: "world", title: "Target world", short: "Optional knowledge base", optional: true, heading: "Build the target-world knowledge base", lead: "Use a primary source to define the world, then refine details with supplement sources.", decision: "Import sources and pick a primary source, or skip this step.", reviewPrefixes: ["file_system/world_knowledge/worlds/_final"], reviewHint: "Inspect world rules, power systems, and key characters." },
  { id: "design", title: "Book design", short: "Worldview and outline", optional: false, heading: "Design worldview, rough outline, and phase outline", lead: "After you enter inspiration, the system generates worldview, rough outline, and a standalone phase outline in sequence. You can keep chatting to adjust them.", decision: "The first draft is generated in three serial steps to keep context small; later adjustments keep all three files in sync.", reviewPrefixes: ["file_system/story_design/worldview.md", "file_system/story_design/rough_outline.md", "file_system/story_design/stage_outline.md", "file_system/story_design/core_gameplay.md"], reviewHint: "Review world rules, core gameplay, and phase progression, and keep adjusting anytime." },
  { id: "stage", title: "Stage design", short: "Mainline and stages", optional: false, heading: "Design the long mainline and stage roadmap", lead: "The system first generates the book-length long mainline, then generates each stage from the matching phases and reference volume outlines. After an interruption you can resume from completed stages.", decision: "Each run generates one stage and uses the previous stage for continuity. After generation you can still refine or extend through chat.", reviewPrefixes: ["file_system/story_design/long_mainline.md", "file_system/story_design/stage_roadmap.md", "file_system/novel_name_synopsis.md"], reviewHint: "Stages use a volume-outline structure: three-act progression, characters, foreshadowing, and core payoff." },
  { id: "arcs", title: "Story arcs", short: "Current stage", optional: false, heading: "Generate story arcs", lead: "Produce a continuous plot blueprint for the current stage.", decision: "Choose a stage, abstract reference narrative patterns, then generate new arcs.", reviewPrefixes: ["file_system/story_arcs"], reviewHint: "Review goals, conflicts, emotion, and hooks for generated volumes, and keep adjusting anytime." },
  { id: "chapters", title: "Chapter outlines", short: "Per-chapter cards", optional: false, heading: "Generate chapter outlines", lead: "Generate chapter outlines serially and keep the protagonist system-panel state in sync for each chapter.", decision: "Choose a stage or story arc, then generate per-chapter cards.", reviewPrefixes: ["file_system/chapter_outlines", "file_system/system_panels"], reviewHint: "Review chapter outlines and independently saved protagonist system-panel snapshots, and keep adjusting anytime." },
  { id: "draft", title: "Draft", short: "Write and refine", optional: false, heading: "Generate draft", lead: "Write continuously from chapter outlines and keep raw-draft backups in the background.", decision: "Choose a stage and story arc, then generate draft serially through chat.", reviewPrefixes: ["file_system/chapters"], reviewHint: "This view shows the refined draft only. Keep adjusting it in chat." },
];

const CONFIG_PREFIXES = {
  data_builder: "DATA_BUILDER",
  adaptive_builder: "ADAPTIVE_BUILDER",
  adaptive_builder_lite: "ADAPTIVE_BUILDER_LITE",
  draft: "DRAFT",
  editor: "EDITOR",
  critic: "CRITIC",
};

const REVIEW_GROUPS = {
  reference: [
    { title: "Book plan", description: "Overall pacing, phase progression, and core conflict.", matches: (path) => path.endsWith("/novel_outline.md") },
  ],
  world: [
    { title: "Target-world settings", description: "Shared world settings later design steps can cite directly.", matches: (path) => path.includes("/worlds/_final/") },
  ],
  design: [
    { title: "Creative design", description: "Defines gameplay, expectation, and stage progression for the book.", matches: (path) => path.includes("/story_design/") },
    { title: "Title suggestions", description: "Title directions and synopsis generated from book design.", matches: (path) => path.endsWith("/novel_name_synopsis.md") },
  ],
  stage: [
    { title: "Long mainline and stage roadmap", description: "Defines book-length suspense, phase goals, and stage order.", matches: (path) => path.endsWith("/long_mainline.md") || path.endsWith("/stage_roadmap.md") },
    { title: "Title and synopsis", description: "Title directions and platform synopsis from the complete book design.", matches: (path) => path.endsWith("/novel_name_synopsis.md") },
  ],
  arcs: [
    { title: "Stage story blueprint", description: "A continuous story blueprint inside the current stage.", matches: (path) => path.includes("/story_arcs/") },
  ],
  chapters: [
    { title: "Chapter design assets", description: "Each chapter's story line, emotional pacing, and descriptive one-chapter synopsis.", matches: (path) => path.includes("/chapter_outlines/") },
    { title: "System-panel state", description: "A structured protagonist-centered snapshot at the end of each chapter.", matches: (path) => path.includes("/system_panels/") },
  ],
  draft: [
    { title: "Refined draft", description: "Chapter text that can still be edited and published.", matches: (path) => path.includes("/chapters/") },
  ],
};

const wizardState = {
  workspace: null,
  summary: null,
  activeStep: null,
  confirmed: new Set(),
  activeTaskId: null,
  logOffset: 0,
  selectedFile: null,
  selectedFileContent: "",
  fileEditing: false,
  fileTree: [],
  reviewArtifacts: [],
  directionMode: "text",
  directionFile: null,
  directionFileContent: "",
  chatAttachments: {},
  arcsChatVolume: null,
  chaptersChatVolume: null,
  chaptersChatArc: null,
  draftChatVolume: null,
  draftChatArc: null,
  draftJobCompleted: {},
  draftJobIds: {},
  referenceFile: null,
  referenceScope: "all",
  mechanicsMode: "auto",
  mechanicsFile: null,
  lastSyncedTaskId: null,
  arcsJobCompleted: {},
  chaptersJobCompleted: {},
  designJobCompleted: {},
  systemPanelStatus: null,
  taskView: "log",
  currentPromptText: "",
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || "Request failed. Please retry shortly.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function closeSettings() {
  $("#settings-panel").classList.remove("open");
  $("#settings-panel").setAttribute("aria-hidden", "true");
  $("#settings-scrim").classList.remove("open");
}

function modelConfigFields(groupId, group) {
  const prefix = CONFIG_PREFIXES[groupId];
  const configured = group.inherited && group.api_key_configured
    ? "Using Lite fallback"
    : (group.api_key_configured ? "API key configured" : "API key not configured");
  const optional = ["draft", "editor", "critic"].includes(groupId);
  return `<section class="model-config-group">
    <header><h3>${escapeHtml(group.label)}</h3><span class="config-status ${group.api_key_configured ? "ready" : "missing"}">${configured}</span></header>
    <label>Model name<input name="${prefix}_MODEL" data-role-prefix="${optional ? prefix : ""}" value="${escapeHtml(group.model || "")}" placeholder="e.g. deepseek-v4-pro" autocomplete="off" /></label>
    <label>Base URL<input name="${prefix}_BASE_URL" data-role-prefix="${optional ? prefix : ""}" value="${escapeHtml(group.base_url || "")}" placeholder="https://api.example.com" autocomplete="off" /></label>
    <label>API Key<input name="${prefix}_API_KEY" data-role-prefix="${optional ? prefix : ""}" type="password" placeholder="${group.api_key_configured ? "Configured; leave blank to keep it" : "Enter API key"}" autocomplete="new-password" /></label>
    ${optional ? `<button class="secondary-button role-clear-button" type="button" data-clear-role="${prefix}">Clear role and use Lite fallback</button>` : ""}
  </section>`;
}

function promptTraceFields(mode) {
  return `<section class="model-config-group">
    <header><h3>Prompt debugging</h3><span class="config-status ${mode === "full" ? "missing" : "ready"}">${mode === "full" ? "Full content saved locally" : "Private by default"}</span></header>
    <label>Prompt trace mode<select name="HARNESS_NOVEL_PROMPT_TRACE_MODE">
      <option value="off" ${mode === "off" ? "selected" : ""}>Off — do not retain model-call diagnostics</option>
      <option value="metadata" ${mode === "metadata" ? "selected" : ""}>Metadata — model, time, and size only</option>
      <option value="full" ${mode === "full" ? "selected" : ""}>Full — save redacted, bounded prompt text for local debugging</option>
    </select></label>
    <p class="settings-note">Full prompts can contain unpublished source and instructions. Use it only on a private machine, then clear task prompts when finished.</p>
  </section>`;
}

async function openSettings() {
  const content = $("#settings-content");
  content.innerHTML = '<p class="settings-loading">Reading local settings…</p>';
  $("#settings-panel").classList.add("open");
  $("#settings-panel").setAttribute("aria-hidden", "false");
  $("#settings-scrim").classList.add("open");
  try {
    const config = await api("/api/config");
    const groups = Object.entries(config.groups || {});
    content.innerHTML = `<form id="model-config-form" class="model-config-form">
      <p class="config-path">${escapeHtml(config.config_path || "")}</p>
      ${groups.map(([id, group]) => modelConfigFields(id, group)).join("")}
      ${promptTraceFields(config.prompt_trace_mode || "metadata")}
      <div class="settings-actions"><button id="cancel-settings" class="secondary-button" type="button">Cancel</button><button class="primary-button" type="submit">Save settings</button></div>
    </form>`;
    $("#cancel-settings").addEventListener("click", closeSettings);
    $("#model-config-form").addEventListener("submit", saveModelConfig);
    $$('[data-clear-role]').forEach((button) => button.addEventListener("click", () => {
      const prefix = button.dataset.clearRole;
      $$(`[data-role-prefix="${prefix}"]`).forEach((input) => {
        input.value = "";
        input.dataset.clear = "true";
      });
    }));
    $$('[data-role-prefix]').forEach((input) => input.addEventListener("input", () => {
      delete input.dataset.clear;
    }));
  } catch (error) {
    content.innerHTML = `<p class="settings-error">${escapeHtml(error.message || "Could not read settings.")}</p>`;
  }
}

async function saveModelConfig(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  const values = {};
  const clearKeys = [];
  [...form.querySelectorAll('input[name], select[name]')].forEach((input) => {
    const value = input.value.trim();
    if (input.dataset.clear === "true") clearKeys.push(input.name);
    else if (value) values[input.name] = value;
  });
  submit.disabled = true;
  try {
    await api("/api/config", { method: "PUT", body: JSON.stringify({ values, clear_keys: clearKeys }) });
    closeSettings();
    showToast("LLM settings saved.");
  } catch (error) {
    showToast(error.message || "Failed to save settings.", true);
  } finally {
    submit.disabled = false;
  }
}

let toastTimer;
function showToast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 3200);
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}

function renderInlineMarkdown(value) {
  const codeTokens = [];
  let rendered = escapeHtml(value).replace(/`([^`]+)`/g, (_, code) => {
    const token = `@@CODE_${codeTokens.length}@@`;
    codeTokens.push(`<code>${code}</code>`);
    return token;
  });
  rendered = rendered.replace(/\[([^\]]+)]\(([^\s)]+)(?:\s+&quot;[^)]*&quot;)?\)/g, (match, label, href) => {
    const normalizedHref = href.replace(/&amp;/g, "&");
    if (!/^(https?:\/\/|mailto:)/i.test(normalizedHref)) return match;
    return `<a href="${href}" target="_blank" rel="noreferrer noopener">${label}</a>`;
  });
  rendered = rendered.replace(/(\*\*|__)(.+?)\1/g, "<strong>$2</strong>");
  rendered = rendered.replace(/~~(.+?)~~/g, "<del>$1</del>");
  rendered = rendered.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  rendered = rendered.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1<em>$2</em>");
  return rendered.replace(/@@CODE_(\d+)@@/g, (_, index) => codeTokens[Number(index)] || "");
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

function isTableDivider(line) {
  const cells = tableCells(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function markdownPreview(value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  let index = 0;
  const isUnordered = (line) => /^\s*(?:[-*+]|—|–)\s+/.test(line);
  const isOrdered = (line) => /^\s*\d+[.)]\s+/.test(line);
  const isBlockStart = (line, next) => /^(#{1,6})\s+/.test(line) || /^\s*```/.test(line) || /^\s*>\s?/.test(line) || /^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line) || isUnordered(line) || isOrdered(line) || (line.includes("|") && isTableDivider(next || ""));

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s*```([^\s]*)\s*$/);
    if (fence) {
      const language = fence[1] ? ` data-language="${escapeHtml(fence[1])}"` : "";
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      output.push(`<pre class="markdown-code"${language}><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
      output.push("<hr />");
      index += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      output.push(`<blockquote>${markdownPreview(quoteLines.join("\n"))}</blockquote>`);
      continue;
    }

    if (line.includes("|") && isTableDivider(lines[index + 1] || "")) {
      const headings = tableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      output.push(`<div class="markdown-table-wrap"><table><thead><tr>${headings.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${headings.map((_, cellIndex) => `<td>${renderInlineMarkdown(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }

    if (isUnordered(line) || isOrdered(line)) {
      const ordered = isOrdered(line);
      const items = [];
      const pattern = ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*(?:[-*+]|—|–)\s+(.+)$/;
      while (index < lines.length) {
        const item = lines[index].match(pattern);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      const tag = ordered ? "ol" : "ul";
      output.push(`<${tag}>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</${tag}>`);
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index], lines[index + 1])) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    output.push(`<p>${paragraph.map(renderInlineMarkdown).join("<br />")}</p>`);
  }
  return output.join("");
}

function inferredDone(step) {
  const summary = wizardState.summary;
  if (!summary) return false;
  if (wizardState.confirmed.has(step.id)) return true;
  if (step.id === "reference") return summary.reference.has_sample && summary.reference.chapter_count > 0;
  if (step.id === "world") return Boolean(summary.world_knowledge.ready);
  if (step.id === "design") return Boolean(summary.story_design?.concept_ready);
  if (step.id === "stage") return Boolean(summary.story_design?.stage_ready);
  if (step.id === "mechanics") return summary.mechanics.mode !== "Not initialized";
  if (step.id === "arcs") return summary.writing.story_arc_count > 0;
  if (step.id === "chapters") return summary.writing.chapter_outline_count > 0;
  if (step.id === "draft") return summary.writing.chapter_count > 0;
  return false;
}

function stepIndex(stepId) { return WIZARD_STEPS.findIndex((step) => step.id === stepId); }

function currentRecommendedStep() {
  const firstIncomplete = WIZARD_STEPS.find((step) => !inferredDone(step));
  return firstIncomplete?.id || "draft";
}

function canOpenStep(step) {
  const index = stepIndex(step.id);
  if (inferredDone(step)) return true;
  if (step.id === currentRecommendedStep()) return true;
  return WIZARD_STEPS.slice(0, index).every((item) => inferredDone(item) || item.optional);
}

function statusForStep(step) {
  if (inferredDone(step)) return "done";
  if (step.id === currentRecommendedStep()) return "active";
  if (canOpenStep(step)) return "ready";
  return "locked";
}

function renderRail() {
  $("#workflow-list").innerHTML = WIZARD_STEPS.map((step, index) => {
    const state = statusForStep(step);
    const active = step.id === wizardState.activeStep;
    const disabled = state === "locked" ? "disabled" : "";
    const meta = state === "done" ? "Has content" : state === "locked" ? "Waiting for previous step" : step.optional ? "Optional" : active ? "Current step" : "Pending";
    return `<button class="workflow-step ${state} ${active ? "active" : ""}" data-step="${step.id}" ${disabled} type="button"><span class="workflow-step-number">${state === "done" ? "✓" : index + 1}</span><span><span class="workflow-step-title">${step.title}</span><span class="workflow-step-meta">${meta}</span></span></button>`;
  }).join("");
  $$('[data-step]').forEach((button) => button.addEventListener("click", () => {
    wizardState.activeStep = button.dataset.step;
    renderRail();
    renderActiveStep();
  }));
}

function referenceStatus() {
  const reference = wizardState.summary?.reference || {};
  const processed = Number(reference.processed_chapter_count || 0);
  const stagedChapters = Number(reference.chapter_count || 0);
  const total = Number(reference.total_chapter_count || 0);
  const hasExisting = Boolean(reference.has_sample);
  const isComplete = hasExisting && Boolean(reference.is_complete);
  return { ...reference, processed, stagedChapters, total, hasExisting, isComplete };
}

function referenceScopeControls(defaultTarget, disabled = false) {
  return `<fieldset class="reference-scope" id="reference-scope" ${disabled ? "disabled" : ""}>
    <legend>Deconstruction range</legend>
    <div class="reference-scope-options">
      <label class="reference-scope-option"><input name="reference-scope" value="all" type="radio" ${wizardState.referenceScope === "all" ? "checked" : ""} /><span>Whole book</span></label>
      <label class="reference-scope-option"><input name="reference-scope" value="prefix" type="radio" ${wizardState.referenceScope === "prefix" ? "checked" : ""} /><span>First</span><input id="reference-max-chapters" type="number" min="1" value="${defaultTarget}" ${wizardState.referenceScope === "prefix" && !disabled ? "" : "disabled"} /><span>chapters</span></label>
    </div>
  </fieldset>`;
}

function designStatus() {
  const design = wizardState.summary?.story_design || {};
  const reference = referenceStatus();
  const ready = Number(design.ready_count || 0) === Number(design.total_count || 5);
  const newReferenceChapters = Number(design.new_reference_chapter_count || 0);
  const baseline = design.reference_baseline_chapters;
  const baselineMissing = baseline === null || baseline === undefined;
  const canUseReference = newReferenceChapters > 0 || (baselineMissing && reference.processed > 0);
  return { ...design, ready, newReferenceChapters, baseline, baselineMissing, referenceProcessed: reference.processed, canUseReference };
}

function isDesignChatStep(step) {
  return step.id === "design" || step.id === "stage" || step.id === "arcs" || step.id === "chapters" || step.id === "draft";
}



function arcsJobMarkup(job) {
  if (!job) return "";
  if (job.status === "idle" && job.can_resume) {
    const completed = Number(job.completed || 0);
    const total = Number(job.total || 0);
    const percent = total > 0 ? Math.round(completed * 100 / total) : 0;
    return `<div class="chat-job-progress is-interrupted" id="arcs-job-progress">
      <div class="chat-job-progress-main">
        <span class="chat-job-status-dot" aria-hidden="true"></span>
        <div class="chat-job-progress-copy">
          <strong>Last run stopped before Arc ${Number(job.next_arc || completed + 1)}</strong>
          <span>Kept ${completed} / ${total} story arcs</span>
        </div>
        <button id="continue-arcs-job" class="chat-job-action resume continue" type="button"><span aria-hidden="true">▶</span>Continue generating</button>
      </div>
      <div class="chat-job-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><i style="width:${percent}%"></i></div>
    </div>`;
  }
  if (["idle", "completed", "failed", "stopped"].includes(job.status)) return "";
  const total = Number(job.total || 0);
  const completed = Number(job.completed || 0);
  const serialRefine = job.progress_kind === "serial_refine";
  const routing = serialRefine && job.phase === "routing";
  const percent = total > 0 ? Math.round(completed * 100 / total) : 4;
  const paused = job.status === "paused";
  const pausing = job.status === "pausing";
  const stopping = job.status === "stopping";
  const pauseAction = paused
    ? '<button id="resume-arcs-job" class="chat-job-action resume" type="button"><span aria-hidden="true">▶</span>Resume</button>'
    : `<button id="pause-arcs-job" class="chat-job-action" type="button" ${(pausing || stopping) ? "disabled" : ""}><span aria-hidden="true">${pausing ? "…" : "Ⅱ"}</span>${pausing ? "Pausing" : "Pause"}</button>`;
  const stopAction = `<button id="stop-arcs-job" class="chat-job-action stop" type="button" ${stopping ? "disabled" : ""}><span aria-hidden="true">■</span>${stopping ? "Stopping" : "Stop"}</button>`;
  const promptAction = Number(job.prompt_count || 0) > 0 ? `<button id="show-arcs-prompt" class="chat-job-action prompt" type="button">Prompt · ${Number(job.prompt_count)}</button>` : "";
  const meta = stopping
    ? (serialRefine ? `Ending adjustment · completed ${completed}` : `Keeping ${completed} completed story arcs`)
    : paused
    ? (serialRefine ? `Serial adjustment paused · completed ${completed} / ${total || "—"}` : `Stopped at ${completed} / ${total || "—"} · completed content saved`)
    : pausing
      ? (serialRefine ? `Pausing the current adjustment request · ${completed} / ${total || "—"}` : `Paused after saving the current unit · ${completed} / ${total || "—"}`)
      : routing
        ? "Finding the earliest affected story arc"
      : serialRefine
        ? `${completed} / ${total || "—"} units to adjust · ${percent}%`
      : total > 0
        ? `${completed} / ${total} story arcs · ${percent}%`
        : "Analyzing generation range";
  return `<div class="chat-job-progress ${paused ? "is-paused" : pausing ? "is-pausing" : stopping ? "is-stopping" : ""} ${routing ? "is-refining" : ""}" id="arcs-job-progress">
    <div class="chat-job-progress-main">
      <span class="chat-job-status-dot" aria-hidden="true"></span>
      <div class="chat-job-progress-copy">
        <strong>${escapeHtml(job.message || "Generating")}</strong>
        <span>${meta}</span>
      </div>
      <div class="chat-job-actions">${promptAction}${pauseAction}${stopAction}</div>
    </div>
    <div class="chat-job-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" ${routing ? 'aria-label="Analyzing adjustment range"' : `aria-valuenow="${percent}"`}><i style="width:${Math.max(2, Math.min(100, percent))}%"></i></div>
  </div>`;
}

function arcsChatPanelMarkup(volume, conversation, job = null) {
  const turns = (conversation && Array.isArray(conversation.turns)) ? conversation.turns : [];
  const sd = wizardState.summary?.story_design || {};
  const stageCount = Math.max(0, Number(sd.stage_count || 0));
  const selector = stageCount
    ? `<select id="arcs-volume-select">${Array.from({ length: stageCount }, (_, i) => `<option value="${i + 1}" ${Number(volume) === i + 1 ? "selected" : ""}>Stage ${i + 1}</option>`).join("")}</select>`
    : `<input id="arcs-volume-select" type="number" min="1" value="${volume}" />`;
  const volumeInfo = (wizardState.summary?.volumes || []).find((item) => Number(item.volume) === Number(volume));
  const arcsExist = Boolean(conversation?.has_arcs) || Boolean(volumeInfo?.arcs?.length);
  const placeholder = "Describe story-arc changes, for example “add a reversal in Arc 1” or “the protagonist breaks through in Arc 3”…";
  const messages = turns.map(chatMessageMarkup).join("");
  const resetBtn = (turns.length || arcsExist) ? '<button id="reset-arcs-chat" class="chat-icon-btn" type="button" title="Delete story arcs for this volume and start over">⟳ Reset</button>' : "";
  const emptyHint = arcsExist ? "Choose a stage, then enter adjustment requests below. On first visit, pick a stage and describe what you need." : "After choosing a stage, enter plot inspiration or requirements to generate story arcs.";
  return `<section class="chat-panel" id="arcs-chat" data-volume="${volume}">
    <header class="chat-panel-bar"><span class="chat-panel-bar-label">Stage / volume</span>${selector}</header>
    <div class="chat-scroll" id="chat-message-list">${messages || `<div class="chat-empty"><div class="chat-empty-icon">📖</div><p>${emptyHint}</p></div>`}</div>
    ${arcsJobMarkup(job)}
    <div class="chat-composer">
      <div class="chat-input-row">
        <textarea id="arcs-chat-input" class="chat-input" placeholder="${placeholder}" rows="1"></textarea>
        <button id="send-arcs-chat" class="chat-send-btn" type="button" title="Send (Ctrl/⌘+Enter)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button>
      </div>
      <div class="chat-composer-meta">${resetBtn}</div>
    </div>
  </section>`;
}

function renderArcsChat(volume, conversation, job = null) {
  const node = $("#arcs-chat-host");
  if (!node) return;
  node.innerHTML = arcsChatPanelMarkup(volume, conversation, job);
  const list = $("#chat-message-list");
  if (list) list.scrollTop = list.scrollHeight;
  $("#arcs-volume-select")?.addEventListener("change", () => {
    wizardState.arcsChatVolume = Number($("#arcs-volume-select").value) || 1;
    loadArcsChat(wizardState.arcsChatVolume);
  });
  $("#send-arcs-chat")?.addEventListener("click", () => sendArcsMessage(volume));
  const jobActive = job && ["running", "pausing", "paused", "stopping"].includes(job.status);
  if (jobActive) {
    $("#send-arcs-chat").disabled = true;
    $("#arcs-chat-input").disabled = true;
  }
  $("#pause-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "pause"));
  $("#resume-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "resume"));
  $("#stop-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "stop"));
  $("#continue-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "continue"));
  const chatInput = $("#arcs-chat-input");
  const autoGrow = () => { if (chatInput) { chatInput.style.height = "auto"; chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px"; } };
  chatInput?.addEventListener("input", autoGrow);
  chatInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); sendArcsMessage(volume); }
  });
  $$("[data-artifact-path]").forEach((btn) => btn.addEventListener("click", () => openReviewFile(btn.dataset.artifactPath)));
  $("#reset-arcs-chat")?.addEventListener("click", async () => {
    if (!confirm(`This will delete all story arcs for stage ${volume} and clear the conversation. Reset?`)) return;
    try {
      await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}/reset`, { method: "POST", body: JSON.stringify({}) });
      await refreshWorkspaceArtifacts();
      const data = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}/conversation`);
      renderArcsChat(volume, data);
      showToast("Reset. The next message will generate again.");
    } catch (error) { showToast(error.message || "Could not reset.", true); }
  });
}

async function loadArcsChat(volume) {
  try {
    const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}`;
    const [data, job] = await Promise.all([api(`${base}/conversation`), api(`${base}/job`)]);
    renderArcsChat(volume, data, job);
    if (["running", "pausing", "paused", "stopping"].includes(job.status)) pollArcsJob(volume);
  } catch (_) { /* ignore */ }
}

let arcsJobPollTimer = null;

async function controlArcsJob(volume, action) {
  try {
    const job = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}/${action}`, {
      method: "POST", body: JSON.stringify({}),
    });
    const conversation = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}/conversation`);
    renderArcsChat(volume, conversation, job);
    pollArcsJob(volume);
  } catch (error) {
    if (action === "stop" && error.status === 404) {
      showToast("The backend is still an old version. Restart novel web, then click Stop. Generated content will not be lost.", true);
    } else {
      showToast(error.message || "Could not control the generation task.", true);
    }
  }
}

function pollArcsJob(volume) {
  if (arcsJobPollTimer) clearTimeout(arcsJobPollTimer);
  const poll = async () => {
    if (Number(wizardState.arcsChatVolume || volume) !== Number(volume)) return;
    try {
      const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}`;
      const job = await api(`${base}/job`);
      const progress = $("#arcs-job-progress");
      if (["running", "pausing", "paused", "stopping"].includes(job.status)) {
        const completed = Number(job.completed || 0);
        const lastCompleted = Number(wizardState.arcsJobCompleted[volume] || 0);
        if (completed > lastCompleted) {
          wizardState.arcsJobCompleted[volume] = completed;
          await refreshReviewArtifactsOnly(job.progress_kind === "serial_refine");
        }
        if (progress) {
          const holder = document.createElement("div");
          holder.innerHTML = arcsJobMarkup(job);
          progress.replaceWith(holder.firstElementChild);
          $("#pause-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "pause"));
          $("#resume-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "resume"));
          $("#stop-arcs-job")?.addEventListener("click", () => controlArcsJob(volume, "stop"));
        } else {
          const conversation = await api(`${base}/conversation`);
          renderArcsChat(volume, conversation, job);
        }
        arcsJobPollTimer = setTimeout(poll, 900);
        return;
      }
      const conversation = await api(`${base}/conversation`);
      await refreshWorkspaceArtifacts();
      renderArcsChat(volume, conversation, job);
      if (job.status === "failed") showToast(job.error || "Generation failed. Please retry.", true);
      else if (job.status === "stopped") showToast("This generation round has ended. Completed content was kept.");
      else if (job.status === "completed") showToast("Story-arc generation completed.");
    } catch (error) {
      arcsJobPollTimer = setTimeout(poll, 1500);
    }
  };
  poll();
}

async function sendArcsMessage(volume) {
  const input = $("#arcs-chat-input");
  const message = (input?.value || "").trim();
  if (!message) return;
  const button = $("#send-arcs-chat");
  if (button) button.disabled = true;
  if (input) input.disabled = true;
  const list = $("#chat-message-list");
  const empty = list?.querySelector(".chat-empty");
  if (empty) empty.remove();
  if (list) {
    const li = document.createElement("li");
    li.className = "chat-message user";
    li.innerHTML = `<div class="chat-message-body">${escapeHtml(message)}</div>`;
    list.appendChild(li);
    const typing = document.createElement("li");
    typing.className = "chat-message assistant typing";
    typing.id = "chat-typing";
    typing.innerHTML = `<div class="chat-message-avatar">AI</div><div class="chat-message-content"><div class="chat-typing-dots"><span></span><span></span><span></span></div></div>`;
    list.appendChild(typing);
    list.scrollTop = list.scrollHeight;
  }
  if (input) input.value = "";
  let started = false;
  try {
    const job = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}/chat`, {
      method: "POST", body: JSON.stringify({ message }),
    });
    wizardState.arcsJobCompleted[volume] = Number(job.completed || 0);
    $("#chat-typing")?.remove();
    const conversation = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/arcs/${volume}/conversation`);
    renderArcsChat(volume, conversation, job);
    pollArcsJob(volume);
  } catch (error) {
    showToast(error.message || "Generation failed. Please retry.", true);
    loadArcsChat(volume);
  } finally {}
}


function chaptersVolumeDetails() {
  const volumes = wizardState.summary?.volumes || [];
  return volumes;
}

function chaptersJobMarkup(job) {
  if (!job) return "";
  if (job.status === "idle" && job.can_resume) {
    const completed = Number(job.completed || 0), total = Number(job.total || 0);
    const percent = total ? Math.round(completed * 100 / total) : 0;
    return `<div class="chat-job-progress is-interrupted" id="chapters-job-progress">
      <div class="chat-job-progress-main"><span class="chat-job-status-dot"></span>
        <div class="chat-job-progress-copy"><strong>Last run stopped before chapter ${Number(job.next_chapter || completed + 1)}</strong><span>Kept ${completed} / ${total} chapters</span></div>
        <button id="continue-chapters-job" class="chat-job-action resume continue" type="button"><span>▶</span>Continue generating</button>
      </div><div class="chat-job-progress-track"><i style="width:${percent}%"></i></div>
    </div>`;
  }
  if (["idle", "completed", "failed", "stopped"].includes(job.status)) return "";
  const total = Number(job.total || 0), completed = Number(job.completed || 0);
  const refining = job.progress_kind === "serial_chapter_refine";
  const routing = refining && job.phase === "routing";
  const percent = total ? Math.round(completed * 100 / total) : 4;
  const paused = job.status === "paused", pausing = job.status === "pausing", stopping = job.status === "stopping";
  const pauseAction = paused
    ? '<button id="resume-chapters-job" class="chat-job-action resume" type="button"><span>▶</span>Resume</button>'
    : `<button id="pause-chapters-job" class="chat-job-action" type="button" ${(pausing || stopping) ? "disabled" : ""}><span>${pausing ? "…" : "Ⅱ"}</span>${pausing ? "Pausing" : "Pause"}</button>`;
  const stopAction = `<button id="stop-chapters-job" class="chat-job-action stop" type="button" ${stopping ? "disabled" : ""}><span>■</span>${stopping ? "Stopping" : "Stop"}</button>`;
  const promptAction = Number(job.prompt_count || 0) > 0 ? `<button id="show-chapters-prompt" class="chat-job-action prompt" type="button">Prompt · ${Number(job.prompt_count)}</button>` : "";
  const meta = stopping ? `Ending · completed ${completed} chapters`
    : paused ? `Paused · ${completed} / ${total || "—"} chapters`
    : pausing ? `Pausing the current request · ${completed} / ${total || "—"}`
    : routing ? "Finding the earliest affected chapter"
    : refining ? `${completed} / ${total || "—"} chapters to adjust · ${percent}%`
    : `${completed} / ${total || "—"} chapters · ${percent}%`;
  return `<div class="chat-job-progress ${paused ? "is-paused" : pausing ? "is-pausing" : stopping ? "is-stopping" : ""} ${routing ? "is-refining" : ""}" id="chapters-job-progress">
    <div class="chat-job-progress-main"><span class="chat-job-status-dot"></span>
      <div class="chat-job-progress-copy"><strong>${escapeHtml(job.message || "Generating chapter outlines")}</strong><span>${meta}</span></div>
      <div class="chat-job-actions">${promptAction}${pauseAction}${stopAction}</div>
    </div><div class="chat-job-progress-track"><i style="width:${Math.max(2, Math.min(100, percent))}%"></i></div>
  </div>`;
}

function chaptersChatPanelMarkup(volume, arcIdx, conversation, job = null) {
  const volumes = chaptersVolumeDetails();
  const volDetail = volumes.find((v) => v.volume === Number(volume)) || { arcs: [] };
  const arcs = volDetail.arcs || [];
  const volumeSelector = volumes.length
    ? `<select id="chapters-volume-select">${volumes.map((v) => `<option value="${v.volume}" ${Number(volume) === v.volume ? "selected" : ""}>Stage ${v.volume}</option>`).join("")}</select>`
    : `<input id="chapters-volume-select" type="number" min="1" value="${volume}" />`;
  const arcSelector = arcs.length
    ? `<select id="chapters-arc-select">${arcs.map((a) => `<option value="${a.idx}" ${Number(arcIdx) === a.idx ? "selected" : ""}>Arc ${a.idx}${a.title ? ` · ${escapeHtml(a.title)}` : ""} (chapters ${a.start_ch}-${a.end_ch})</option>`).join("")}</select>`
    : `<select id="chapters-arc-select" disabled><option>No story arcs on this stage</option></select>`;
  const turns = (conversation && Array.isArray(conversation.turns)) ? conversation.turns : [];
  const placeholder = "Describe chapter-outline changes, for example “make Chapter 1 more oppressive” or “strengthen the reversal in the Chapter 3 synopsis”…";
  const messages = turns.map(chatMessageMarkup).join("");
  const resetBtn = (turns.length || conversation?.has_outlines) ? '<button id="reset-chapters-chat" class="chat-icon-btn" type="button" title="Delete this batch of chapter outlines and system panels and start over">⟳ Reset</button>' : "";
  const emptyHint = arcs.length ? "Choose a stage and story arc, then enter a description to generate chapter outlines." : "This stage has no story arcs yet. Generate them in the Story arcs step first.";
  const panel = wizardState.systemPanelStatus || { selection_mode: "auto", decided: false, enabled: false };
  const panelResult = panel.unavailable
    ? "Settings API is not loaded yet; restart the server to use it"
    : panel.selection_mode === "auto"
    ? (panel.decided ? `Auto-detect result: ${panel.enabled ? "system panel needed" : "system panel not needed"}` : "Auto-detect on first chapter-outline generation")
    : (panel.enabled ? "Manually enabled; protagonist state will update each chapter" : "Off; chapter system panels will not be generated");
  return `<section class="chat-panel" id="chapters-chat" data-volume="${volume}" data-arc="${arcIdx}">
    <header class="chat-panel-bar">
      <span class="chat-panel-bar-label">Stage / volume</span>${volumeSelector}
      <span class="chat-panel-bar-label">Story arc</span>${arcSelector}
    </header>
    <div class="system-panel-config-bar">
      <div><strong>System panel</strong><span>${escapeHtml(panelResult)}</span></div>
      <select id="chapter-system-panel-mode" aria-label="System-panel mode" ${panel.unavailable ? "disabled" : ""}>
        <option value="auto" ${panel.selection_mode === "auto" ? "selected" : ""}>Auto-detect</option>
        <option value="enabled" ${panel.selection_mode === "enabled" ? "selected" : ""}>Enable</option>
        <option value="disabled" ${panel.selection_mode === "disabled" ? "selected" : ""}>Don't use</option>
      </select>
    </div>
    <div class="chat-scroll" id="chat-message-list">${messages || `<div class="chat-empty"><div class="chat-empty-icon">📝</div><p>${emptyHint}</p></div>`}</div>
    ${chaptersJobMarkup(job)}
    <div class="chat-composer">
      <div class="chat-input-row">
        <textarea id="chapters-chat-input" class="chat-input" placeholder="${placeholder}" rows="1"></textarea>
        <button id="send-chapters-chat" class="chat-send-btn" type="button" title="Send (Ctrl/⌘+Enter)" ${arcs.length ? "" : "disabled"}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button>
      </div>
      <div class="chat-composer-meta">${resetBtn}</div>
    </div>
  </section>`;
}

function renderChaptersChat(volume, arcIdx, conversation, job = null) {
  const node = $("#chapters-chat-host");
  if (!node) return;
  node.innerHTML = chaptersChatPanelMarkup(volume, arcIdx, conversation, job);
  const list = $("#chat-message-list");
  if (list) list.scrollTop = list.scrollHeight;
  $("#chapters-volume-select")?.addEventListener("change", () => {
    wizardState.chaptersChatVolume = Number($("#chapters-volume-select").value) || 1;
    wizardState.chaptersChatArc = null;
    const volDetail = chaptersVolumeDetails().find((v) => v.volume === wizardState.chaptersChatVolume);
    const firstArc = volDetail?.arcs?.[0]?.idx;
    if (firstArc) {
      wizardState.chaptersChatArc = firstArc;
      loadChaptersChat(wizardState.chaptersChatVolume, firstArc);
    } else {
      renderChaptersChat(wizardState.chaptersChatVolume, 0, { turns: [] });
    }
  });
  $("#chapters-arc-select")?.addEventListener("change", () => {
    wizardState.chaptersChatArc = Number($("#chapters-arc-select").value) || 1;
    loadChaptersChat(wizardState.chaptersChatVolume, wizardState.chaptersChatArc);
  });
  $("#send-chapters-chat")?.addEventListener("click", () => sendChaptersMessage(volume, arcIdx));
  const jobActive = job && ["running", "pausing", "paused", "stopping"].includes(job.status);
  if (jobActive) {
    $("#send-chapters-chat").disabled = true;
    $("#chapters-chat-input").disabled = true;
    $("#chapters-volume-select").disabled = true;
    $("#chapters-arc-select").disabled = true;
    $("#chapter-system-panel-mode").disabled = true;
  }
  $("#chapter-system-panel-mode")?.addEventListener("change", async () => {
    try {
      const mode = $("#chapter-system-panel-mode").value;
      wizardState.systemPanelStatus = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/system-panel`, {
        method: "POST", body: JSON.stringify({ mode }),
      });
      showToast(mode === "auto" ? "Will auto-detect when chapter outlines are first generated." : mode === "enabled" ? "System panel enabled." : "System panel disabled. Existing state files are kept.");
      loadChaptersChat(volume, arcIdx);
    } catch (error) { showToast(error.message || "Could not update system-panel settings.", true); }
  });
  $("#pause-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "pause"));
  $("#resume-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "resume"));
  $("#stop-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "stop"));
  $("#continue-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "continue"));
  const chatInput = $("#chapters-chat-input");
  const autoGrow = () => { if (chatInput) { chatInput.style.height = "auto"; chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px"; } };
  chatInput?.addEventListener("input", autoGrow);
  chatInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); sendChaptersMessage(volume, arcIdx); }
  });
  $$("[data-artifact-path]").forEach((btn) => btn.addEventListener("click", () => openReviewFile(btn.dataset.artifactPath)));
  $("#reset-chapters-chat")?.addEventListener("click", async () => {
    if (!confirm(`This will delete all chapter outlines and matching system panels for Arc ${arcIdx} (volume ${volume}) and clear the conversation. Reset?`)) return;
    try {
      await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}/reset`, { method: "POST", body: JSON.stringify({}) });
      await refreshWorkspaceArtifacts();
      const data = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}/conversation`);
      renderChaptersChat(volume, arcIdx, data);
      showToast("Reset. The next message will generate again.");
    } catch (error) { showToast(error.message || "Could not reset.", true); }
  });
}

async function loadChaptersChat(volume, arcIdx) {
  if (!arcIdx) { renderChaptersChat(volume, 0, { turns: [] }); return; }
  try {
    const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}`;
    const [data, job] = await Promise.all([api(`${base}/conversation`), api(`${base}/job`)]);
    try {
      wizardState.systemPanelStatus = await api(
        `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/system-panel`,
      );
    } catch (_) {
      wizardState.systemPanelStatus = {
        selection_mode: "auto", decided: false, enabled: false, unavailable: true,
      };
    }
    renderChaptersChat(volume, arcIdx, data, job);
    if (["running", "pausing", "paused", "stopping"].includes(job.status)) pollChaptersJob(volume, arcIdx);
  } catch (error) {
    renderChaptersChat(volume, arcIdx, { turns: [] }, { status: "idle" });
    showToast(error.message || "Could not load the chapter-outline chat.", true);
  }
}

let chaptersJobPollTimer = null;

async function controlChaptersJob(volume, arcIdx, action) {
  try {
    const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}`;
    const job = await api(`${base}/${action}`, { method: "POST", body: JSON.stringify({}) });
    const conversation = await api(`${base}/conversation`);
    renderChaptersChat(volume, arcIdx, conversation, job);
    pollChaptersJob(volume, arcIdx);
  } catch (error) { showToast(error.message || "Could not control the chapter-outline task.", true); }
}

function pollChaptersJob(volume, arcIdx) {
  if (chaptersJobPollTimer) clearTimeout(chaptersJobPollTimer);
  const poll = async () => {
    if (Number(wizardState.chaptersChatVolume) !== Number(volume) || Number(wizardState.chaptersChatArc) !== Number(arcIdx)) return;
    try {
      const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}`;
      const job = await api(`${base}/job`);
      const key = `${volume}:${arcIdx}`;
      if (["running", "pausing", "paused", "stopping"].includes(job.status)) {
        const completed = Number(job.completed || 0);
        if (completed > Number(wizardState.chaptersJobCompleted[key] || 0)) {
          wizardState.chaptersJobCompleted[key] = completed;
          await refreshReviewArtifactsOnly(job.progress_kind === "serial_chapter_refine", "chapters");
        }
        const progress = $("#chapters-job-progress");
        if (progress) {
          const holder = document.createElement("div");
          holder.innerHTML = chaptersJobMarkup(job);
          progress.replaceWith(holder.firstElementChild);
          $("#pause-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "pause"));
          $("#resume-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "resume"));
          $("#stop-chapters-job")?.addEventListener("click", () => controlChaptersJob(volume, arcIdx, "stop"));
        } else {
          renderChaptersChat(volume, arcIdx, await api(`${base}/conversation`), job);
        }
        chaptersJobPollTimer = setTimeout(poll, 900);
        return;
      }
      await refreshWorkspaceArtifacts();
      renderChaptersChat(volume, arcIdx, await api(`${base}/conversation`), job);
      if (job.status === "failed") showToast(job.error || "Chapter-outline generation failed.", true);
      else if (job.status === "stopped") showToast("This chapter-outline round has ended. Completed content was kept.");
      else if (job.status === "completed") showToast("Chapter outlines generated.");
    } catch (_) { chaptersJobPollTimer = setTimeout(poll, 1500); }
  };
  poll();
}

async function sendChaptersMessage(volume, arcIdx) {
  const input = $("#chapters-chat-input");
  const message = (input?.value || "").trim();
  if (!message) return;
  const button = $("#send-chapters-chat");
  if (button) button.disabled = true;
  if (input) input.disabled = true;
  const list = $("#chat-message-list");
  const empty = list?.querySelector(".chat-empty");
  if (empty) empty.remove();
  if (list) {
    const li = document.createElement("li");
    li.className = "chat-message user";
    li.innerHTML = `<div class="chat-message-body">${escapeHtml(message)}</div>`;
    list.appendChild(li);
    const typing = document.createElement("li");
    typing.className = "chat-message assistant typing";
    typing.id = "chat-typing";
    typing.innerHTML = `<div class="chat-message-avatar">AI</div><div class="chat-message-content"><div class="chat-typing-dots"><span></span><span></span><span></span></div></div>`;
    list.appendChild(typing);
    list.scrollTop = list.scrollHeight;
  }
  if (input) input.value = "";
  try {
    const job = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}/chat`, {
      method: "POST", body: JSON.stringify({ message }),
    });
    wizardState.chaptersJobCompleted[`${volume}:${arcIdx}`] = Number(job.completed || 0);
    $("#chat-typing")?.remove();
    const conversation = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/chapters/${volume}/${arcIdx}/conversation`);
    renderChaptersChat(volume, arcIdx, conversation, job);
    pollChaptersJob(volume, arcIdx);
  } catch (error) {
    showToast(error.message || "Generation failed. Please retry.", true);
    loadChaptersChat(volume, arcIdx);
  } finally {}
}

function draftJobMarkup(job) {
  if (!job) return "";
  if (job.status === "idle" && job.can_resume) {
    return `<div class="chat-job-progress is-interrupted" id="draft-job-progress"><div class="chat-job-progress-main"><span class="chat-job-status-dot"></span><div class="chat-job-progress-copy"><strong>Last run stopped before chapter ${Number(job.next_chapter)}</strong><span>Kept ${Number(job.completed || 0)} / ${Number(job.total || 0)} draft chapters</span></div><button id="continue-draft-job" class="chat-job-action resume continue" type="button">▶ Continue generating</button></div></div>`;
  }
  if (["idle", "completed", "failed", "stopped"].includes(job.status)) return "";
  const total = Number(job.total || 0), completed = Number(job.completed || 0);
  const paused = job.status === "paused", stopping = job.status === "stopping";
  const routing = job.progress_kind === "serial_draft_refine" && job.phase === "routing";
  const pause = paused ? '<button id="resume-draft-job" class="chat-job-action resume" type="button">▶ Resume</button>' : '<button id="pause-draft-job" class="chat-job-action" type="button">Ⅱ Pause</button>';
  const promptAction = Number(job.prompt_count || 0) > 0 ? `<button id="show-draft-prompt" class="chat-job-action prompt" type="button">Prompt · ${Number(job.prompt_count)}</button>` : "";
  return `<div class="chat-job-progress ${paused ? "is-paused" : ""} ${routing ? "is-refining" : ""}" id="draft-job-progress"><div class="chat-job-progress-main"><span class="chat-job-status-dot"></span><div class="chat-job-progress-copy"><strong>${escapeHtml(job.message || "Generating draft")}</strong><span>${routing ? "Finding the earliest affected chapter" : `${completed} / ${total || "—"} chapters`}</span></div><div class="chat-job-actions">${promptAction}${pause}<button id="stop-draft-job" class="chat-job-action stop" type="button" ${stopping ? "disabled" : ""}>■ ${stopping ? "Stopping" : "Stop"}</button></div></div><div class="chat-job-progress-track"><i style="width:${total ? Math.round(completed * 100 / total) : 3}%"></i></div></div>`;
}

function draftChatPanelMarkup(volume, arcIdx, conversation, job = null) {
  const volumes = wizardState.summary?.volumes || [];
  const detail = volumes.find((item) => Number(item.volume) === Number(volume)) || { arcs: [] };
  const arcs = detail.arcs || [], turns = Array.isArray(conversation?.turns) ? conversation.turns : [];
  const guide = conversation?.writing_guide || {};
  const resetBtn = (turns.length || conversation?.has_drafts)
    ? '<button id="reset-draft-chat" class="chat-icon-btn" type="button" title="Delete all draft for the current story arc and start over">⟳ Reset</button>'
    : "";
  const volumeSelector = `<select id="draft-chat-volume">${volumes.map((item) => `<option value="${item.volume}" ${Number(volume) === Number(item.volume) ? "selected" : ""}>Stage / volume ${item.volume}</option>`).join("")}</select>`;
  const arcSelector = arcs.length ? `<select id="draft-chat-arc">${arcs.map((arc) => `<option value="${arc.idx}" ${Number(arcIdx) === Number(arc.idx) ? "selected" : ""}>Arc ${arc.idx}${arc.title ? ` · ${escapeHtml(arc.title)}` : ""} (chapters ${arc.start_ch}-${arc.end_ch})</option>`).join("")}</select>` : '<select id="draft-chat-arc" disabled><option>No story arcs on this stage</option></select>';
  return `<section class="chat-panel draft-chat-panel"><header class="chat-panel-bar"><span class="chat-panel-bar-label">Stage / volume</span>${volumeSelector}<span class="chat-panel-bar-label">Story arcs</span>${arcSelector}</header>
    <div class="writing-guide-bar"><div><strong>Writing guide</strong><span>${guide.custom ? "Using a custom writing guide" : "Using the project default system_prompt.md"}</span></div><div class="writing-guide-actions"><input id="draft-guide-file" type="file" accept=".txt,.md" hidden><button id="upload-draft-guide" class="chat-icon-btn" type="button">Upload guide</button>${guide.custom ? '<button id="reset-draft-guide" class="chat-icon-btn" type="button">Restore default</button>' : ""}</div></div>
    <div class="chat-scroll" id="chat-message-list">${turns.map(chatMessageMarkup).join("") || `<div class="chat-empty"><div class="chat-empty-icon">✍</div><p>${arcs.length ? "Enter draft requirements for this arc to start serial chapter writing." : "Generate story arcs and chapter outlines first."}</p></div>`}</div>${draftJobMarkup(job)}
    <div class="chat-composer"><div class="chat-input-row"><textarea id="draft-chat-input" class="chat-input" rows="1" placeholder="Enter draft generation or adjustment requirements"></textarea><button id="send-draft-chat" class="chat-send-btn" type="button" title="Send (Ctrl/⌘+Enter)" aria-label="Send" ${arcs.length ? "" : "disabled"}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button></div><div class="chat-composer-meta draft-chat-options"><label class="draft-humanize-option"><input id="draft-chat-humanize" type="checkbox" ${wizardState.draftChatHumanize === false ? "" : "checked"} /><span>Humanize after generation</span></label>${resetBtn}</div></div></section>`;
}

function renderDraftChat(volume, arcIdx, conversation, job = null) {
  const host = $("#draft-chat-host"); if (!host) return;
  host.innerHTML = draftChatPanelMarkup(volume, arcIdx, conversation, job);
  const active = job && ["running", "pausing", "paused", "stopping"].includes(job.status);
  if (active) ["#draft-chat-volume", "#draft-chat-arc", "#draft-chat-input", "#draft-chat-humanize", "#send-draft-chat"].forEach((s) => { if ($(s)) $(s).disabled = true; });
  $("#draft-chat-volume")?.addEventListener("change", () => { wizardState.draftChatVolume = Number($("#draft-chat-volume").value); const d = (wizardState.summary?.volumes || []).find((v) => Number(v.volume) === wizardState.draftChatVolume); wizardState.draftChatArc = d?.arcs?.[0]?.idx || null; loadDraftChat(wizardState.draftChatVolume, wizardState.draftChatArc); });
  $("#draft-chat-arc")?.addEventListener("change", () => { wizardState.draftChatArc = Number($("#draft-chat-arc").value); loadDraftChat(volume, wizardState.draftChatArc); });
  $("#send-draft-chat")?.addEventListener("click", () => sendDraftMessage(volume, arcIdx));
  $("#draft-chat-humanize")?.addEventListener("change", () => { wizardState.draftChatHumanize = Boolean($("#draft-chat-humanize")?.checked); });
  $("#draft-chat-input")?.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendDraftMessage(volume, arcIdx); } });
  for (const action of ["pause", "resume", "stop", "continue"]) $(`#${action}-draft-job`)?.addEventListener("click", () => controlDraftJob(volume, arcIdx, action));
  $("#upload-draft-guide")?.addEventListener("click", () => $("#draft-guide-file")?.click());
  $("#draft-guide-file")?.addEventListener("change", async () => { const file = $("#draft-guide-file")?.files?.[0]; if (!file) return; try { const upload = await uploadFile(file); await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/writing-guide`, { method: "POST", body: JSON.stringify({ upload_id: upload.id }) }); showToast("Custom writing guide saved."); loadDraftChat(volume, arcIdx); } catch (e) { showToast(e.message || "Could not save the writing guide.", true); } });
  $("#reset-draft-guide")?.addEventListener("click", async () => { await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/writing-guide`, { method: "DELETE" }); showToast("Restored the project default system_prompt.md."); loadDraftChat(volume, arcIdx); });
  $("#reset-draft-chat")?.addEventListener("click", async () => {
    const volumeDetail = (wizardState.summary?.volumes || []).find(
      (item) => Number(item.volume) === Number(volume),
    );
    const selectedArc = (volumeDetail?.arcs || []).find(
      (item) => Number(item.idx) === Number(arcIdx),
    );
    const chapterRange = selectedArc
      ? `Chapters ${selectedArc.start_ch}-${selectedArc.end_ch}`
      : "Current story arc";
    if (!confirm(`This will delete raw draft, refined draft, history versions, and finalized markers for ${chapterRange}. Reset?`)) return;
    try {
      await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/${volume}/${arcIdx}/reset`, {
        method: "POST", body: JSON.stringify({}),
      });
      const progressKey = `${volume}:${arcIdx}`;
      wizardState.draftJobCompleted[progressKey] = 0;
      delete wizardState.draftJobIds[progressKey];
      await refreshWorkspaceArtifacts();
      await loadDraftChat(volume, arcIdx);
      showToast("Draft for the current story arc has been reset.");
    } catch (error) {
      showToast(error.message || "Could not reset the draft.", true);
    }
  });
  $$("[data-artifact-path]").forEach((button) => button.addEventListener("click", () => openReviewFile(button.dataset.artifactPath)));
}

let draftJobPollTimer = null;
async function loadDraftChat(volume, arcIdx) {
  if (!arcIdx) {
    try {
      const guide = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/writing-guide`);
      renderDraftChat(volume, 0, { turns: [], writing_guide: guide });
    } catch (_) { renderDraftChat(volume, 0, { turns: [], writing_guide: { custom: false } }); }
    return;
  }
  try { const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/${volume}/${arcIdx}`; const [conversation, job] = await Promise.all([api(`${base}/conversation`), api(`${base}/job`)]); renderDraftChat(volume, arcIdx, conversation, job); if (["running", "pausing", "paused", "stopping"].includes(job.status)) pollDraftJob(volume, arcIdx); } catch (e) { showToast(e.message || "Could not load the draft chat.", true); }
}
async function sendDraftMessage(volume, arcIdx) {
  const message = ($("#draft-chat-input")?.value || "").trim(); if (!message) return;
  try { const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/${volume}/${arcIdx}`; const humanize = $("#draft-chat-humanize")?.checked !== false; wizardState.draftChatHumanize = humanize; const job = await api(`${base}/chat`, { method: "POST", body: JSON.stringify({ message, humanize }) }); const key = `${volume}:${arcIdx}`; wizardState.draftJobCompleted[key] = 0; wizardState.draftJobIds[key] = job.id || ""; renderDraftChat(volume, arcIdx, await api(`${base}/conversation`), job); pollDraftJob(volume, arcIdx); } catch (e) { showToast(e.message || "Could not start draft generation.", true); }
}
async function controlDraftJob(volume, arcIdx, action) {
  try { const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/${volume}/${arcIdx}`; const job = await api(`${base}/${action}`, { method: "POST", body: JSON.stringify({}) }); renderDraftChat(volume, arcIdx, await api(`${base}/conversation`), job); pollDraftJob(volume, arcIdx); } catch (e) { showToast(e.message || "Could not control the draft task.", true); }
}
function pollDraftJob(volume, arcIdx) {
  if (draftJobPollTimer) clearTimeout(draftJobPollTimer);
  const poll = async () => { if (Number(wizardState.draftChatVolume) !== Number(volume) || Number(wizardState.draftChatArc) !== Number(arcIdx)) return; const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/drafts/${volume}/${arcIdx}`; try { const job = await api(`${base}/job`); if (["running", "pausing", "paused", "stopping"].includes(job.status)) { const key = `${volume}:${arcIdx}`, jobId = job.id || "", done = Number(job.completed || 0); if (jobId && wizardState.draftJobIds[key] !== jobId) { wizardState.draftJobIds[key] = jobId; wizardState.draftJobCompleted[key] = 0; } if (done > Number(wizardState.draftJobCompleted[key] || 0)) { wizardState.draftJobCompleted[key] = done; const refining = job.progress_kind === "serial_draft_refine"; await refreshReviewArtifactsOnly(refining, "draft", !refining); } const progress = $("#draft-job-progress"); if (progress) { const holder = document.createElement("div"); holder.innerHTML = draftJobMarkup(job); progress.replaceWith(holder.firstElementChild); for (const action of ["pause", "resume", "stop"]) $(`#${action}-draft-job`)?.addEventListener("click", () => controlDraftJob(volume, arcIdx, action)); } else { renderDraftChat(volume, arcIdx, await api(`${base}/conversation`), job); } draftJobPollTimer = setTimeout(poll, 1000); return; } await refreshWorkspaceArtifacts(); renderDraftChat(volume, arcIdx, await api(`${base}/conversation`), job); if (job.status === "failed") showToast(job.error || "Draft generation failed.", true); else if (job.status === "stopped") showToast("This draft round has ended."); else if (job.status === "completed") showToast("Draft generation completed."); } catch (_) { draftJobPollTimer = setTimeout(poll, 1500); } }; poll();
}

function chatArtifactCards(artifacts) {
  if (!Array.isArray(artifacts) || !artifacts.length) return "";
  const cards = artifacts.map((item) => {
    const name = escapeHtml((item.path || "").split("/").pop() || "file");
    const label = escapeHtml(item.label || name);
    const path = escapeHtml(item.path || "");
    return `<button class="chat-artifact-card" data-artifact-path="${path}" type="button"><span class="chat-artifact-card-icon">📄</span><span class="chat-artifact-card-label">${label}</span><span class="chat-artifact-card-name">${name}</span></button>`;
  }).join("");
  return `<div class="chat-artifacts">${cards}</div>`;
}

function chatMessageMarkup(turn) {
  const isUser = turn.role === "user";
  if (isUser) {
    return `<li class="chat-message user"><div class="chat-message-body">${escapeHtml(turn.content || "")}</div></li>`;
  }
  const artCards = chatArtifactCards(turn.artifacts);
  return `<li class="chat-message assistant"><div class="chat-message-avatar">AI</div><div class="chat-message-content"><div class="chat-message-body">${escapeHtml(turn.content || "")}</div>${artCards}</div></li>`;
}

function designJobMarkup(job) {
  if (job && ["idle", "stopped", "failed"].includes(job.status) && job.can_resume) {
    const completed = Number(job.completed || 0);
    const total = Math.max(1, Number(job.total || 1));
    const percent = Math.max(0, Math.min(100, Math.round(completed * 100 / total)));
    return `<div class="chat-job-progress is-interrupted" id="design-job-progress">
      <div class="chat-job-progress-main">
        <span class="chat-job-status-dot" aria-hidden="true"></span>
        <div class="chat-job-progress-copy">
          <strong>Stage design is not finished</strong>
          <span>Kept ${completed} / ${total} stages</span>
        </div>
        <button id="continue-design-job" class="chat-job-action resume continue" type="button"><span>▶</span>Continue generating</button>
      </div>
      <div class="chat-job-progress-track"><i style="width:${percent}%"></i></div>
    </div>`;
  }
  if (!job || !["queued", "running", "pausing", "paused", "stopping"].includes(job.status)) return "";
  const completed = Number(job.completed || 0);
  const total = Math.max(1, Number(job.total || 1));
  const percent = Math.max(3, Math.min(100, Math.round(completed * 100 / total)));
  const paused = job.status === "paused";
  const pausing = job.status === "pausing";
  const stopping = job.status === "stopping";
  const promptAction = Number(job.prompt_count || 0) > 0
    ? `<button id="show-design-prompt" class="chat-job-action prompt" type="button">Prompt · ${Number(job.prompt_count)}</button>`
    : "";
  const stageActions = job.progress_kind === "stage_design"
    ? `<div class="chat-job-actions">${promptAction}${paused
        ? '<button id="resume-design-job" class="chat-job-action resume" type="button"><span>▶</span>Resume</button>'
        : `<button id="pause-design-job" class="chat-job-action" type="button" ${(pausing || stopping) ? "disabled" : ""}><span>${pausing ? "…" : "Ⅱ"}</span>${pausing ? "Pausing" : "Pause"}</button>`}
       <button id="stop-design-job" class="chat-job-action stop" type="button" ${stopping ? "disabled" : ""}><span>■</span>${stopping ? "Stopping" : "Stop"}</button></div>`
    : (promptAction ? `<div class="chat-job-actions">${promptAction}</div>` : "");
  const progressMeta = job.progress_kind === "design_concept"
    ? `${completed} / ${total} design items · ${Math.round(completed * 100 / total)}%`
    : stopping ? `Ending · completed ${completed} / ${total} stages`
    : paused ? `Paused · ${completed} / ${total} stages`
    : `${completed} / ${total} stages · ${Math.round(completed * 100 / total)}%`;
  return `<div class="chat-job-progress ${paused ? "is-paused" : pausing ? "is-pausing" : stopping ? "is-stopping" : ""}" id="design-job-progress">
    <div class="chat-job-progress-main">
      <span class="chat-job-status-dot" aria-hidden="true"></span>
      <div class="chat-job-progress-copy">
        <strong>${escapeHtml(job.message || "Generating book design")}</strong>
        <span>${progressMeta}</span>
      </div>
      ${stageActions}
    </div>
    <div class="chat-job-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(completed * 100 / total)}"><i style="width:${percent}%"></i></div>
  </div>`;
}

function designChatPanelMarkup(scope, conversation, job = null) {
  const turns = (conversation && Array.isArray(conversation.turns)) ? conversation.turns : [];
  const busy = Boolean(job && ["queued", "running", "pausing", "paused", "stopping"].includes(job.status));
  const sd = wizardState.summary?.story_design || {};
  const filesExist = scope === "concept" ? Boolean(sd.concept_ready) : Boolean(sd.stage_assets_exist ?? sd.stage_ready);
  const placeholder = filesExist
    ? "Describe what to change this round (the whole file is rewritten from the previous version; untouched parts are kept)…"
    : (scope === "concept" ? "Write the genre, protagonist, cheat, conflict, or any inspiration to generate the first draft…" : "Generate the long mainline and stage roadmap from the rough outline and worldview…");
  const messages = turns.map(chatMessageMarkup).join("");
  const resetBtn = filesExist && !busy ? '<button id="reset-design-chat" class="chat-icon-btn" type="button" title="Delete current artifacts and start over">⟳ Reset</button>' : "";
  const emptyHint = filesExist
    ? "First draft generated. Keep sending changes, for example “change the protagonist cheat to deduction” or “change Stage 1 to faction conflict”."
    : (scope === "concept" ? "Nothing here yet. Write your inspiration to generate the first rough outline and worldview." : "Nothing here yet. Write your ideas for the long mainline and stages to start generation.");
  const unusedReference = Number(sd.unused_reference_chapter_count || 0);
  const referenceOption = scope === "concept" && filesExist && unusedReference > 0
    ? `<label class="chat-reference-option">
        <input id="use-new-reference" type="checkbox" />
        <span class="chat-reference-switch" aria-hidden="true"><i></i></span>
        <span class="chat-reference-copy">
          <span><strong>Sync new deconstruction into the phase outline</strong><b>${unusedReference} chapters pending</b></span>
          <small>Only adjust the last phase, or append a phase when the reference novel adds a volume</small>
        </span>
      </label>`
    : "";
  const stageSyncOption = scope === "stage" && filesExist && Boolean(sd.stage_sync_pending)
    ? `<label class="chat-reference-option">
        <input id="sync-stage-design" type="checkbox" />
        <span class="chat-reference-switch" aria-hidden="true"><i></i></span>
        <span class="chat-reference-copy">
          <span><strong>Sync phase-outline changes</strong><b>Phase outline updated</b></span>
          <small>Only adjust the last stage, or append a stage when a new phase is added</small>
        </span>
      </label>`
    : "";
  const nameSynopsisAction = scope === "stage" && filesExist && !busy
    ? '<button id="refresh-name-synopsis" class="chat-icon-btn" type="button">Regenerate title and synopsis</button>'
    : "";
  return `<section class="chat-panel" id="design-chat" data-scope="${scope}">
    <div class="chat-scroll" id="chat-message-list">${messages || `<div class="chat-empty"><div class="chat-empty-icon">💬</div><p>${emptyHint}</p></div>`}</div>
    ${designJobMarkup(job)}
    <div class="chat-composer">
      <div class="chat-attachments" id="chat-attachments"></div>
      <div class="chat-input-row">
        <button class="chat-attach-button" id="chat-attach" type="button" title="Load a file as reference" aria-label="Load file" ${busy ? "disabled" : ""}><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
        <textarea id="chat-input" class="chat-input" placeholder="${placeholder}" rows="1" ${busy ? "disabled" : ""}></textarea>
        <button id="send-design-chat" class="chat-send-btn" type="button" title="Send (Ctrl/⌘+Enter)" ${busy ? "disabled" : ""}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button>
      </div>
      ${referenceOption}
      ${stageSyncOption}
      <div class="chat-composer-meta">${nameSynopsisAction}${resetBtn}</div>
    </div>
    <input id="chat-attach-input" type="file" multiple accept=".txt,.md,.json,.yaml,.yml,.csv" hidden />
  </section>`;
}

function chatAttachments(scope) {
  if (!wizardState.chatAttachments) wizardState.chatAttachments = {};
  if (!wizardState.chatAttachments[scope]) wizardState.chatAttachments[scope] = [];
  return wizardState.chatAttachments[scope];
}

function renderChatAttachments(scope) {
  const host = $("#chat-attachments");
  if (!host) return;
  const items = chatAttachments(scope);
  host.innerHTML = items.map((item, index) => `<span class="chat-attachment-chip">${escapeHtml(item.name)}<button type="button" class="chat-attachment-remove" data-remove-attachment="${index}" aria-label="Remove attachment">×</button></span>`).join("");
  host.classList.toggle("has-items", items.length > 0);
  $$("[data-remove-attachment]").forEach((btn) => btn.addEventListener("click", () => {
    chatAttachments(scope).splice(Number(btn.dataset.removeAttachment), 1);
    renderChatAttachments(scope);
  }));
}

function bindChatAttach(scope) {
  const picker = $("#chat-attach-input");
  $("#chat-attach")?.addEventListener("click", () => picker?.click());
  picker?.addEventListener("change", () => {
    const files = Array.from(picker.files || []);
    picker.value = "";
    if (!files.length) return;
    let pending = files.length;
    const done = () => { pending -= 1; if (pending === 0) renderChatAttachments(scope); };
    files.forEach((file) => {
      if (file.size > 1024 * 1024 * 2) { showToast(`“${file.name}” is over 2MB and was not loaded (trim it and retry).`, true); done(); return; }
      const reader = new FileReader();
      reader.onload = () => { chatAttachments(scope).push({ name: file.name, content: String(reader.result || "") }); done(); };
      reader.onerror = () => { showToast(`Could not read “${file.name}”.`, true); done(); };
      reader.readAsText(file, "utf-8");
    });
  });
}

function renderDesignChat(scope, conversation, job = null) {
  const node = $("#design-chat-host");
  if (!node) return;
  node.innerHTML = designChatPanelMarkup(scope, conversation, job);
  const list = $("#chat-message-list");
  if (list) list.scrollTop = list.scrollHeight;
  renderChatAttachments(scope);
  bindChatAttach(scope);
  $$("[data-artifact-path]").forEach((btn) => btn.addEventListener("click", () => openReviewFile(btn.dataset.artifactPath)));
  $("#send-design-chat")?.addEventListener("click", () => sendDesignMessage(scope));
  bindDesignJobControls(scope);
  $("#refresh-name-synopsis")?.addEventListener("click", async () => {
    try { await refreshNameSynopsis(); } catch (error) { showToast(error.message || "Could not generate title and synopsis.", true); }
  });
  const chatInput = $("#chat-input");
  const autoGrow = () => { if (chatInput) { chatInput.style.height = "auto"; chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px"; } };
  chatInput?.addEventListener("input", autoGrow);
  chatInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); sendDesignMessage(scope); }
  });
  $("#reset-design-chat")?.addEventListener("click", async () => {
    const msg = scope === "concept" ? "This will delete the current rough outline and worldview and clear the conversation. The next message will generate a first draft again. Reset?" : "This will delete the current long mainline, stage roadmap, title, and synopsis, and clear the conversation. The next message will generate a first draft again. Reset?";
    if (!confirm(msg)) return;
    try {
      await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}/reset`, { method: "POST", body: JSON.stringify({}) });
      wizardState.chatAttachments[scope] = [];
      await refreshWorkspaceArtifacts();
      const data = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}/conversation`);
      renderDesignChat(scope, data);
      showToast("Reset. The next message will generate a first draft again.");
    } catch (error) { showToast(error.message || "Could not reset.", true); }
  });
}

async function loadDesignChat(scope) {
  try {
    const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}`;
    const [data, job] = await Promise.all([api(`${base}/conversation`), api(`${base}/job`)]);
    renderDesignChat(scope, data, job);
    if (["queued", "running", "pausing", "paused", "stopping"].includes(job.status)) pollDesignJob(scope);
  } catch (_) { /* ignore */ }
}

let designJobPollTimer = null;

function bindDesignJobControls(scope) {
  $("#pause-design-job")?.addEventListener("click", () => controlDesignJob(scope, "pause"));
  $("#resume-design-job")?.addEventListener("click", () => controlDesignJob(scope, "resume"));
  $("#stop-design-job")?.addEventListener("click", () => controlDesignJob(scope, "stop"));
  $("#continue-design-job")?.addEventListener("click", () => controlDesignJob(scope, "continue"));
  $("#show-design-prompt")?.addEventListener("click", () => showJobPrompts(
    `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}/prompts`,
    scope === "concept" ? "Book design · model prompt" : "Stage design · model prompt",
  ));
}

async function controlDesignJob(scope, action) {
  try {
    const job = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}/${action}`, {
      method: "POST", body: JSON.stringify({}),
    });
    const progress = $("#design-job-progress");
    if (progress) {
      const holder = document.createElement("div");
      holder.innerHTML = designJobMarkup(job);
      progress.replaceWith(holder.firstElementChild);
      bindDesignJobControls(scope);
    }
    pollDesignJob(scope);
  } catch (error) {
    showToast(error.message || "Could not control the stage-design task.", true);
  }
}

function pollDesignJob(scope) {
  if (designJobPollTimer) clearTimeout(designJobPollTimer);
  const expectedStep = scope === "concept" ? "design" : "stage";
  const poll = async () => {
    if (wizardState.activeStep !== expectedStep) return;
    const base = `/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}`;
    try {
      const job = await api(`${base}/job`);
      if (["queued", "running", "pausing", "paused", "stopping"].includes(job.status)) {
        const completed = Number(job.completed || 0);
        const previous = Number(wizardState.designJobCompleted[scope] || 0);
        if (completed > previous) {
          wizardState.designJobCompleted[scope] = completed;
          await refreshReviewArtifactsOnly(false, expectedStep, true);
        }
        const progress = $("#design-job-progress");
        if (progress) {
          const holder = document.createElement("div");
          holder.innerHTML = designJobMarkup(job);
          progress.replaceWith(holder.firstElementChild);
          bindDesignJobControls(scope);
        } else {
          const conversation = await api(`${base}/conversation`);
          renderDesignChat(scope, conversation, job);
        }
        designJobPollTimer = setTimeout(poll, 900);
        return;
      }
      await refreshWorkspaceArtifacts();
      const conversation = await api(`${base}/conversation`);
      renderDesignChat(scope, conversation, job);
      if (job.status === "failed") showToast(job.error || "Book design failed. Please retry.", true);
      else if (job.status === "stopped") showToast("This stage-design round has ended. Completed content was kept.");
      else if (job.status === "completed") showToast(scope === "concept" ? "Book design generated." : "Stage design generated.");
    } catch (_) {
      designJobPollTimer = setTimeout(poll, 1500);
    }
  };
  poll();
}

async function sendDesignMessage(scope) {
  const input = $("#chat-input");
  const message = (input?.value || "").trim();
  const attachments = chatAttachments(scope).map((item) => ({ name: item.name, content: item.content }));
  const useNewReference = Boolean($("#use-new-reference")?.checked);
  const syncUpdatedDesign = Boolean($("#sync-stage-design")?.checked);
  if (!message && !attachments.length && !useNewReference && !syncUpdatedDesign) return;
  const button = $("#send-design-chat");
  const attachButton = $("#chat-attach");
  if (button) button.disabled = true;
  if (attachButton) attachButton.disabled = true;
  if (input) input.disabled = true;
  const list = $("#chat-message-list");
  const empty = list?.querySelector(".chat-empty");
  if (empty) empty.remove();
  if (list) {
    const li = document.createElement("li");
    li.className = "chat-message user";
    const preview = attachments.length ? `${message}\n(attachments: ${attachments.map((a) => a.name).join(", ")})` : message;
    li.innerHTML = `<div class="chat-message-body">${escapeHtml(preview)}</div>`;
    list.appendChild(li);
    const typing = document.createElement("li");
    typing.className = "chat-message assistant typing";
    typing.id = "chat-typing";
    typing.innerHTML = `<div class="chat-message-avatar">AI</div><div class="chat-message-content"><div class="chat-typing-dots"><span></span><span></span><span></span></div></div>`;
    list.appendChild(typing);
    list.scrollTop = list.scrollHeight;
  }
  if (input) input.value = "";
  try {
    const job = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/design/${scope}/chat`, {
      method: "POST", body: JSON.stringify({
        message,
        attachments,
        use_new_reference: useNewReference,
        sync_updated_design: syncUpdatedDesign,
      }),
    });
    wizardState.chatAttachments[scope] = [];
    $("#chat-typing")?.remove();
    wizardState.designJobCompleted[scope] = 0;
    const composer = $("#design-chat .chat-composer");
    composer?.insertAdjacentHTML("beforebegin", designJobMarkup(job));
    bindDesignJobControls(scope);
    started = true;
    pollDesignJob(scope);
  } catch (error) {
    const message = error.message || "Generation failed. Please retry.";
    const typing = $("#chat-typing");
    if (typing) {
      typing.classList.remove("typing");
      typing.classList.add("error");
      typing.innerHTML = `<div class="chat-message-avatar">!</div><div class="chat-message-content"><div class="chat-message-body">Generation failed: ${escapeHtml(message)}</div></div>`;
    }
    showToast(message, true);
  } finally {
    if (!started) {
      if (button) button.disabled = false;
      if (attachButton) attachButton.disabled = false;
      if (input) input.disabled = false;
    }
  }
}








function stageOptions(selected = 1, includeEnd = false) {
  const stageCount = Math.max(0, Number(wizardState.summary?.story_design?.stage_count || 0));
  if (!stageCount) return `<input id="stage-volume" type="number" min="1" value="${selected}" />`;
  const options = Array.from({ length: stageCount }, (_, index) => {
    const value = index + 1;
    return `<option value="${value}" ${value === Number(selected) ? "selected" : ""}>Stage / volume ${value}</option>`;
  });
  if (includeEnd) options.unshift('<option value="">Append at the end</option>');
  return `<select id="stage-volume">${options.join("")}</select>`;
}

function worldSources() {
  return wizardState.summary?.world_knowledge?.sources || [];
}

function worldForm() {
  const sources = worldSources();
  const worldReady = Boolean(wizardState.summary?.world_knowledge?.ready);
  const sourceList = sources.length
    ? `<div class="world-uploaded">
        <div class="world-uploaded-heading"><span>Uploaded</span><strong>${sources.length}  files</strong></div>
        <ul class="source-list">${sources.map((source) => `<li><strong>${escapeHtml(source.file_name)}</strong><span>${source.size ? `${Math.ceil(source.size / 1024).toLocaleString()} KB` : "Imported"}</span></li>`).join("")}</ul>
      </div>`
    : '<p class="reference-file-status">No target-world sources uploaded yet</p>';
  return `
    <div class="reference-source world-source-flat" id="world-source">
      ${sourceList}
      <label class="reference-file-picker" id="world-file-picker" for="world-file-input"><span id="world-file-label">${sources.length ? "Add more sources" : "Choose source files"}</span><input id="world-file-input" type="file" multiple accept=".txt,.md,.json,.yaml,.yml,.csv,.tsv" /><small id="world-file-help">Multiple files are allowed. The largest file becomes the primary source; others are supplement sources.</small></label>
      <p id="world-file-status" class="reference-file-status">No new file selected yet</p>
      <ul id="world-new-file-list" class="source-list world-new-file-list" hidden></ul>
    </div>
    ${sources.length ? `<div class="world-enable-row">
      <label class="world-toggle"><input id="world-enabled" type="checkbox" ${wizardState.summary?.world_knowledge?.enabled === false ? "" : "checked"} /><span class="world-toggle-text">Enable target-world knowledge base</span></label>
      <small>${worldReady ? "All 7 knowledge-base sections are built. Turn it off to stop injecting sources into later design; turn it on again to resume." : "Build starts automatically after import. If a task is interrupted, retry with the button below without uploading a new file."}</small>
    </div>` : ""}`;
}

function mechanicsForm() {
  const configured = wizardState.summary?.mechanics?.mode && wizardState.summary.mechanics.mode !== "Not initialized";
  const mode = wizardState.mechanicsMode === "none" ? "auto" : wizardState.mechanicsMode;
  wizardState.mechanicsMode = mode;
  return `<fieldset class="mechanics-source" id="mechanics-source"><legend>System-panel settings</legend>
    <div class="direction-source-switch" role="radiogroup" aria-label="System-panel setup method">
      <label class="direction-source-option ${mode === "auto" ? "active" : ""}"><input name="mechanics-mode" value="auto" type="radio" ${mode === "auto" ? "checked" : ""} />Auto-detect</label>
      <label class="direction-source-option ${mode === "text" ? "active" : ""}"><input name="mechanics-mode" value="text" type="radio" ${mode === "text" ? "checked" : ""} />Type directly</label>
      <label class="direction-source-option ${mode === "file" ? "active" : ""}"><input name="mechanics-mode" value="file" type="radio" ${mode === "file" ? "checked" : ""} />Read from file</label>
    </div>
    <p class="decision-note" data-mechanics-panel="auto" ${mode === "auto" ? "" : "hidden"}>Decide from core gameplay whether a system panel, numeric tracking, or light state constraints are needed.</p>
    <label data-mechanics-panel="text" ${mode === "text" ? "" : "hidden"}>System-panel settings<textarea id="mechanics-direction" placeholder="For example: merit points can be exchanged for deduction attempts, and upgrades consume fate shards."></textarea></label>
    <div class="direction-file-panel" data-mechanics-panel="file" ${mode === "file" ? "" : "hidden"}><label class="direction-file-picker" for="mechanics-file-input"><span>Choose a system-panel settings file</span><input id="mechanics-file-input" type="file" accept=".txt,.md,.json,.yaml,.yml" /><small>The system panel, numeric formulas, and resource rules are used as the initial settings.</small></label><p id="mechanics-file-status" class="direction-file-status">${wizardState.mechanicsFile ? `Selected: ${escapeHtml(wizardState.mechanicsFile.name)}` : "No file selected yet"}</p></div>
    ${configured ? '<label class="check-label"><input id="mechanics-force" type="checkbox" />Overwrite existing system-panel settings</label>' : ""}
  </fieldset>`;
}

function volumeForm(kind) {
  const stageCount = Number(wizardState.summary?.story_design?.stage_count || 0);
  const fieldId = `${kind}-volume`;
  const options = stageCount
    ? `<select id="${fieldId}">${Array.from({ length: stageCount }, (_, index) => `<option value="${index + 1}">Stage / volume ${index + 1}</option>`).join("")}</select>`
    : `<input id="${fieldId}" type="number" min="1" value="1" />`;
  return `<fieldset class="generation-options"><legend>Generation range</legend><label>Stage / volume${options}</label><label class="check-label"><input id="${kind}-force" type="checkbox" />Overwrite existing content in this volume</label></fieldset>`;
}

function draftForm() {
  const stageCount = Number(wizardState.summary?.story_design?.stage_count || 0);
  const volumeDetails = wizardState.summary?.volumes || [];
  const volumes = stageCount
    ? `<select id="draft-volume">${Array.from({ length: stageCount }, (_, index) => `<option value="${index + 1}">Stage / volume ${index + 1}</option>`).join("")}</select>`
    : '<input id="draft-volume" type="number" min="1" value="1" />';
  const firstVolume = volumeDetails.find((item) => Number(item.volume) === 1) || volumeDetails[0];
  const firstArcs = firstVolume?.arcs || [];
  const arcOptions = firstArcs.length
    ? firstArcs.map((arc) => `<option value="${arc.idx}" data-start="${arc.start_ch}" data-end="${arc.end_ch}">Arc ${arc.idx}${arc.title ? ` · ${escapeHtml(arc.title)}` : ""} (chapters ${arc.start_ch}-${arc.end_ch})</option>`).join("")
    : '<option value="">No story arcs on this stage</option>';
  const firstArc = firstArcs[0];
  const firstCount = firstArc ? firstArc.end_ch - firstArc.start_ch + 1 : "";
  return `<fieldset class="generation-options"><legend>Generation range</legend>
    <div class="inline-number-fields">
      <label>Stage / volume${volumes}</label>
      <label>Story arcs<select id="draft-arc" ${firstArcs.length ? "" : "disabled"}>${arcOptions}</select></label>
    </div>
    <div class="draft-range-row">
      <p id="draft-range-hint" class="decision-note">${firstArc ? `This run: chapters ${firstArc.start_ch}-${firstArc.end_ch}, ${firstCount} chapters.` : "Generate this stage's story arcs in the Story arcs step first."}</p>
      <label>Chapters to generate this run<input id="draft-max" type="number" min="1" ${firstCount ? `max="${firstCount}" value="${firstCount}"` : "disabled"} /></label>
    </div>
    <label class="check-label"><input id="draft-humanize" type="checkbox" checked />Humanize after generation</label>
    <label class="check-label"><input id="draft-humanize-existing" type="checkbox" />Only refine existing draft in the selected range (do not write new chapters)</label>
  </fieldset>`;
}

function bindDraftRange() {
  const volumeInput = $("#draft-volume");
  const arcSelect = $("#draft-arc");
  const maxInput = $("#draft-max");
  const hint = $("#draft-range-hint");
  if (!volumeInput || !arcSelect || !maxInput || !hint) return;

  const updateRange = () => {
    const option = arcSelect.selectedOptions?.[0];
    const start = Number(option?.dataset.start || 0);
    const end = Number(option?.dataset.end || 0);
    const count = start && end >= start ? end - start + 1 : 0;
    maxInput.disabled = !count;
    if (count) {
      maxInput.max = String(count);
      maxInput.value = String(count);
      hint.textContent = `This run: chapters ${start}-${end}, ${count} chapters.`;
    } else {
      maxInput.removeAttribute("max");
      maxInput.value = "";
      hint.textContent = "Generate this stage's story arcs in the Story arcs step first.";
    }
  };

  const updateArcs = () => {
    const volume = Number(volumeInput.value || 0);
    const detail = (wizardState.summary?.volumes || []).find((item) => Number(item.volume) === volume);
    const arcs = detail?.arcs || [];
    arcSelect.innerHTML = arcs.length
      ? arcs.map((arc) => `<option value="${arc.idx}" data-start="${arc.start_ch}" data-end="${arc.end_ch}">Arc ${arc.idx}${arc.title ? ` · ${escapeHtml(arc.title)}` : ""} (chapters ${arc.start_ch}-${arc.end_ch})</option>`).join("")
      : '<option value="">No story arcs on this stage</option>';
    arcSelect.disabled = !arcs.length;
    updateRange();
  };

  volumeInput.addEventListener("change", updateArcs);
  arcSelect.addEventListener("change", updateRange);
  updateArcs();
}

function formForStep(step) {
  if (step.id === "reference") {
    const reference = referenceStatus();
    if (reference.hasExisting) {
      const coverage = reference.total
        ? `${reference.processed} / ${reference.total} chapters`
        : `${reference.processed || reference.stagedChapters} chapters`;
      const defaultTarget = reference.total || Math.max(reference.processed, reference.stagedChapters, 200);
      const currentFile = escapeHtml((reference.source_name || "sample_novel.txt").replace(/^[0-9a-f]{16}_/i, ""));
      const selectedFile = wizardState.referenceFile;
      return `
        <div class="reference-source reference-existing" id="reference-source">
          <div class="reference-current-file">
            <span>Uploaded</span>
            <strong>${currentFile}</strong>
            <small>Deconstructed ${coverage}</small>
          </div>
          <label class="reference-file-picker" id="reference-file-picker" for="reference-file-input">
            <span id="reference-file-label">${selectedFile ? "New full-book novel selected" : "Choose the updated full novel"}</span>
            <input id="reference-file-input" type="file" accept=".txt,text/plain" />
            <small id="reference-file-help">${selectedFile ? "The system matches already deconstructed chapters and only deconstructs the new part." : "Already deconstructed chapters are skipped, and the ending story arcs are rechecked."}</small>
          </label>
          <p id="reference-file-status" class="reference-file-status">${selectedFile ? `New file: ${escapeHtml(selectedFile.name)}（${Math.ceil(selectedFile.size / 1024).toLocaleString()} KB）` : (reference.isComplete ? "No new file selected yet" : "No re-upload needed; retry unfinished deconstruction steps directly")}</p>
          ${referenceScopeControls(defaultTarget, reference.isComplete && !selectedFile)}
        </div>`;
    }
    const selectedFile = wizardState.referenceFile;
    return `
    <fieldset class="reference-source" id="reference-source">
      <legend>Reference novel</legend>
      <label class="reference-file-picker ${selectedFile ? "selected" : ""}" id="reference-file-picker" for="reference-file-input"><span id="reference-file-label">${selectedFile ? "Reference novel selected" : "Import novel text"}</span><input id="reference-file-input" type="file" accept=".txt,text/plain" /><small id="reference-file-help">TXT files are supported. Encoding is detected in the background; non-UTF-8 text is converted before deconstruction.</small></label>
      <p id="reference-file-status" class="reference-file-status">${selectedFile ? `Selected: ${escapeHtml(selectedFile.name)} (${Math.ceil(selectedFile.size / 1024).toLocaleString()} KB). Set the deconstruction range.` : "Choose a novel file first, then set the deconstruction range."}</p>
      ${referenceScopeControls(200, !selectedFile)}
    </fieldset>`;
  }
  if (step.id === "world") return worldForm();
  if (step.id === "design") {
    return '<div id="design-chat-host"></div>';
  }
  if (step.id === "stage") {
    const conceptReady = Boolean(wizardState.summary?.story_design?.concept_ready);
    if (!conceptReady) {
      return '<fieldset class="generation-options"><legend>Generation range</legend><p class="decision-note">Finish the previous Book design step before generating the stage roadmap.</p></fieldset>';
    }
    return '<div id="design-chat-host"></div>';
  }
  if (step.id === "mechanics") return mechanicsForm();
  if (step.id === "arcs") return "";
  if (step.id === "chapters") return "";
  if (step.id === "draft") return draftForm();
  return "";
}

function renderDirectionMode(mode) {
  wizardState.directionMode = mode;
  const source = $("#direction-source");
  if (!source) return;
  source.dataset.mode = mode;
  $$('[data-direction-mode]').forEach((button) => {
    const active = button.dataset.directionMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $$('[data-direction-panel]').forEach((panel) => { panel.hidden = panel.dataset.directionPanel !== mode; });
}

function bindDirectionSource() {
  $$('[data-direction-mode]').forEach((button) => button.addEventListener("click", () => renderDirectionMode(button.dataset.directionMode)));
  const input = $("#direction-file-input");
  input?.addEventListener("change", () => {
    const file = input.files?.[0] || null;
    wizardState.directionFile = file;
    wizardState.directionFileContent = "";
    const status = $("#direction-file-status");
    const preview = $("#direction-file-preview");
    const previewBody = preview?.querySelector("pre");
    if (!file) {
      status.textContent = "No file selected yet";
      preview.hidden = true;
      return;
    }
    status.textContent = `Reading: ${file.name}`;
    const reader = new FileReader();
    reader.onload = () => {
      wizardState.directionFileContent = String(reader.result || "");
      status.textContent = `Read: ${file.name} (${wizardState.directionFileContent.length.toLocaleString()} characters)`;
      if (previewBody) previewBody.textContent = wizardState.directionFileContent.slice(0, 1800) || "(file is empty)";
      preview.hidden = false;
    };
    reader.onerror = () => {
      wizardState.directionFile = null;
      status.textContent = "Failed to read the file. Choose it again.";
      preview.hidden = true;
      showToast("Could not read this file.", true);
    };
    reader.readAsText(file, "utf-8");
  });
}



function bindReferenceSource() {
  const hasExisting = referenceStatus().hasExisting;
  const fileInput = $("#reference-file-input");
  fileInput?.addEventListener("change", () => {
    wizardState.referenceFile = fileInput.files?.[0] || null;
    const status = $("#reference-file-status");
    const file = wizardState.referenceFile;
    const scope = $("#reference-scope");
    const picker = $("#reference-file-picker");
    const label = $("#reference-file-label");
    const help = $("#reference-file-help");
    const action = $("#v0-step-form .primary-button");
    if (scope) {
      scope.hidden = false;
      scope.disabled = !file && referenceStatus().isComplete;
    }
    if (picker) picker.classList.toggle("selected", Boolean(file));
    if (label) label.textContent = file ? "New full-book novel selected" : (hasExisting ? "Upload the author's updated full novel" : "Import novel text");
    if (help) help.textContent = file
      ? "The system matches already deconstructed chapters and only deconstructs the new part. You can pick another file."
      : (hasExisting ? "Upload the re-downloaded full TXT and recheck the ending story arcs." : "TXT files are supported. Encoding is detected in the background; non-UTF-8 text is converted before deconstruction.");
    if (status) status.textContent = file
      ? `Selected: ${file.name} (${Math.ceil(file.size / 1024).toLocaleString()} KB). Set this deconstruction range.`
      : (hasExisting
        ? (referenceStatus().isComplete ? "No new file selected yet." : "No re-upload needed; retry unfinished deconstruction steps directly.")
        : "Choose a novel file first, then set the deconstruction range.");
    if (action) {
      action.disabled = !file && referenceStatus().isComplete;
      action.textContent = !file && hasExisting && !referenceStatus().isComplete
        ? "Retry unfinished steps"
        : "Import and start deconstruction";
    }
    const maxInput = $("#reference-max-chapters");
    if (maxInput) maxInput.disabled = !file || wizardState.referenceScope !== "prefix";
  });
  $$('input[name="reference-scope"]').forEach((input) => input.addEventListener("change", () => {
    if (!input.checked) return;
    wizardState.referenceScope = input.value;
    const maxInput = $("#reference-max-chapters");
    maxInput.disabled = input.value !== "prefix";
    if (input.value === "prefix") maxInput.focus();
  }));
}

function bindWorldSource() {
  const input = $("#world-file-input");
  input?.addEventListener("change", () => {
    const files = [...(input.files || [])];
    const status = $("#world-file-status");
    const picker = $("#world-file-picker");
    const label = $("#world-file-label");
    const list = $("#world-new-file-list");
    if (picker) picker.classList.toggle("selected", files.length > 0);
    if (label) label.textContent = files.length ? `${files.length} new sources selected` : (worldSources().length ? "Add more sources" : "Choose source files");
    if (status) status.textContent = files.length ? "Added this time" : "No new file selected yet";
    if (list) {
      list.replaceChildren();
      files.forEach((file) => {
        const item = document.createElement("li");
        const name = document.createElement("strong");
        const size = document.createElement("span");
        name.textContent = file.name;
        size.textContent = `${Math.ceil(file.size / 1024).toLocaleString()} KB`;
        item.append(name, size);
        list.appendChild(item);
      });
      list.hidden = !files.length;
    }
  });
  $("#world-enabled")?.addEventListener("change", async (event) => {
    const enabled = Boolean(event.target.checked);
    const toggle = $("#world-enabled");
    if (toggle) toggle.disabled = true;
    try {
      await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/world-knowledge/enabled`, {
        method: "POST", body: JSON.stringify({ enabled }),
      });
      await refreshWorkspaceArtifacts();
      showToast(enabled ? "Target-world knowledge base enabled." : "Target-world knowledge base is off. Later design will not inject these sources.");
    } catch (error) {
      showToast(error.message || "Toggle failed.", true);
      await refreshWorkspaceArtifacts();
    } finally {
      if (toggle) toggle.disabled = false;
    }
  });
}

function renderMechanicsMode(mode) {
  wizardState.mechanicsMode = mode;
  $$('input[name="mechanics-mode"]').forEach((input) => {
    const active = input.value === mode;
    input.checked = active;
    input.closest(".direction-source-option")?.classList.toggle("active", active);
  });
  $$('[data-mechanics-panel]').forEach((panel) => { panel.hidden = panel.dataset.mechanicsPanel !== mode; });
}

function bindMechanicsSource() {
  $$('input[name="mechanics-mode"]').forEach((input) => input.addEventListener("change", () => {
    if (input.checked) renderMechanicsMode(input.value);
  }));
  const fileInput = $("#mechanics-file-input");
  fileInput?.addEventListener("change", () => {
    wizardState.mechanicsFile = fileInput.files?.[0] || null;
    const status = $("#mechanics-file-status");
    if (status) status.textContent = wizardState.mechanicsFile ? `Selected: ${wizardState.mechanicsFile.name}` : "No file selected yet";
  });
}

async function uploadFile(file) {
  const body = new FormData();
  body.append("file", file);
  return api("/api/uploads", { method: "POST", body });
}

async function activateTask(task, message) {
  wizardState.activeTaskId = task.id;
  wizardState.logOffset = 0;
  const log = $("#drawer-log");
  if (log) log.textContent = "";
  $("#drawer-prompts").innerHTML = '<p class="drawer-prompt-empty">Waiting for a model call…</p>';
  $("#drawer-prompt-count").textContent = "0";
  setTaskView("log");
  $("#task-drawer").classList.add("open");
  $("#drawer-scrim").classList.add("open");
  await refreshTasks();
  if (message) showToast(message);
  return task;
}

async function startTask(type, args, message) {
  if (!wizardState.workspace) throw new Error("Create or select a workspace first.");
  const task = await api("/api/tasks", {
    method: "POST",
    body: JSON.stringify({ type, workspace: wizardState.workspace, args }),
  });
  return activateTask(task, message);
}

async function submitWorldStep() {
  const files = [...($("#world-file-input")?.files || [])];
  if (files.length) {
    const uploads = await Promise.all(files.map(uploadFile));
    await startTask("world_import", { upload_ids: uploads.map((upload) => upload.id) }, "Started importing and building the target-world knowledge base (largest file as primary source).");
    return;
  }
  const sources = worldSources();
  if (!sources.length) throw new Error("Select at least one target-world source first.");
  await startTask("world_build", { force: false }, "Resumed building the target-world knowledge base from a checkpoint. Watch progress in the task log.");
}

async function submitMechanicsStep() {
  const mode = $('input[name="mechanics-mode"]:checked')?.value || "auto";
  const args = { force: Boolean($("#mechanics-force")?.checked) };
  if (mode === "none") args.disable = true;
  if (mode === "text") {
    const direction = $("#mechanics-direction")?.value.trim() || "";
    if (!direction) throw new Error("Enter system-panel settings, or switch to auto-detect.");
    args.direction = direction;
  }
  if (mode === "file") {
    if (!wizardState.mechanicsFile) throw new Error("Choose a system-panel settings file first.");
    args.mechanics_upload_id = (await uploadFile(wizardState.mechanicsFile)).id;
  }
  await startTask("mechanics_init", args, "Started initializing the system panel.");
}

function selectedVolume(id) {
  const value = Number($(id)?.value || 0);
  if (!Number.isInteger(value) || value < 1) throw new Error("Choose a valid stage / volume number.");
  return value;
}

async function submitArcsStep() {
  // Story arcs are driven by the unified dialog; keep an empty handler for form-submit routing.
}

async function submitChaptersStep() {
  // Chapter outlines are driven by the unified dialog; keep an empty handler for form-submit routing.
}

async function submitDraftStep() {
  const volume = selectedVolume("#draft-volume");
  const selectedArc = $("#draft-arc")?.selectedOptions?.[0];
  const arcIdx = Number(selectedArc?.value || 0);
  const start = Number(selectedArc?.dataset.start || 0);
  const end = Number(selectedArc?.dataset.end || 0);
  if (!arcIdx || !start || end < start) throw new Error("Choose a story arc that has already been generated.");
  const max = Number($("#draft-max")?.value || 0);
  const arcChapterCount = end - start + 1;
  if (!Number.isInteger(max) || max < 1 || max > arcChapterCount) {
    throw new Error(`This run should be 1-${arcChapterCount} chapters.`);
  }
  const humanizeExisting = Boolean($("#draft-humanize-existing")?.checked);
  await startTask("write", {
    volume,
    start,
    max,
    no_humanize: !Boolean($("#draft-humanize")?.checked),
    humanize_existing: humanizeExisting,
  }, humanizeExisting
    ? `Started refining existing draft for stage ${volume} Arc ${arcIdx}.`
    : `Started generating draft for stage ${volume} Arc ${arcIdx} (from chapter ${start}, ${max} chapters).`);
}



async function refreshNameSynopsis() {
  await startTask("novel_name_synopsis", { force: true }, "Started regenerating title suggestions and synopsis.");
}

async function _gatherDirectionArgs() {
  const args = {};
  if (wizardState.directionMode === "file") {
    if (!wizardState.directionFile || !wizardState.directionFileContent) throw new Error("Choose and read a creative-direction file first.");
    const upload = await uploadFile(wizardState.directionFile);
    args.direction_upload_id = upload.id;
  } else {
    const direction = $("#direction-input")?.value.trim() || "";
    if (!direction) throw new Error("Enter a creative direction, or switch to reading a file.");
    args.direction = direction;
  }
  return args;
}

async function submitDesignStep() {
  // Book design is driven by the unified dialog; keep an empty handler for form-submit routing.
}

async function submitStageStep() {
  // Stage design is driven by the unified dialog; keep an empty handler for form-submit routing.
}
async function submitReferenceStep() {
  if (!wizardState.workspace) throw new Error("Select a workspace first.");
  const reference = referenceStatus();
  if (reference.isComplete && !wizardState.referenceFile) return;
  const scope = $('input[name="reference-scope"]:checked')?.value || "all";
  const args = {};
  if (scope === "prefix") {
    const maxChapters = Number($("#reference-max-chapters")?.value);
    if (!Number.isInteger(maxChapters) || maxChapters < 1) throw new Error("Enter a valid deconstruction chapter count.");
    if (reference.hasExisting && maxChapters < reference.processed) throw new Error(`Already deconstructed through chapter ${reference.processed}; the target chapter count cannot be smaller.`);
    args.max_chapters = maxChapters;
  }
  let taskType = "reference_resume";
  if (!reference.hasExisting) {
    if (!wizardState.referenceFile) throw new Error("Choose the reference novel TXT file to deconstruct.");
    args.reference_upload_id = (await uploadFile(wizardState.referenceFile)).id;
    taskType = "init";
  } else if (wizardState.referenceFile) {
    args.reference_upload_id = (await uploadFile(wizardState.referenceFile)).id;
  }
  await startTask(taskType, args, reference.hasExisting ? "Started resuming reference deconstruction. Watch progress in the task log." : "Started importing and deconstructing the reference novel. Watch encoding detection and progress in the task log.");
  wizardState.referenceFile = null;
}

function displayVolume(path) {
  const matched = path.match(/vol_(\d+)/i);
  return matched ? `Volume ${Number(matched[1])}` : "Current file";
}

function chapterNumberFromPath(path) {
  const filename = path.split("/").pop() || "";
  const matched = filename.match(/^chapter_0*(\d+)/i)
    || filename.match(/^0*(\d+)(?:[_\-.]|$)/)
    || filename.match(/第\s*(\d+)\s*章/);
  return matched ? Number(matched[1]) : null;
}

function chapterFinalizationTarget(path) {
  const chapter = chapterNumberFromPath(path);
  const volumeMatch = path.match(/\/vol_(\d+)\//i);
  if (chapter === null || !volumeMatch || !path.includes("/chapters/")) return null;
  return { kind: "drafts", volume: Number(volumeMatch[1]), chapter };
}

function chapterFinalizationRecord(path) {
  const chapter = chapterNumberFromPath(path);
  const volumeMatch = path.match(/\/vol_(\d+)\//i);
  if (chapter === null || !volumeMatch) return null;
  const volumeKey = `vol_${String(Number(volumeMatch[1])).padStart(2, "0")}`;
  return wizardState.summary?.finalized_chapters?.drafts?.[volumeKey]?.[String(chapter)] || null;
}

function artifactDescriptor(step, path) {
  const filename = path.split("/").pop();
  const arcMatch = filename.match(/^arc_(\d+)_ch(\d+)_(\d+)/i);
  const chapterNumber = chapterNumberFromPath(path);
  const worldDescriptions = {
    "世界观.md": ["Worldview", "Heaven-and-earth rules, era background, and core conflicts."],
    "力量体系.md": ["Power system", "Realms, power sources, and promotion limits."],
    "关键人物.md": ["Key characters", "Identity, relationships, abilities, and roles."],
    "势力描述.md": ["Factions", "Organizations, interests, and conflict structure."],
    "故事主线.md": ["Main plot", "Event causality with the primary source taking priority."],
    "关键物品.md": ["Key items", "Artifacts, resources, and plot function."],
    "技能体系.md": ["Skill system", "Arts, techniques, methods, and usage rules."],
  };
  const designDescriptions = {
    "core_gameplay.md": ["Core gameplay", "The upgrade and feedback loop that keeps readers going."],
    "worldview.md": ["Worldview", "Base rules, power system, and map layers of the new novel."],
    "rough_outline.md": ["Rough outline", "Core story, gameplay loop, main characters, and operating risks."],
    "stage_outline.md": ["Phase outline", "Standalone record of each phase's goals, changes, and links."],
    "long_mainline.md": ["Long mainline", "Expectation and suspense that run across multiple stages."],
    "stage_roadmap.md": ["Stage roadmap", "Each stage's goals, resources, and phase progression."],
    "character_arcs.md": ["Character arcs", "Key nodes and relationship changes for main characters."],
    "design_state.json": ["Design progress", "Records how much reference deconstruction book design has absorbed."],
    "novel_name_synopsis.md": ["Title suggestions", "Title directions and synopsis generated from the creative skeleton."],
  };
  const mechanicsDescriptions = {
    "profile.json": ["System-panel profile", "Whether the system panel is enabled and which mode it uses."],
    "design.md": ["Mechanics design", "Overall notes for the system or state tracking."],
    "rules.json": ["System-panel rules", "Computable events and constraints that must not be broken."],
    "state.json": ["Initial state", "Initial data for resources, skills, tasks, and more."],
  };

  if (step.id === "reference") {
    if (filename === "novel_outline.md") return { label: "Book outline", description: "Overall story structure and pacing of the reference novel." };
    if (filename === "volume_outline.md") return { label: "This volume's outline", description: "This volume's goals, conflicts, and phase turns." };
    if (arcMatch) return { label: `Story arc ${Number(arcMatch[1])}`, description: `Narrative structure from reference chapters ${Number(arcMatch[2])}-${Number(arcMatch[3])}.` };
  }
  if (step.id === "world" && worldDescriptions[filename]) return { label: worldDescriptions[filename][0], description: worldDescriptions[filename][1] };
  if ((step.id === "design" || step.id === "stage") && designDescriptions[filename]) return { label: designDescriptions[filename][0], description: designDescriptions[filename][1] };
  if (step.id === "stage" && filename === "novel_name_synopsis.md") return { label: "Title and synopsis", description: "Generated from the rough outline, worldview, long mainline, and stage roadmap." };
  if (step.id === "mechanics" && mechanicsDescriptions[filename]) return { label: mechanicsDescriptions[filename][0], description: mechanicsDescriptions[filename][1] };
  if (step.id === "arcs" && arcMatch) return { label: `Story arc ${Number(arcMatch[1])}`, description: `Covers new-novel chapters ${Number(arcMatch[2])}-${Number(arcMatch[3])}.` };
  if (step.id === "chapters" && chapterNumber !== null && path.includes("/system_panels/")) return { label: `Chapter ${chapterNumber} system panel`, description: "A structured protagonist-centered snapshot at the end of this chapter." };
  if (step.id === "chapters" && chapterNumber !== null) return { label: `Chapter ${chapterNumber}`, description: "This chapter's story line, emotional pacing, and descriptive synopsis." };
  if (step.id === "draft" && chapterNumber !== null) return { label: `Chapter ${chapterNumber}`, description: path.includes("/drafts/") ? "Raw draft kept before humanization." : "Official draft after humanization." };
  return { label: filename, description: "Files generated by this step." };
}

function referenceVolumeInfo(directory) {
  const matched = directory.match(/^vol_(\d+)(?:_(.+))?$/i);
  const number = matched ? Number(matched[1]) : Number.MAX_SAFE_INTEGER;
  const name = matched?.[2]?.replace(/_/g, " ") || "Unnamed volume";
  return { number, title: matched ? `Volume ${number} · ${name}` : directory };
}

function referenceReviewGroups(scopedFiles) {
  const volumes = new Map();

  scopedFiles.forEach((item) => {
    const matched = item.path.match(/^reference\/outlines\/([^/]+)\/(?:volume_outline\.md|story_arcs\/[^/]+)$/);
    if (!matched) return;
    const [_, directory] = matched;
    if (!volumes.has(directory)) volumes.set(directory, []);
    volumes.get(directory).push(item);
  });

  const volumeGroups = [...volumes.entries()]
    .map(([directory, files]) => {
      const info = referenceVolumeInfo(directory);
      const orderedFiles = [...files].sort((left, right) => {
        const leftOutline = left.path.endsWith("/volume_outline.md");
        const rightOutline = right.path.endsWith("/volume_outline.md");
        if (leftOutline !== rightOutline) return leftOutline ? -1 : 1;
        return left.path.localeCompare(right.path, "en-US", { numeric: true });
      });
      const arcCount = orderedFiles.filter((file) => file.path.includes("/story_arcs/")).length;
      return {
        ...info,
        files: orderedFiles,
        description: `${orderedFiles.some((file) => file.path.endsWith("/volume_outline.md")) ? "includes this volume outline" : "volume outline not found"} · ${arcCount} story arcs`,
      };
    })
    .sort((left, right) => left.number - right.number);

  const volumeArtifacts = volumeGroups.flatMap((volume) => volume.files.map((file) => ({ path: file.path, ...artifactDescriptor({ id: "reference" }, file.path), groupTitle: volume.title })));

  const overviewFile = scopedFiles.find((item) => item.path === "reference/outlines/novel_outline.md");
  const overviewArtifacts = overviewFile
    ? [{ path: overviewFile.path, ...artifactDescriptor({ id: "reference" }, overviewFile.path), groupTitle: "Book outline" }]
    : [];

  return [
    ...(overviewArtifacts.length ? [{ kind: "reference-overview", title: "Book outline", description: "Overall story structure and pacing of the reference novel, aggregated from each volume.", artifacts: overviewArtifacts }] : []),
    { kind: "reference-volumes", title: "Volume deconstruction", description: `Inspect volume outlines and story arcs by volume, ${volumeGroups.length} volumes.`, volumes: volumeGroups.map((volume) => ({ ...volume, artifacts: volume.files.map((file) => ({ path: file.path, ...artifactDescriptor({ id: "reference" }, file.path), groupTitle: volume.title })) })), artifacts: volumeArtifacts },
  ].filter((group) => (group.artifacts || []).length);
}

function storyArcVolumeInfo(directory) {
  const matched = directory.match(/^vol_(\d+)$/i);
  const number = matched ? Number(matched[1]) : Number.MAX_SAFE_INTEGER;
  return { number, title: matched ? `Volume ${number}` : directory };
}

function storyArcsReviewGroups(scopedFiles) {
  const volumes = new Map();
  scopedFiles.forEach((item) => {
    const matched = item.path.match(/^file_system\/story_arcs\/(vol_\d+)\/(arc_\d+_ch\d+_\d+\.md)$/i);
    if (!matched) return;
    const [_, directory] = matched;
    if (!volumes.has(directory)) volumes.set(directory, []);
    volumes.get(directory).push(item);
  });

  const volumeGroups = [...volumes.entries()]
    .map(([directory, files]) => {
      const info = storyArcVolumeInfo(directory);
      const orderedFiles = [...files].sort((left, right) => left.path.localeCompare(right.path, "en-US", { numeric: true }));
      return { ...info, files: orderedFiles, description: `${orderedFiles.length} story arcs generated` };
    })
    .sort((left, right) => left.number - right.number);

  const artifacts = volumeGroups.flatMap((volume) => volume.files.map((file) => ({ path: file.path, ...artifactDescriptor({ id: "arcs" }, file.path), groupTitle: volume.title })));
  return artifacts.length ? [{
    kind: "story-arc-volumes",
    title: "Generated story arcs",
    description: `Generated content grouped by volume, ${volumeGroups.length} volumes.`,
    volumes: volumeGroups.map((volume) => ({ ...volume, artifacts: volume.files.map((file) => ({ path: file.path, ...artifactDescriptor({ id: "arcs" }, file.path), groupTitle: volume.title })) })),
    artifacts,
  }] : [];
}

function chapterArcReviewGroups(step, scopedFiles) {
  function buildGroup(files, title, contentKind) {
    const volumeNumbers = new Set();
    files.forEach((item) => {
      const matched = item.path.match(/\/vol_(\d+)\//i);
      if (matched) volumeNumbers.add(Number(matched[1]));
    });
    const volumes = [...volumeNumbers].sort((a, b) => a - b).map((volumeNumber) => {
      const volumeInfo = (wizardState.summary?.volumes || []).find((item) => Number(item.volume) === volumeNumber);
      const knownArcs = [...(volumeInfo?.arcs || [])].sort((a, b) => Number(a.idx) - Number(b.idx));
      const volumeFiles = files.filter((item) => Number(item.path.match(/\/vol_(\d+)\//i)?.[1]) === volumeNumber);
      const buckets = knownArcs.map((arc) => ({
        idx: Number(arc.idx), start_ch: Number(arc.start_ch), end_ch: Number(arc.end_ch),
        name: String(arc.title || "").trim(), files: [],
      }));
      const unmatched = [];
      volumeFiles.forEach((file) => {
        const chapter = chapterNumberFromPath(file.path);
        const bucket = buckets.find((arc) => chapter !== null && chapter >= arc.start_ch && chapter <= arc.end_ch);
        (bucket ? bucket.files : unmatched).push(file);
      });
      if (unmatched.length) buckets.push({ idx: null, start_ch: null, end_ch: null, name: "", files: unmatched });
      const arcs = buckets.filter((arc) => arc.files.length).map((arc) => {
        const orderedFiles = [...arc.files].sort((left, right) => left.path.localeCompare(right.path, "en-US", { numeric: true }));
        const arcTitle = arc.idx === null ? "Unassigned story arcs" : `Story arc ${arc.idx}${arc.name ? ` · ${arc.name}` : ""}`;
        const groupTitle = `Volume ${volumeNumber} · ${arcTitle}`;
        return {
          ...arc,
          title: arcTitle,
          description: arc.idx === null ? `${orderedFiles.length} chapter files` : `Chapters ${arc.start_ch}-${arc.end_ch} · ${orderedFiles.length} files`,
          artifacts: orderedFiles.map((file) => ({ path: file.path, ...artifactDescriptor(step, file.path), groupTitle })),
        };
      });
      return { number: volumeNumber, title: `Volume ${volumeNumber}`, description: `${arcs.length} story arcs`, arcs };
    }).filter((volume) => volume.arcs.length);
    const artifacts = volumes.flatMap((volume) => volume.arcs.flatMap((arc) => arc.artifacts));
    return artifacts.length ? {
      kind: "chapter-arc-volumes", contentKind, title,
      description: `Inspect by volume and story arc, ${volumes.length} volumes.`,
      volumes, artifacts,
    } : null;
  }

  if (step.id === "chapters") {
    return [
      buildGroup(scopedFiles.filter((file) => file.path.includes("/chapter_outlines/")), "Chapter outlines", "outlines"),
      buildGroup(scopedFiles.filter((file) => file.path.includes("/system_panels/")), "System panel", "panels"),
    ].filter(Boolean);
  }
  const drafts = buildGroup(scopedFiles, "Generated draft", "drafts");
  return drafts ? [drafts] : [];
}

function isHiddenSupportFile(path) {
  return path.includes("/versions/")
    || path.includes("/conversation")
    || path.endsWith("conversation.json")
    || /\conversation_arc_\d+\.json$/.test(path)
    || path.endsWith("conversation/concept.json")
    || path.endsWith("conversation/stage.json")
    || /_compact\.md$/.test(path)
    || path.endsWith("design_state.json")
    || path.endsWith("arc_usage_state.json")
    || path.endsWith("direction_history.json")
    || path.endsWith("finalized_chapters.json")
    || path.endsWith("manifest.json")
    || path.endsWith("arcs_index.json")
    || path.endsWith("chapter_cards_index.json");
}

function reviewGroupsFor(step) {
  const scopedFiles = wizardState.fileTree.filter((item) => item.type === "file" && step.reviewPrefixes.some((prefix) => item.path.startsWith(prefix)) && !isHiddenSupportFile(item.path));
  if (step.id === "reference") return referenceReviewGroups(scopedFiles);
  if (step.id === "arcs") return storyArcsReviewGroups(scopedFiles);
  if (step.id === "chapters" || step.id === "draft") return chapterArcReviewGroups(step, scopedFiles);
  const groups = (REVIEW_GROUPS[step.id] || []).map((group) => ({ ...group, files: scopedFiles.filter((item) => group.matches(item.path)) }));
  const matched = new Set(groups.flatMap((group) => group.files.map((file) => file.path)));
  const remaining = scopedFiles.filter((file) => !matched.has(file.path));
  if (remaining.length) groups.push({ title: "Other related files", description: "Supporting files generated by this step.", files: remaining });
  return groups.filter((group) => group.files.length).map((group) => ({
    ...group,
    artifacts: group.files.map((file) => ({ path: file.path, ...artifactDescriptor(step, file.path), groupTitle: group.title })),
  }));
}

function artifactButton(artifact, activePath) {
  const chapterNumber = chapterNumberFromPath(artifact.path);
  const compactChapter = chapterNumber !== null && (
    artifact.path.includes("/chapter_outlines/")
    || artifact.path.includes("/system_panels/")
    || artifact.path.includes("/chapters/")
  );
  if (compactChapter) {
    const record = chapterFinalizationRecord(artifact.path);
    const isDraft = artifact.path.includes("/chapters/");
    const synchronized = record?.status === "synced";
    const badge = record
      ? (isDraft
        ? (synchronized ? "✓ Final" : "Final · pending sync")
        : (synchronized ? "✓ Draft synced" : "Draft pending sync"))
      : "";
    return `<button class="artifact-item artifact-chapter-row ${artifact.path === activePath ? "active" : ""} ${record ? "is-finalized" : ""}" data-review-path="${escapeHtml(artifact.path)}" type="button"><span>Chapter ${chapterNumber}</span>${badge ? `<small>${escapeHtml(badge)}</small>` : ""}</button>`;
  }
  return `<button class="artifact-item ${artifact.path === activePath ? "active" : ""}" data-review-path="${escapeHtml(artifact.path)}" type="button"><span class="artifact-file-icon" aria-hidden="true"></span><span class="artifact-item-copy"><span class="artifact-item-type">Generated content</span><span class="artifact-item-label">${escapeHtml(artifact.label)}</span><span class="artifact-item-description">${escapeHtml(artifact.description)}</span><span class="artifact-item-filename">${escapeHtml(artifact.path.split("/").pop())}</span></span></button>`;
}

function reviewOutlineMarkup(step, groups, artifacts) {
  const activePath = artifacts[0]?.path;
  if (step.id === "reference") {
    const overview = groups.find((group) => group.kind === "reference-overview");
    const volumes = groups.find((group) => group.kind === "reference-volumes");
    return `<aside class="artifact-outline reference-outline" id="artifact-outline">
      ${overview ? `<section class="artifact-group"><div class="artifact-group-heading"><span class="artifact-group-kicker">Book structure · 1 file</span><h3>${escapeHtml(overview.title)}</h3><p>${escapeHtml(overview.description)}</p></div><div class="artifact-list">${overview.artifacts.map((artifact) => artifactButton(artifact, activePath)).join("")}</div></section>` : ""}
      ${volumes ? `<section class="reference-section reference-volume-section"><header class="reference-section-heading"><span>Volume deconstruction</span><p>${escapeHtml(volumes.description)}</p></header><div class="volume-accordion">${volumes.volumes.map((volume, index) => `<details class="volume-node" ${index === 0 ? "open" : ""}><summary><span class="volume-node-marker" aria-hidden="true"></span><span class="volume-node-copy"><strong>${escapeHtml(volume.title)}</strong><small>${escapeHtml(volume.description)}</small></span></summary><div class="volume-node-files">${volume.artifacts.map((artifact) => artifactButton(artifact, activePath)).join("")}</div></details>`).join("")}</div></section>` : ""}
    </aside>`;
  }
  if (step.id === "arcs") {
    const volumes = groups.find((group) => group.kind === "story-arc-volumes");
    return `<aside class="artifact-outline story-arcs-outline" id="artifact-outline">
      ${volumes ? `<section class="reference-section reference-volume-section"><header class="reference-section-heading"><span>Generated story arcs</span><p>${escapeHtml(volumes.description)}</p></header><div class="volume-accordion">${volumes.volumes.map((volume) => `<details class="volume-node" open><summary><span class="volume-node-marker" aria-hidden="true"></span><span class="volume-node-copy"><strong>${escapeHtml(volume.title)}</strong><small>${escapeHtml(volume.description)}</small></span></summary><div class="volume-node-files">${volume.artifacts.map((artifact) => artifactButton(artifact, activePath)).join("")}</div></details>`).join("")}</div></section>` : ""}
    </aside>`;
  }
  if (step.id === "chapters" || step.id === "draft") {
    return `<aside class="artifact-outline chapter-arc-outline" id="artifact-outline">
      ${groups.filter((group) => group.kind === "chapter-arc-volumes").map((content, groupIndex) => `<section class="reference-section reference-volume-section"><header class="reference-section-heading"><span>${escapeHtml(content.title)}</span><p>${escapeHtml(content.description)}</p></header><div class="volume-accordion">${content.volumes.map((volume, volumeIndex) => `<details class="volume-node" ${groupIndex === 0 && volumeIndex === 0 ? "open" : ""}><summary><span class="volume-node-marker" aria-hidden="true"></span><span class="volume-node-copy"><strong>${escapeHtml(volume.title)}</strong><small>${escapeHtml(volume.description)}</small></span></summary><div class="arc-node-list">${volume.arcs.map((arc, arcIndex) => `<details class="arc-node" ${groupIndex === 0 && volumeIndex === 0 && arcIndex === 0 ? "open" : ""}><summary><span class="arc-node-marker" aria-hidden="true"></span><span class="volume-node-copy"><strong>${escapeHtml(arc.title)}</strong><small>${escapeHtml(arc.description)}</small></span></summary><div class="arc-node-files">${arc.artifacts.map((artifact) => artifactButton(artifact, activePath)).join("")}</div></details>`).join("")}</div></details>`).join("")}</div></section>`).join("")}
    </aside>`;
  }
  return `<aside class="artifact-outline" id="artifact-outline">${groups.map((group) => `<section class="artifact-group"><div class="artifact-group-heading"><span class="artifact-group-kicker">Content groups · ${group.artifacts.length} files</span><h3>${escapeHtml(group.title)}</h3><p>${escapeHtml(group.description)}</p></div><div class="artifact-list">${group.artifacts.map((artifact) => artifactButton(artifact, activePath)).join("")}</div></section>`).join("")}</aside>`;
}

function renderActiveStep() {
  const step = WIZARD_STEPS.find((item) => item.id === wizardState.activeStep) || WIZARD_STEPS[0];
  const status = statusForStep(step);
  const groups = reviewGroupsFor(step);
  const artifacts = groups.flatMap((group) => group.artifacts);
  wizardState.reviewArtifacts = artifacts;
  const isDone = status === "done";
  const reference = step.id === "reference" ? referenceStatus() : null;
  const referenceActionDisabled = Boolean(
    reference && (
      (reference.isComplete && !wizardState.referenceFile)
      || (!reference.hasExisting && !wizardState.referenceFile)
    ),
  );
  const design = (step.id === "design" || step.id === "stage") ? designStatus() : null;
  const hidePrimaryAction = step.id === "design" || step.id === "arcs" || step.id === "chapters" || (step.id === "stage" && !Boolean(wizardState.summary?.story_design?.stage_ready));
  const actionLabel = step.id === "reference"
    ? (reference?.hasExisting && !reference?.isComplete && !wizardState.referenceFile
      ? "Retry unfinished steps"
      : "Import and start deconstruction")
    : step.id === "design"
      ? (design?.concept_ready ? "Regenerate rough outline and worldview" : "Generate rough outline and worldview")
      : step.id === "stage"
        ? (design?.stage_ready ? "Extend later stages" : "Generate long mainline and stage roadmap")
      : step.id === "world"
        ? "Import and start building"
        : step.id === "mechanics"
          ? "Initialize system panel"
          : step.id === "arcs"
            ? "Generate story arcs"
            : step.id === "chapters"
              ? "Generate chapter outlines"
              : step.id === "draft"
                ? "Start generating draft"
                : "Start generating";
  const heading = step.heading;
  const lead = step.lead;
  const decision = step.decision;
  $("#step-count").textContent = `STEP ${String(stepIndex(step.id) + 1).padStart(2, "0")} / ${String(WIZARD_STEPS.length).padStart(2, "0")}`;
  const chatStep = isDesignChatStep(step);
  $("#step-canvas").innerHTML = chatStep ? `
    <article class="step-view step-view-chat">
      <header class="chat-step-header">
        <div>
          <p class="step-eyebrow">${step.optional ? "Optional step" : "Writing workflow"}</p>
          <h1 class="step-title">${heading}</h1>
          <p class="step-lead">${lead}</p>
        </div>
      </header>
      <section class="chat-band" id="chat-band">
        <div id="${step.id === "arcs" ? "arcs-chat-host" : step.id === "chapters" ? "chapters-chat-host" : step.id === "draft" ? "draft-chat-host" : "design-chat-host"}"></div>
      </section>
      <section class="review-band">
        <div class="band-heading"><h2>Generated content</h2><p>${step.reviewHint}</p></div>
        <div class="review-empty" id="review-empty" ${artifacts.length ? "hidden" : ""}>Review results on the right, or click file links in the chat.</div>
        <div id="review-layout" class="review-layout" ${artifacts.length ? "" : "hidden"}>
          ${reviewOutlineMarkup(step, groups, artifacts)}
          <section class="review-preview"><div id="review-document" class="review-document"></div></section>
        </div>
        <div class="review-actions">
          <button class="primary-button" id="confirm-step" type="button" ${artifacts.length ? "" : "disabled"}>Continue</button>
        </div>
      </section>
    </article>` : `
    <article class="step-view">
      <p class="step-eyebrow">${step.optional ? "Optional step" : "Writing workflow"}</p>
      <div class="step-status ${status}">${isDone ? "Generated content exists; you can return and adjust anytime" : status === "locked" ? "Waiting for earlier steps" : step.optional ? "You can run or skip this step" : "Waiting for generated content"}</div>
      <h1 class="step-title">${heading}</h1>
      <p class="step-lead">${lead}</p>
      <section class="decision-band">
        <div class="band-heading"><h2>This step decides</h2><p>${step.short}</p></div>
        <div class="decision-layout">
          <form class="decision-form" id="v0-step-form">
            ${formForStep(step)}
            ${hidePrimaryAction ? "" : `<div class="decision-actions">
              <button class="primary-button" type="submit" ${referenceActionDisabled ? "disabled" : ""}>${actionLabel}</button>
              ${step.optional ? '<button class="text-button" id="skip-step" type="button">Skip this step for this book</button>' : ""}
            </div>`}
          </form>
          <aside class="context-note"><strong>Design notes</strong>${decision}</aside>
        </div>
      </section>
      <section class="review-band">
        <div class="band-heading"><h2>Generated content</h2><p>${step.reviewHint}</p></div>
        <div class="review-empty" id="review-empty" ${artifacts.length ? "hidden" : ""}>No artifacts found for this step yet. After generation they are grouped here by use.</div>
        <div id="review-layout" class="review-layout" ${artifacts.length ? "" : "hidden"}>
          ${reviewOutlineMarkup(step, groups, artifacts)}
          <section class="review-preview"><div id="review-document" class="review-document"></div></section>
        </div>
        <div class="review-actions">
          <button class="primary-button" id="confirm-step" type="button" ${artifacts.length ? "" : "disabled"}>Continue</button>
        </div>
      </section>
    </article>`;
  $("#v0-step-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.submitter;
    if (submit) submit.disabled = true;
    try {
      if (step.id === "reference") await submitReferenceStep();
      else if (step.id === "world") await submitWorldStep();
      else if (step.id === "design") await submitDesignStep();
      else if (step.id === "stage") await submitStageStep();
      else if (step.id === "mechanics") await submitMechanicsStep();
      else if (step.id === "arcs") await submitArcsStep();
      else if (step.id === "chapters") await submitChaptersStep();
      else if (step.id === "draft") await submitDraftStep();
    } catch (error) {
      showToast(error.message || "Could not start the generation task.", true);
    } finally {
      if (submit) submit.disabled = false;
    }
  });
  if (step.id === "reference") bindReferenceSource();
  if (step.id === "world") bindWorldSource();
  if (step.id === "design") {
    loadDesignChat("concept");
  }
  if (step.id === "stage") {
    loadDesignChat("stage");
  }
  if (step.id === "arcs") {
    wizardState.arcsChatVolume = wizardState.arcsChatVolume || 1;
    loadArcsChat(wizardState.arcsChatVolume);
  }
  if (step.id === "chapters") {
    const volumes = wizardState.summary?.volumes || [];
    wizardState.chaptersChatVolume = wizardState.chaptersChatVolume || (volumes[0]?.volume || 1);
    if (!wizardState.chaptersChatArc) {
      const vol = volumes.find((v) => v.volume === wizardState.chaptersChatVolume);
      wizardState.chaptersChatArc = vol?.arcs?.[0]?.idx || null;
    }
    loadChaptersChat(wizardState.chaptersChatVolume, wizardState.chaptersChatArc);
  }
  if (step.id === "draft") {
    const volumes = wizardState.summary?.volumes || [];
    wizardState.draftChatVolume = wizardState.draftChatVolume || (volumes[0]?.volume || 1);
    if (!wizardState.draftChatArc) {
      const volume = volumes.find((item) => Number(item.volume) === Number(wizardState.draftChatVolume));
      wizardState.draftChatArc = volume?.arcs?.[0]?.idx || null;
    }
    loadDraftChat(wizardState.draftChatVolume, wizardState.draftChatArc);
  }
  if (step.id === "mechanics") bindMechanicsSource();
  if (step.id === "draft") bindDraftRange();
  $("#skip-step")?.addEventListener("click", () => confirmStep(step, true));
  $("#confirm-step").addEventListener("click", () => confirmStep(step, false));
  $$('[data-review-path]').forEach((button) => button.addEventListener("click", () => openReviewFile(button.dataset.reviewPath)));
  const selectedArtifact = artifacts.find((artifact) => artifact.path === wizardState.selectedFile) || artifacts[0];
  if (selectedArtifact) openReviewFile(selectedArtifact.path);
}

function confirmStep(step, skipped) {
  wizardState.confirmed.add(step.id);
  const next = WIZARD_STEPS[stepIndex(step.id) + 1];
  wizardState.activeStep = next?.id || step.id;
  renderRail();
  renderActiveStep();
  showToast(skipped ? "Skipped this optional step." : "Moved to the next step. You can still come back and adjust the current content.");
}

function isReferenceAsset(path) {
  return path.startsWith("reference/");
}

function isReferenceStoryArc(path) {
  return /^reference\/outlines\/[^/]+\/story_arcs\/arc_\d+_ch\d+_\d+\.md$/i.test(path);
}

const CARD_SECTION_LABELS = [
  ["chapter_outline_600", "Chapter synopsis"],
  ["story_line", "Story line"],
];
const CARD_RHYTHM_LABELS = [
  ["core_content", "Core content"],
  ["emotion_tone", "Emotional tone"],
  ["beat_detail", "Pacing breakdown"],
];
const CARD_ENTITY_LABELS = [
  ["characters", "Characters"],
  ["factions", "Factions"],
  ["locations", "Locations"],
  ["items", "Items"],
  ["skills", "Skills"],
];

function renderChapterCardSections(chapter) {
  const sections = CARD_SECTION_LABELS
    .map(([key, label]) => {
      const value = (chapter[key] || "").trim();
      return value ? `<section class="card-section"><h5>${label}</h5><p>${escapeHtml(value)}</p></section>` : "";
    })
    .join("");
  const rhythm = chapter.chapter_rhythm || {};
  const rhythmSections = CARD_RHYTHM_LABELS
    .map(([key, label]) => {
      const value = (rhythm[key] || "").trim();
      return value ? `<section class="card-section"><h5>${label}</h5><p>${escapeHtml(value)}</p></section>` : "";
    })
    .join("");
  const highlights = Array.isArray(chapter.highlights) ? chapter.highlights : [];
  const highlightText = highlights.map((item) => escapeHtml(String(item || "").trim())).filter(Boolean).join("\n");
  const highlightSection = highlightText ? `<section class="card-section"><h5>Highlights</h5><p>${highlightText.replace(/\n/g, "<br>")}</p></section>` : "";
  const rhythmWrap = rhythmSections ? `<section class="card-section card-section-rhythm"><h5>Chapter pacing</h5><div class="card-grid card-grid-inner">${rhythmSections}</div></section>` : "";
  if (!sections && !rhythmSections && !highlightText) {
    return '<p class="card-empty">This chapter has no fact-card content yet.</p>';
  }
  return `<div class="card-grid">${sections}${rhythmWrap}${highlightSection}</div>`;
}

function renderReferenceArcChapters(artifact, data) {
  const documentNode = $("#review-document");
  if (!documentNode) return;
  const chapters = Array.isArray(data.chapters) ? data.chapters : [];
  let activeIndex = 0;
  const renderActiveChapter = () => {
    const chapter = chapters[activeIndex];
    const content = $("#reference-arc-chapter-content");
    if (!chapter || !content) return;
    const sourceTag = chapter.source === "raw" ? '<span class="card-source-tag">Source fallback</span>' : "";
    content.innerHTML = `<header><p>Chapter ${chapter.number}</p><h4>${escapeHtml(chapter.title)}</h4>${sourceTag}</header>${renderChapterCardSections(chapter)}`;
    $$('[data-reference-chapter-index]').forEach((button) => button.classList.toggle("active", Number(button.dataset.referenceChapterIndex) === activeIndex));
  };

  documentNode.innerHTML = `<header class="preview-meta"><div><p>Chapter fact card</p><h3>${escapeHtml(artifact.label)} · Covered chapters</h3><span>Select a chapter on the left to view its fact card. Reference deconstruction assets are read-only and cannot be edited in the workbench.</span><code>${escapeHtml(data.path)}</code></div><div class="preview-tools"><button id="back-to-reference-arc" class="secondary-button" type="button">Back to story arc</button></div></header><div class="reference-arc-browser"><nav class="reference-arc-chapter-list" aria-label="Story-arc chapters">${chapters.map((chapter, index) => `<button class="reference-arc-chapter ${index === 0 ? "active" : ""}" data-reference-chapter-index="${index}" type="button"><strong>Chapter ${chapter.number}</strong><span>${escapeHtml(chapter.title)}</span></button>`).join("")}</nav><article id="reference-arc-chapter-content" class="reference-arc-chapter-content"></article></div>`;
  $("#back-to-reference-arc")?.addEventListener("click", () => renderReviewDocument(artifact));
  $$('[data-reference-chapter-index]').forEach((button) => button.addEventListener("click", () => {
    activeIndex = Number(button.dataset.referenceChapterIndex);
    renderActiveChapter();
  }));
  renderActiveChapter();
}

async function openReferenceArcChapters(path, artifact) {
  try {
    const data = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/reference-arc-chapters?path=${encodeURIComponent(path)}`);
    renderReferenceArcChapters(artifact, data);
  } catch (error) {
    showToast(error.message || "Could not read source chapters for this story arc.", true);
  }
}

function isSystemPanelSnapshot(path) {
  return path.includes("/system_panels/") && path.toLowerCase().endsWith(".json");
}

function systemPanelValueMarkup(value) {
  if (value === null || value === undefined || value === "") return '<span class="panel-empty-value">Not recorded</span>';
  if (Array.isArray(value)) {
    if (!value.length) return '<span class="panel-empty-value">None</span>';
    if (value.some((item) => item && typeof item === "object")) {
      return `<div class="panel-item-list">${value.map((item) => `<div>${systemPanelValueMarkup(item)}</div>`).join("")}</div>`;
    }
    return `<div class="panel-tag-list">${value.map((item) => `<span>${escapeHtml(String(item))}</span>`).join("")}</div>`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return '<span class="panel-empty-value">None</span>';
    return `<dl class="panel-kv-list">${entries.map(([key, item]) => `<div><dt>${escapeHtml(key)}</dt><dd>${systemPanelValueMarkup(item)}</dd></div>`).join("")}</dl>`;
  }
  if (typeof value === "boolean") return `<span class="panel-boolean ${value ? "yes" : "no"}">${value ? "yes" : "no"}</span>`;
  return `<span class="panel-scalar">${escapeHtml(String(value))}</span>`;
}

function systemPanelPreview(content) {
  let panel;
  try { panel = JSON.parse(content); } catch (_) { return null; }
  if (!panel || typeof panel !== "object" || Array.isArray(panel)) return null;
  const state = panel.panel && typeof panel.panel === "object"
    ? panel.panel
    : panel.protagonist_state && typeof panel.protagonist_state === "object"
      ? panel.protagonist_state
      : {};
  const labels = {
    values: "Core numbers", resources: "Resources", inventory: "Item counts",
    skills: "Skill levels", task_progress: "Quest progress",
    identity: "Identity", attributes: "Attributes", equipment: "Equipment", tasks: "Quests", relationships: "Relationships",
    injuries_and_status: "Injuries and status", flags: "Key flags",
  };
  const orderedState = {};
  Object.entries(labels).forEach(([key, label]) => {
    const value = state[key];
    const hasContent = Array.isArray(value) ? value.length : value && typeof value === "object" ? Object.keys(value).length : value !== undefined && value !== "";
    if (hasContent) orderedState[label] = value;
  });
  Object.entries(state).filter(([key]) => !labels[key]).forEach(([key, value]) => { orderedState[key] = value; });
  const stateSections = `<section class="system-panel-wide panel-current-state"><h4>Current panel</h4>${systemPanelValueMarkup(orderedState)}</section>`;
  const changes = Array.isArray(panel.changes) ? panel.changes : [];
  const changeMarkup = changes.length
    ? `<section class="system-panel-wide"><h4>This chapter's changes</h4><div class="panel-change-list">${changes.map((item) => `<article><strong>${escapeHtml(String(item.field || [item.category, item.key].filter(Boolean).join(".") || "State changes"))}</strong><div><span>${escapeHtml(String(item.before ?? "Not recorded"))}</span><b>→</b><span>${escapeHtml(String(item.after ?? "Not recorded"))}</span></div>${item.reason ? `<p>${escapeHtml(String(item.reason))}</p>` : ""}</article>`).join("")}</div></section>`
    : '<section class="system-panel-wide panel-no-change"><h4>Chapter changes</h4><p>This chapter has no protagonist state changes to record.</p></section>';
  const displays = Array.isArray(panel.panel_display) ? panel.panel_display : [];
  const notes = Array.isArray(panel.continuity_notes) ? panel.continuity_notes : [];
  const supporting = [
    displays.length ? `<section class="system-panel-wide"><h4>In-text display panel</h4>${systemPanelValueMarkup(displays)}</section>` : "",
    notes.length ? `<section class="system-panel-wide"><h4>Continuity notes for the next chapter</h4>${systemPanelValueMarkup(notes)}</section>` : "",
  ].join("");
  return `<div class="system-panel-overview"><div><span>Chapter</span><strong>Chapter ${Number(panel.chapter || 0)}</strong></div><div><span>State changes</span><strong>${changes.length} items</strong></div>${displays.length ? `<div><span>Draft display</span><strong>${displays.length} items</strong></div>` : ""}</div><div class="system-panel-grid">${stateSections}${changeMarkup}${supporting}</div>`;
}

async function copyPreviewText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("The browser blocked copying");
}

function renderReviewDocument(artifact) {
  const documentNode = $("#review-document");
  if (!documentNode || !wizardState.selectedFile) return;
  const path = wizardState.selectedFile;
  const readonlyReference = isReferenceAsset(path);
  const copyDraftButton = (
    wizardState.activeStep === "draft"
    && path.includes("/chapters/")
    && !wizardState.fileEditing
  ) ? '<button id="copy-draft-preview" class="secondary-button copy-preview-button" type="button">Copy all</button>' : "";
  const finalizationTarget = chapterFinalizationTarget(path);
  const finalizationRecord = chapterFinalizationRecord(path);
  const finalized = Boolean(finalizationRecord?.finalized);
  const finalizationButton = finalizationTarget && !wizardState.fileEditing
    ? `<button id="toggle-chapter-finalized" class="secondary-button ${finalized ? "is-finalized" : ""}" type="button">${finalized ? "Unmark final" : "Mark as final"}</button>`
    : "";
  const controls = isReferenceStoryArc(path)
    ? '<div class="preview-tools"><button id="view-reference-arc-chapters" class="secondary-button" type="button">View chapters in this arc</button></div>'
    : readonlyReference
      ? ""
      : wizardState.fileEditing
        ? '<div class="preview-tools"><button id="cancel-file-edit" class="secondary-button" type="button">Cancel</button><button id="save-file-edit" class="primary-button" type="button">Save changes</button></div>'
        : `<div class="preview-tools">${copyDraftButton}${finalizationButton}<button id="edit-review-file" class="secondary-button" type="button">Edit this file</button></div>`;
  const panelPreview = !wizardState.fileEditing && isSystemPanelSnapshot(path)
    ? systemPanelPreview(wizardState.selectedFileContent)
    : null;
  const body = wizardState.fileEditing
    ? `<textarea id="review-editor" class="review-editor" spellcheck="false">${escapeHtml(wizardState.selectedFileContent)}</textarea>`
    : panelPreview || markdownPreview(wizardState.selectedFileContent);
  documentNode.innerHTML = `<header class="preview-meta"><div><p>${escapeHtml(artifact.groupTitle)}</p><h3>${escapeHtml(artifact.label)}</h3><span>${escapeHtml(artifact.description)}</span><code>${escapeHtml(path)}</code></div>${controls}</header>${body}`;
  $("#view-reference-arc-chapters")?.addEventListener("click", () => openReferenceArcChapters(path, artifact));
  $("#toggle-chapter-finalized")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/finalized-chapters`, {
        method: "POST",
        body: JSON.stringify({ ...finalizationTarget, finalized: !finalized }),
      });
      wizardState.summary.finalized_chapters = result.finalized_chapters;
      renderActiveStep();
      await openReviewFile(path);
      showToast(finalized ? "Final marker removed." : "Marked as finalized. Later chat adjustments will skip this chapter.");
    } catch (error) {
      button.disabled = false;
      showToast(error.message || "Could not update the final marker.", true);
    }
  });
  $("#copy-draft-preview")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await copyPreviewText(wizardState.selectedFileContent);
      button.textContent = "Copied";
      showToast("Draft copied to the clipboard.");
      setTimeout(() => {
        if (button.isConnected) {
          button.textContent = "Copy all";
          button.disabled = false;
        }
      }, 1200);
    } catch (error) {
      button.disabled = false;
      showToast(error.message || "Copy failed. Select the draft text and copy it manually.", true);
    }
  });
  $("#edit-review-file")?.addEventListener("click", () => {
    wizardState.fileEditing = true;
    renderReviewDocument(artifact);
  });
  $("#cancel-file-edit")?.addEventListener("click", () => {
    wizardState.fileEditing = false;
    renderReviewDocument(artifact);
  });
  $("#save-file-edit")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const content = $("#review-editor")?.value;
    if (typeof content !== "string") return;
    button.disabled = true;
    try {
      await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/file`, {
        method: "PUT",
        body: JSON.stringify({ path, content }),
      });
      wizardState.selectedFileContent = content;
      wizardState.fileEditing = false;
      await refreshWorkspaceArtifacts();
      await openReviewFile(path);
      showToast("File saved.");
    } catch (error) {
      showToast(error.message || "Failed to save the file.", true);
    } finally {
      button.disabled = false;
    }
  });
}

async function openReviewFile(path) {
  try {
    if (!wizardState.workspace) throw new Error("Select a workspace first.");
    const data = await api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/file?path=${encodeURIComponent(path)}`);
    const artifact = wizardState.reviewArtifacts.find((item) => item.path === path) || { label: path.split("/").pop(), description: "Editable files in the workspace.", groupTitle: "Workspace files" };
    wizardState.selectedFile = path;
    wizardState.selectedFileContent = data.content;
    wizardState.fileEditing = false;
    $("#review-empty").hidden = true;
    $("#review-layout").hidden = false;
    renderReviewDocument(artifact);
    $$('[data-review-path]').forEach((button) => button.classList.toggle("active", button.dataset.reviewPath === path));
  } catch (error) { showToast(error.message, true); }
}

function isPreviewableFile(path) {
  return /\.(?:md|txt|json|yaml|yml|csv|tsv)$/i.test(path);
}

function isHiddenReferenceSupportFile(path) {
  return path === "reference/sample_novel.txt"
    || path === "reference/analysis_state.json"
    || path === "reference/chapter_cards_index.json"
    || path === "reference/outlines/volume_outline.md"
    || path.startsWith("reference/chapters/")
    || path.startsWith("reference/chapter_cards/")
    || /\/meta\.json$/i.test(path)
    || /\/arcs_index\.json$/i.test(path);
}

function renderFileBrowser() {
  const query = ($("#file-search")?.value || "").trim().toLowerCase();
  const items = wizardState.fileTree.filter((item) => item.type === "file" && isPreviewableFile(item.path) && !isHiddenReferenceSupportFile(item.path) && item.path.toLowerCase().includes(query));
  $("#file-browser-list").innerHTML = items.length
    ? items.map((item) => `<button class="file-browser-item" data-open-file="${escapeHtml(item.path)}" type="button"><strong>${escapeHtml(item.path.split("/").pop())}</strong><span>${escapeHtml(item.path)}</span></button>`).join("")
    : '<p class="review-empty">No previewable text files found.</p>';
  $$('[data-open-file]').forEach((button) => button.addEventListener("click", async () => {
    closeFileBrowser();
    await openReviewFile(button.dataset.openFile);
  }));
}

function openFileBrowser() {
  if (!wizardState.workspace) {
    showToast("Create or select a workspace first.", true);
    return;
  }
  $("#file-browser").classList.add("open");
  $("#file-scrim").classList.add("open");
  $("#file-search").value = "";
  renderFileBrowser();
}

function closeFileBrowser() {
  $("#file-browser").classList.remove("open");
  $("#file-scrim").classList.remove("open");
}

function taskLabel(task) {
  if (task.status === "running") return "Running";
  if (task.status === "succeeded") return "Done";
  if (task.status === "succeeded_with_warnings") return "Needs review";
  if (task.status === "failed") return "Failed";
  return "Waiting";
}

function promptCardsMarkup(items, openLatest = false) {
  if (!items?.length) return '<p class="drawer-prompt-empty">This task has not called a model yet.</p>';
  return items.map((item, index) => {
    const call = `Call ${index + 1}${item.model ? ` · ${escapeHtml(item.model)}` : ""}`;
    const open = openLatest && index === items.length - 1 ? " open" : "";
    const hasPrompt = item.content_recorded === true || Object.prototype.hasOwnProperty.call(item, "prompt");
    const content = hasPrompt
      ? (item.prompt || "(No prompt text was sent.)")
      : `Prompt content was not retained (${item.trace_mode || "metadata"} mode). Choose Full in Settings only for private local debugging.`;
    return `<details class="drawer-prompt-card"${open}>
      <summary><strong>${call}</strong><span>${escapeHtml(item.created_at || "")}</span></summary>
      <pre>${escapeHtml(content)}</pre>
    </details>`;
  }).join("");
}

function setTaskView(view) {
  wizardState.taskView = view === "prompt" ? "prompt" : "log";
  $$('[data-task-view]').forEach((button) => button.classList.toggle("active", button.dataset.taskView === wizardState.taskView));
  const promptView = wizardState.taskView === "prompt";
  $("#drawer-log").hidden = promptView;
  $("#drawer-prompts").hidden = !promptView;
  if (promptView) refreshTaskPrompts();
}

async function refreshTaskPrompts() {
  if (!wizardState.activeTaskId) return;
  try {
    const data = await api(`/api/tasks/${wizardState.activeTaskId}/prompts`);
    const items = data.items || [];
    $("#drawer-prompt-count").textContent = String(Number(data.task?.prompt_count ?? items.length));
    $("#drawer-prompts").innerHTML = promptCardsMarkup(items, true);
  } catch (_) { /* task may have been removed */ }
}

function openPromptDialog(items, meta = "Model call") {
  const dialog = $("#prompt-dialog");
  const list = Array.isArray(items) ? items : [];
  $("#prompt-dialog-meta").textContent = meta;
  $("#prompt-dialog-list").innerHTML = promptCardsMarkup(list, true);
  const latest = list[list.length - 1];
  wizardState.currentPromptText = latest && latest.content_recorded ? String(latest.prompt || "") : "";
  if (typeof dialog.showModal === "function") dialog.showModal();
}

async function showJobPrompts(url, meta) {
  try {
    const data = await api(url);
    openPromptDialog(data.items || [], meta);
  } catch (error) {
    showToast(error.message || "Could not read the model prompt.", true);
  }
}

async function refreshTasks() {
  if (!wizardState.workspace) return;
  const tasks = (await api(`/api/tasks?workspace=${encodeURIComponent(wizardState.workspace)}`)).items;
  wizardState._tasks = tasks;
  if (!wizardState.activeTaskId && tasks[0]) wizardState.activeTaskId = tasks[0].id;
  const activeTask = tasks.find((task) => task.id === wizardState.activeTaskId);
  $("#drawer-prompt-count").textContent = String(Number(activeTask?.prompt_count || 0));
  $("#delete-current-task").disabled = !activeTask || ["queued", "running"].includes(activeTask.status);
  $("#drawer-tasks").innerHTML = tasks.length ? tasks.map((task) => `<button class="drawer-task ${task.id === wizardState.activeTaskId ? "active" : ""}" data-task="${task.id}" type="button"><span><span class="drawer-task-title">${escapeHtml(task.label)}</span><span class="drawer-task-meta">${escapeHtml(task.created_at || "")}</span></span><span class="task-state ${task.status}">${taskLabel(task)}</span></button>`).join("") : '<p class="review-empty">This workspace has no task records yet.</p>';
  $$('[data-task]').forEach((button) => button.addEventListener("click", () => {
    wizardState.activeTaskId = button.dataset.task;
    wizardState.logOffset = 0;
    $("#drawer-log").textContent = "";
    refreshTasks().then(() => Promise.all([refreshLog(), refreshTaskPrompts()]));
  }));
}

async function refreshLog() {
  if (!wizardState.activeTaskId) return;
  try {
    const data = await api(`/api/tasks/${wizardState.activeTaskId}/logs?offset=${wizardState.logOffset}`);
    if (data.content) {
      const log = $("#drawer-log");
      log.textContent += data.content;
      log.scrollTop = log.scrollHeight;
      wizardState.logOffset = data.next_offset;
    }
    wizardState._tasks = wizardState._tasks?.map((item) => item.id === data.task.id ? data.task : item);
    $("#drawer-prompt-count").textContent = String(Number(data.task.prompt_count || 0));
    if (["succeeded", "succeeded_with_warnings", "failed"].includes(data.task.status) && wizardState.lastSyncedTaskId !== data.task.id) {
      wizardState.lastSyncedTaskId = data.task.id;
      await refreshTasks();
      await refreshWorkspaceArtifacts();
      showToast(data.task.status === "failed" ? "The task finished unsuccessfully. Check the log." : "Task completed. Generated content was refreshed.", data.task.status === "failed");
    }
  } catch (_) { /* A server restart clears in-memory task metadata. */ }
}

async function refreshWorkspaceArtifacts() {
  if (!wizardState.workspace) return;
  const [summary, tree] = await Promise.all([
    api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}`),
    api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/tree`),
  ]);
  wizardState.summary = summary;
  wizardState.fileTree = tree.items;
  renderRail();
  renderActiveStep();
}

async function refreshReviewArtifactsOnly(reloadSelected = false, expectedStep = "arcs", followLatest = false) {
  if (!wizardState.workspace || wizardState.activeStep !== expectedStep) return;
  const [summary, tree] = await Promise.all([
    api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}`),
    api(`/api/workspaces/${encodeURIComponent(wizardState.workspace)}/tree`),
  ]);
  wizardState.summary = summary;
  wizardState.fileTree = tree.items;
  renderRail();

  const step = WIZARD_STEPS.find((item) => item.id === expectedStep);
  const groups = reviewGroupsFor(step);
  const artifacts = groups.flatMap((group) => group.artifacts);
  wizardState.reviewArtifacts = artifacts;

  const empty = $("#review-empty");
  const layout = $("#review-layout");
  if (empty) empty.hidden = artifacts.length > 0;
  if (layout) layout.hidden = artifacts.length === 0;
  const continueButton = $("#confirm-step");
  if (continueButton) continueButton.disabled = artifacts.length === 0;
  if (!layout || !artifacts.length) return;

  const previousPath = wizardState.selectedFile;
  const outline = $("#artifact-outline");
  const nextOutline = reviewOutlineMarkup(step, groups, artifacts);
  if (outline) outline.outerHTML = nextOutline;
  else layout.insertAdjacentHTML("afterbegin", nextOutline);
  $$('[data-review-path]').forEach((button) => button.addEventListener("click", () => openReviewFile(button.dataset.reviewPath)));

  const selected = artifacts.find((artifact) => artifact.path === previousPath);
  if (followLatest) {
    await openReviewFile(artifacts[artifacts.length - 1].path);
  } else if (selected) {
    $$('[data-review-path]').forEach((button) => button.classList.toggle("active", button.dataset.reviewPath === previousPath));
    if (reloadSelected) await openReviewFile(previousPath);
  } else {
    await openReviewFile(artifacts[artifacts.length - 1].path);
  }
}

async function refreshWorkspaceOptions(selectedName = "") {
  const data = await api("/api/workspaces");
  const select = $("#workspace-select");
  select.innerHTML = data.items.length
    ? data.items.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join("")
    : '<option value="">No workspace yet</option>';
  if (selectedName && data.items.some((item) => item.name === selectedName)) select.value = selectedName;
  const deleteButton = $("#delete-workspace");
  if (deleteButton) deleteButton.disabled = !data.items.length;
  return data;
}

function openWorkspacePanel() {
  $("#workspace-panel").classList.add("open");
  $("#workspace-panel").setAttribute("aria-hidden", "false");
  $("#workspace-scrim").classList.add("open");
  $("#new-workspace-name").focus();
}

function closeWorkspacePanel() {
  $("#workspace-panel").classList.remove("open");
  $("#workspace-panel").setAttribute("aria-hidden", "true");
  $("#workspace-scrim").classList.remove("open");
}

async function createWorkspace(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const name = $("#new-workspace-name").value.trim();
  if (!name) throw new Error("Enter a work title.");
  if ([...$("#workspace-select").options].some((option) => option.value === name)) throw new Error("That workspace already exists. Use another name.");
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const task = await api("/api/tasks", { method: "POST", body: JSON.stringify({ type: "workspace_init", workspace: name, args: {} }) });
    await refreshWorkspaceOptions(name);
    await selectWorkspace(name);
    closeWorkspacePanel();
    form.reset();
    await activateTask(task, "Workspace created. Import a reference novel in step 1. ");
  } finally {
    submit.disabled = false;
  }
}

async function deleteCurrentWorkspace() {
  const name = wizardState.workspace || $("#workspace-select")?.value || "";
  if (!name) throw new Error("There is no workspace to delete.");
  const confirmed = window.confirm(
    `Delete workspace “${name}”?\n\nLocal files, generated content, task logs, and prompts will be permanently deleted. This cannot be undone.`,
  );
  if (!confirmed) return;

  const button = $("#delete-workspace");
  button.disabled = true;
  try {
    await api(`/api/workspaces/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await refreshWorkspaceOptions();
    const nextName = data.items[0]?.name || "";
    $("#workspace-select").value = nextName;
    await selectWorkspace(nextName);
    showToast(`Workspace “${name}” deleted.`);
  } finally {
    button.disabled = !$("#workspace-select")?.value;
  }
}

async function selectWorkspace(name) {
  wizardState.workspace = name || null;
  wizardState.confirmed = new Set();
  wizardState.activeTaskId = null;
  wizardState.logOffset = 0;
  wizardState.directionMode = "text";
  wizardState.directionFile = null;
  wizardState.directionFileContent = "";
  wizardState.referenceFile = null;
  wizardState.referenceScope = "all";
  wizardState.mechanicsMode = "auto";
  wizardState.mechanicsFile = null;
  wizardState.selectedFile = null;
  wizardState.selectedFileContent = "";
  wizardState.fileEditing = false;
  wizardState.designExtensionSource = "existing";
  wizardState.lastSyncedTaskId = null;
  if (!name) {
    wizardState.summary = null;
    wizardState.fileTree = [];
    wizardState.activeStep = "reference";
  } else {
    const [summary, tree] = await Promise.all([api(`/api/workspaces/${encodeURIComponent(name)}`), api(`/api/workspaces/${encodeURIComponent(name)}/tree`)]);
    wizardState.summary = summary;
    wizardState.fileTree = tree.items;
    wizardState.activeStep = currentRecommendedStep();
  }
  renderRail();
  renderActiveStep();
  refreshTasks();
}

async function boot() {
  try {
    const data = await refreshWorkspaceOptions();
    const select = $("#workspace-select");
    select.addEventListener("change", () => selectWorkspace(select.value));
    $("#new-workspace").addEventListener("click", openWorkspacePanel);
    $("#delete-workspace").addEventListener("click", async () => {
      try { await deleteCurrentWorkspace(); } catch (error) { showToast(error.message || "Could not delete the workspace.", true); }
    });
    $("#new-workspace-form").addEventListener("submit", async (event) => {
      try { await createWorkspace(event); } catch (error) { showToast(error.message || "Could not create the workspace.", true); }
    });
    $("#cancel-workspace").addEventListener("click", closeWorkspacePanel);
    $("#close-workspace-panel").addEventListener("click", closeWorkspacePanel);
    $("#workspace-scrim").addEventListener("click", closeWorkspacePanel);
    $("#open-task-drawer").addEventListener("click", () => { $("#task-drawer").classList.add("open"); $("#drawer-scrim").classList.add("open"); refreshTasks(); });
    $("#close-task-drawer").addEventListener("click", closeDrawer);
    $("#drawer-scrim").addEventListener("click", closeDrawer);
    $("#close-file-browser").addEventListener("click", closeFileBrowser);
    $("#file-scrim").addEventListener("click", closeFileBrowser);
    $("#file-search").addEventListener("input", renderFileBrowser);
    $("#open-settings").addEventListener("click", openSettings);
    $("#close-settings").addEventListener("click", closeSettings);
    $("#settings-scrim").addEventListener("click", closeSettings);
    $$('[data-task-view]').forEach((button) => button.addEventListener("click", () => setTaskView(button.dataset.taskView)));
    $("#close-prompt-dialog").addEventListener("click", () => $("#prompt-dialog").close());
    $("#copy-current-prompt").addEventListener("click", async () => {
      if (!wizardState.currentPromptText) return;
      await navigator.clipboard.writeText(wizardState.currentPromptText);
      showToast("Prompt copied.");
    });
    $("#clear-workspace-prompts").addEventListener("click", async () => {
      if (!wizardState.workspace || !confirm("Clear prompt records for all finished tasks in this workspace? Task logs and generated artifacts are kept.")) return;
      try {
        const result = await api(`/api/task-prompts?workspace=${encodeURIComponent(wizardState.workspace)}`, { method: "DELETE" });
        $("#drawer-prompts").innerHTML = '<p class="drawer-prompt-empty">Historical prompts for this workspace were cleared.</p>';
        $("#drawer-prompt-count").textContent = "0";
        await refreshTasks();
        const skipped = Number(result.skipped_active_count || 0);
        showToast(skipped ? `Cleared historical prompts; skipped ${skipped} running tasks.` : "Historical prompts for this workspace were cleared.");
      } catch (error) { showToast(error.message || "Could not clear prompts.", true); }
    });
    $("#delete-current-task").addEventListener("click", async () => {
      if (!wizardState.activeTaskId || !confirm("Delete the current task record? Its run log and prompts will also be deleted. This cannot be undone.")) return;
      try {
        await api(`/api/tasks/${wizardState.activeTaskId}`, { method: "DELETE" });
        wizardState.activeTaskId = null;
        wizardState.logOffset = 0;
        $("#drawer-log").textContent = "";
        $("#drawer-prompts").innerHTML = '<p class="drawer-prompt-empty">Select a task.</p>';
        await refreshTasks();
        showToast("Task record, log, and prompts deleted.");
      } catch (error) { showToast(error.message || "Could not delete the task record.", true); }
    });
    document.addEventListener("click", (event) => {
      const id = event.target.closest("button")?.id;
      const workspace = encodeURIComponent(wizardState.workspace || "");
      if (id === "show-arcs-prompt") {
        showJobPrompts(`/api/workspaces/${workspace}/arcs/${Number(wizardState.arcsChatVolume || 1)}/prompts`, "Story arcs · model prompt");
      } else if (id === "show-chapters-prompt") {
        showJobPrompts(`/api/workspaces/${workspace}/chapters/${Number(wizardState.chaptersChatVolume || 1)}/${Number(wizardState.chaptersChatArc || 1)}/prompts`, "Chapter outlines · model prompt");
      } else if (id === "show-draft-prompt") {
        showJobPrompts(`/api/workspaces/${workspace}/drafts/${Number(wizardState.draftChatVolume || 1)}/${Number(wizardState.draftChatArc || 1)}/prompts`, "Draft generation · model prompt");
      }
    });
    await selectWorkspace(data.items[0]?.name || "");
    setInterval(async () => {
      await refreshLog();
      if (wizardState.taskView === "prompt" && $("#task-drawer").classList.contains("open")) await refreshTaskPrompts();
    }, 1400);
  } catch (error) { showToast(error.message, true); }
}

function closeDrawer() { $("#task-drawer").classList.remove("open"); $("#drawer-scrim").classList.remove("open"); }
boot();
