function designLensesBase(name) {
  return `/api/workspaces/${encodeURIComponent(name)}/design/lenses`;
}

function designLensesOn(name) {
  return wizardState.workspace === name;
}

function designLensBarMarkup(status) {
  const lenses = Array.isArray(status && status.lenses) ? status.lenses : [];
  const subtitle = lenses.length ? escapeHtml(lenses.join(", ")) : "";
  const reset = status && status.exists
    ? '<button id="reset-design-lenses" class="chat-icon-btn" type="button">Restore default</button>'
    : "";
  return `<div class="writing-guide-bar"><div><strong>Critic lenses</strong><span>${subtitle}</span></div><div class="writing-guide-actions"><input id="design-lenses-file" type="file" accept=".txt,.md" hidden><button id="upload-design-lenses" class="chat-icon-btn" type="button">Upload lenses</button>${reset}</div></div>`;
}

function bindDesignLenses(scope) {
  $("#upload-design-lenses")?.addEventListener("click", () => $("#design-lenses-file")?.click());
  $("#design-lenses-file")?.addEventListener("change", () => saveDesignLenses(scope));
  $("#reset-design-lenses")?.addEventListener("click", () => resetDesignLenses(scope));
}

async function fetchDesignLenses(workspace) {
  try {
    return await api(designLensesBase(workspace));
  } catch (_) {
    return null;
  }
}

async function saveDesignLenses(scope) {
  const file = $("#design-lenses-file")?.files?.[0];
  if (!file) return;
  const workspace = wizardState.workspace;
  try {
    const upload = await uploadFile(file);
    if (!designLensesOn(workspace)) return;
    const status = await api(designLensesBase(workspace), {
      method: "POST", body: JSON.stringify({ upload_id: upload.id }),
    });
    if (!designLensesOn(workspace)) return;
    wizardState.designLenses = status;
    showToast("Critic lenses saved.");
    loadDesignChat(scope);
  } catch (error) {
    if (!designLensesOn(workspace)) return;
    showToast(error.message || "Could not save critic lenses.", true);
  }
}

async function resetDesignLenses(scope) {
  const workspace = wizardState.workspace;
  try {
    const status = await api(designLensesBase(workspace), { method: "DELETE" });
    if (!designLensesOn(workspace)) return;
    wizardState.designLenses = status;
    showToast("Restored the built-in critic lenses.");
    loadDesignChat(scope);
  } catch (error) {
    if (!designLensesOn(workspace)) return;
    showToast(error.message || "Could not reset critic lenses.", true);
  }
}
