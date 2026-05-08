const state = {
  presets: [],
  settings: {},
  queue: { paused: true, items: [] },
  qualityRawVisible: false,
};

const $ = (id) => document.getElementById(id);

function toast(title, message = "", kind = "info") {
  const node = document.createElement("div");
  node.className = `toast toast-${kind}`;
  node.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span>`;
  $("toast-region").appendChild(node);
  setTimeout(() => node.remove(), 4400);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json();
}

function selectedPreset() {
  return $("preset-select").value || state.settings.default_preset || "best-original";
}

function currentUrls() {
  return $("url-input").value.trim();
}

function archiveForDownload() {
  return $("archive-for-download").checked;
}

function renderPresets() {
  const select = $("preset-select");
  select.innerHTML = "";
  for (const preset of state.presets) {
    const option = document.createElement("option");
    option.value = preset.id;
    option.textContent = preset.name;
    select.appendChild(option);
  }
  select.value = state.settings.default_preset || "best-original";
  renderPresetPanel();
}

function renderPresetPanel() {
  const preset = state.presets.find((item) => item.id === selectedPreset());
  if (!preset) return;
  $("preset-title").textContent = preset.name;
  $("preset-copy").textContent = preset.description;
  $("preset-badge").textContent = preset.id === "best-original" ? "Default original-first" : preset.downloads ? "Download" : "Inspect only";
  $("url-input").disabled = !preset.needs_url;
  $("check-button").disabled = !preset.needs_url;
  if (!preset.needs_url) {
    $("url-input").placeholder = "This preset uses your authenticated SoundCloud likes.";
  } else {
    $("url-input").placeholder = "https://soundcloud.com/artist/track\nhttps://soundcloud.com/artist/sets/playlist";
  }
}

function renderSettings() {
  const settings = state.settings;
  $("auth-token").value = settings.auth_configured ? settings.masked_auth_token : "";
  $("auth-token").placeholder =
    settings.auth_configured && settings.auth_source === "environment"
      ? "Configured from environment"
      : "Paste token";
  $("archive-enabled").checked = !!settings.archive_enabled;
  $("archive-for-download").checked = !!settings.archive_enabled;
  $("name-format").value = settings.name_format || "";
  $("playlist-format").value = settings.playlist_name_format || "";
  $("max-concurrent").value = settings.max_concurrent_downloads || 1;
  $("artist-folders").checked = !!settings.artist_folders;
  $("flat-folder").checked = !!settings.no_playlist_folder;
  $("original-art").checked = !!settings.original_art;
  $("add-description").checked = !!settings.add_description;
}

function statusClass(status) {
  return `status-${String(status || "pending").toLowerCase().replaceAll(" ", "-")}`;
}

function badgeForStatus(status) {
  const normalized = String(status || "Pending").toLowerCase();
  let className = "neutral";
  if (normalized === "running" || normalized === "done") className = "ok";
  if (normalized === "failed" || normalized === "cancelled") className = "bad";
  if (normalized === "skipped") className = "warn";
  return `<span class="pill ${className}">${escapeHtml(status)}</span>`;
}

function qualityBadge(label) {
  const lower = String(label).toLowerCase();
  let className = "quality-badge";
  if (lower.includes("original")) className += " original";
  else if (lower.includes("lossless") || lower.includes("flac")) className += " flac";
  else if (lower.includes("opus")) className += " opus";
  else if (lower.includes("mp3") || lower.includes("m4a") || lower.includes("aac")) className += " mp3";
  else if (lower.includes("skip")) className += " skipped";
  else if (lower.includes("fail")) className += " failed";
  else className += " neutral";
  return `<span class="${className}">${escapeHtml(label)}</span>`;
}

function renderQueue(queue) {
  state.queue = queue;
  $("queue-pill").textContent = queue.paused ? "Queue paused" : "Queue active";
  $("queue-pill").className = queue.paused ? "pill neutral" : "pill ok";
  const list = $("queue-list");
  if (!queue.items.length) {
    list.className = "queue-list empty-state";
    list.textContent = "No downloads queued yet.";
    return;
  }
  list.className = "queue-list";
  list.innerHTML = queue.items.map(renderQueueItem).join("");
}

function renderQueueItem(item) {
  const badges = (item.summary?.badges || []).map(qualityBadge).join("");
  const files = (item.files || [])
    .slice(0, 6)
    .map((file) => `<span class="quality-badge neutral">${escapeHtml(file.name)} | ${escapeHtml(file.size_label)}</span>`)
    .join("");
  const command = (item.command || []).join(" ");
  const canRetry = ["Failed", "Cancelled", "Skipped", "Done"].includes(item.status);
  const canCancel = ["Pending", "Running"].includes(item.status);
  return `
    <article class="queue-item ${statusClass(item.status)}" data-id="${escapeHtml(item.id)}">
      <div class="queue-head">
        <div class="queue-title">
          <strong>${escapeHtml(item.preset_name)}</strong>
          <span>${escapeHtml(item.target)}</span>
        </div>
        <div class="queue-actions">
          ${badgeForStatus(item.status)}
          ${canRetry ? `<button class="small-button retry-item" data-id="${escapeHtml(item.id)}" type="button">Retry</button>` : ""}
          ${canCancel ? `<button class="danger-button cancel-item" data-id="${escapeHtml(item.id)}" type="button">Cancel</button>` : ""}
        </div>
      </div>
      <div class="progress-shell"><div class="progress-bar"></div></div>
      <div class="summary-grid">${badges}${files}</div>
      <details>
        <summary>Logs and command</summary>
        <pre class="log-view">${escapeHtml(command + "\n\n" + (item.logs || []).join("\n"))}</pre>
        <button class="small-button copy-logs" data-id="${escapeHtml(item.id)}" type="button">Copy Logs</button>
      </details>
    </article>
  `;
}

function renderQuality(data) {
  $("quality-status").textContent = data.return_code === 0 ? "Check complete" : "Check failed";
  $("quality-status").className = data.return_code === 0 ? "pill ok" : "pill bad";
  $("quality-badges").innerHTML = (data.badges || []).map(qualityBadge).join("");
  const list = $("quality-list");
  if (data.qualities?.length) {
    list.className = "quality-list";
    list.innerHTML = data.qualities
      .map(
        (item) => `
          <div class="quality-row">
            <strong>${escapeHtml(item.preset)}</strong>
            <span>${escapeHtml(item.mime)}</span>
            <span>${escapeHtml(item.protocol)}</span>
          </div>
        `,
      )
      .join("");
  } else {
    list.className = "quality-list empty-state";
    list.textContent = "No structured qualities detected. Raw scdl output is shown below.";
  }
  $("quality-raw").textContent = data.raw || "";
  $("quality-raw").classList.remove("hidden");
}

function renderRecent(data) {
  $("download-path").textContent = data.download_dir || "/downloads";
  const list = $("recent-list");
  if (!data.files?.length) {
    list.className = "recent-list empty-state";
    list.textContent = "No files found.";
    return;
  }
  list.className = "recent-list";
  list.innerHTML = data.files
    .map(
      (file) => `
      <div class="recent-row">
        <div>
          <strong>${escapeHtml(file.name)}</strong>
          <span>${escapeHtml(file.folder)} / ${escapeHtml(file.extension.toUpperCase())}</span>
        </div>
        <div>
          <strong>${escapeHtml(file.size_label)}</strong>
          <span>${escapeHtml(file.modified)}</span>
        </div>
      </div>
    `,
    )
    .join("");
}

function renderArchive(data) {
  $("archive-path").textContent = data.path || "/config/archive.txt";
  $("archive-count").textContent = `${data.count || 0} items`;
}

function renderHealth(data) {
  const rows = [
    ["App", data.app?.ok, data.app?.version],
    ["scdl", data.scdl?.ok, `${data.scdl?.version || "unavailable"} ${data.scdl?.path || ""}`],
    ["ffmpeg", data.ffmpeg?.ok, data.ffmpeg?.path || "not found"],
    ["downloads", data.downloads?.ok, data.downloads?.path],
    ["config", data.config?.ok, data.config?.path],
    ["archive", data.archive?.ok, `${data.archive?.path} (${data.archive?.count || 0})`],
    ["logs", data.logs?.ok, data.logs?.path],
  ];
  $("health-list").innerHTML = rows
    .map(
      ([label, ok, text]) => `
        <div class="health-row">
          <span class="pill ${ok ? "ok" : "bad"}">${ok ? "OK" : "Fix"}</span>
          <div><strong>${escapeHtml(label)}</strong><br /><span>${escapeHtml(text || "")}</span></div>
        </div>
      `,
    )
    .join("");
  const allOk = rows.every(([, ok]) => ok);
  $("health-pill").textContent = allOk ? "Healthy" : "Needs attention";
  $("health-pill").className = allOk ? "pill ok" : "pill warn";
}

async function loadInitial() {
  const [presets, settings, queue, health, recent, archive] = await Promise.all([
    api("/api/presets"),
    api("/api/settings"),
    api("/api/queue"),
    api("/api/health"),
    api("/api/recent"),
    api("/api/archive"),
  ]);
  state.presets = presets.presets;
  state.settings = settings;
  renderPresets();
  renderSettings();
  renderQueue(queue);
  renderHealth(health);
  renderRecent(recent);
  renderArchive(archive);
}

async function addToQueue(autostart = false) {
  const preset = selectedPreset();
  if (preset === "check-qualities") {
    await checkQualities();
    return;
  }
  const data = await api("/api/queue", {
    method: "POST",
    body: JSON.stringify({
      urls: currentUrls(),
      preset,
      autostart,
      archive_enabled: archiveForDownload(),
    }),
  });
  renderQueue(data.queue);
  toast(autostart ? "Download started" : "Added to queue", `${data.items.length} item(s)`);
}

async function checkQualities() {
  const urls = currentUrls().split(/\s+/).filter(Boolean);
  if (!urls.length) throw new Error("Paste a SoundCloud URL first");
  $("check-button").disabled = true;
  $("quality-status").textContent = "Checking";
  $("quality-status").className = "pill warn";
  try {
    const data = await api("/api/qualities", {
      method: "POST",
      body: JSON.stringify({ url: urls[0] }),
    });
    renderQuality(data);
    toast("Quality check complete", "Review the result panel before downloading.", "ok");
  } finally {
    $("check-button").disabled = false;
  }
}

async function saveSettings(clearToken = false) {
  const tokenValue = $("auth-token").value.trim();
  const payload = {
    clear_auth_token: clearToken,
    auth_token: clearToken ? "" : tokenValue,
    archive_enabled: $("archive-enabled").checked,
    name_format: $("name-format").value.trim(),
    playlist_name_format: $("playlist-format").value.trim(),
    no_playlist_folder: $("flat-folder").checked,
    artist_folders: $("artist-folders").checked,
    original_art: $("original-art").checked,
    add_description: $("add-description").checked,
    max_concurrent_downloads: Number($("max-concurrent").value || 1),
    default_preset: selectedPreset(),
  };
  state.settings = await api("/api/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  renderSettings();
  toast("Settings saved", "Future queue items will use the new settings.", "ok");
}

async function refreshRecent() {
  renderRecent(await api("/api/recent"));
}

async function refreshHealth() {
  renderHealth(await api("/api/health"));
}

async function refreshArchive() {
  renderArchive(await api("/api/archive"));
}

function confirmModal(title, message, onConfirm) {
  $("modal-title").textContent = title;
  $("modal-copy").textContent = message;
  $("modal-root").classList.remove("hidden");
  const cleanup = () => {
    $("modal-root").classList.add("hidden");
    $("modal-confirm").onclick = null;
    $("modal-cancel").onclick = null;
  };
  $("modal-cancel").onclick = cleanup;
  $("modal-confirm").onclick = async () => {
    cleanup();
    await onConfirm();
  };
}

function wireEvents() {
  $("preset-select").addEventListener("change", renderPresetPanel);
  $("check-button").addEventListener("click", () => checkQualities().catch((error) => toast("Quality check failed", error.message, "bad")));
  $("add-button").addEventListener("click", () => addToQueue(false).catch((error) => toast("Could not add", error.message, "bad")));
  $("start-download-button").addEventListener("click", () => addToQueue(true).catch((error) => toast("Could not start", error.message, "bad")));
  $("queue-start").addEventListener("click", async () => renderQueue(await api("/api/queue/start", { method: "POST", body: "{}" })));
  $("queue-pause").addEventListener("click", async () => renderQueue(await api("/api/queue/pause", { method: "POST", body: "{}" })));
  $("retry-failed").addEventListener("click", async () => renderQueue(await api("/api/queue/retry-failed", { method: "POST", body: "{}" })));
  $("clear-completed").addEventListener("click", async () => renderQueue(await api("/api/queue/clear-completed", { method: "POST", body: "{}" })));
  $("clear-all").addEventListener("click", () =>
    confirmModal("Clear all queue items", "Running downloads will be cancelled and the queue will be emptied.", async () => {
      renderQueue(await api("/api/queue/clear-all", { method: "POST", body: JSON.stringify({ confirm: true }) }));
      toast("Queue cleared", "", "ok");
    }),
  );
  $("save-settings").addEventListener("click", () => saveSettings(false).catch((error) => toast("Settings failed", error.message, "bad")));
  $("clear-token").addEventListener("click", () =>
    confirmModal("Clear auth token", "Private tracks, likes, and some original downloads may stop working.", () => saveSettings(true)),
  );
  $("refresh-recent").addEventListener("click", () => refreshRecent().catch((error) => toast("Refresh failed", error.message, "bad")));
  $("refresh-health").addEventListener("click", () => refreshHealth().catch((error) => toast("Health check failed", error.message, "bad")));
  $("clear-archive").addEventListener("click", () =>
    confirmModal("Clear archive", "This removes the already-downloaded record. Future downloads may duplicate files.", async () => {
      await api("/api/archive/clear", { method: "POST", body: JSON.stringify({ confirm: true }) });
      await refreshArchive();
      toast("Archive cleared", "", "ok");
    }),
  );
  $("archive-import").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/api/archive/import", { method: "POST", body: form });
    if (!response.ok) {
      toast("Import failed", await response.text(), "bad");
      return;
    }
    renderArchive(await response.json());
    toast("Archive imported", "The archive file was replaced.", "ok");
  });
  $("paste-button").addEventListener("click", async () => {
    try {
      $("url-input").value = await navigator.clipboard.readText();
    } catch {
      toast("Clipboard unavailable", "Browser permission was not granted.", "bad");
    }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      addToQueue(true).catch((error) => toast("Could not start", error.message, "bad"));
    }
  });
  $("queue-list").addEventListener("click", async (event) => {
    const retry = event.target.closest(".retry-item");
    const cancel = event.target.closest(".cancel-item");
    const copy = event.target.closest(".copy-logs");
    if (retry) {
      renderQueue(await api(`/api/queue/${retry.dataset.id}/retry`, { method: "POST", body: "{}" }));
    }
    if (cancel) {
      renderQueue(await api(`/api/queue/${cancel.dataset.id}/cancel`, { method: "POST", body: "{}" }));
    }
    if (copy) {
      const item = state.queue.items.find((queueItem) => queueItem.id === copy.dataset.id);
      await navigator.clipboard.writeText((item?.logs || []).join("\n"));
      toast("Logs copied", "", "ok");
    }
  });
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot") {
      renderQueue(data.queue);
      refreshRecent().catch(() => {});
      refreshArchive().catch(() => {});
    }
    if (data.type === "log") {
      const item = state.queue.items.find((queueItem) => queueItem.id === data.item_id);
      if (item) {
        item.logs = [...(item.logs || []), data.line].slice(-200);
        renderQueue(state.queue);
      }
    }
  };
  source.onerror = () => {
    $("queue-pill").textContent = "Live updates reconnecting";
    $("queue-pill").className = "pill warn";
  };
}

wireEvents();
loadInitial()
  .then(connectEvents)
  .catch((error) => toast("App failed to load", error.message, "bad"));
