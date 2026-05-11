const state = {
  presets: [],
  settings: {},
  queue: { paused: true, items: [] },
  qualityRawVisible: false,
  historyPage: 1,
  historyTotal: 0,
  urlInfo: null,
  urlInspectTimer: null,
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

function firstUrl() {
  return currentUrls().split(/\s+/).filter(Boolean)[0] || "";
}

function archiveForDownload() {
  return $("archive-for-download").checked;
}

function profileTypeLabels() {
  return Object.fromEntries(
    Object.entries(state.settings.profile_download_types || {}).map(([value, config]) => [
      value,
      typeof config === "string" ? config : config.label || value,
    ]),
  );
}

function currentProfileType() {
  return $("profile-type").value || state.urlInfo?.default_profile_type || state.settings.default_profile_download_type || "uploads";
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

function fillSelect(select, values, selected) {
  select.innerHTML = "";
  Object.entries(values || {}).forEach(([value, labelConfig]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = typeof labelConfig === "string" ? labelConfig : labelConfig.label || value;
    select.appendChild(option);
  });
  select.value = selected;
}

function renderUrlInfo(info = state.urlInfo, resetProfile = false) {
  const badge = $("url-type-badge");
  const helper = $("url-helper");
  const profileField = $("profile-type-field");
  const profileSelect = $("profile-type");
  const profileLabels = profileTypeLabels();
  const selectedProfile = resetProfile
    ? info?.default_profile_type || state.settings.default_profile_download_type || "uploads"
    : profileSelect.value || info?.default_profile_type || state.settings.default_profile_download_type || "uploads";
  fillSelect(profileSelect, profileLabels, selectedProfile);

  if (!firstUrl()) {
    badge.textContent = "No URL";
    badge.className = "pill neutral";
    helper.textContent = "Paste a SoundCloud track, playlist, or profile URL.";
    profileField.classList.add("hidden");
    return;
  }

  if (!info || !info.valid) {
    badge.textContent = "Unknown";
    badge.className = "pill bad";
    helper.textContent = info?.message || "Please paste a valid SoundCloud URL.";
    profileField.classList.add("hidden");
    return;
  }

  badge.textContent = info.label || "SoundCloud";
  badge.className = info.is_profile ? "pill warn" : info.is_track ? "pill ok" : "pill neutral";
  helper.textContent = info.message || "";
  profileField.classList.toggle("hidden", !info.is_profile);
}

async function inspectCurrentUrl() {
  const url = firstUrl();
  if (!url) {
    state.urlInfo = null;
    renderUrlInfo(null);
    return null;
  }
  try {
    const info = await api("/api/url-info", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    state.urlInfo = info;
    renderUrlInfo(info, true);
    return info;
  } catch (error) {
    state.urlInfo = { valid: false, message: error.message };
    renderUrlInfo(state.urlInfo);
    return state.urlInfo;
  }
}

function scheduleUrlInspect() {
  clearTimeout(state.urlInspectTimer);
  state.urlInspectTimer = setTimeout(() => inspectCurrentUrl().catch(() => {}), 220);
}

function renderOrganizationPreview(settings) {
  const preview = previewForMode(settings.organization_mode || "library-clean", settings.organization_preview || []);
  $("organization-preview").className = "mini-log";
  $("organization-preview").innerHTML = preview.map((line) => `<code>${escapeHtml(line)}</code>`).join("");
}

function previewForMode(mode, fallback = []) {
  const previews = {
    flat: ["J Dilla - Song Title.flac", "Artist - Track.opus", "Uploader - DJ Edit.mp3"],
    "by-artist": ["Artists/J Dilla/Song Title.flac", "Artists/Artist/Track.opus"],
    "by-playlist": ["Playlists/Beat Tape/001 - Artist - Track.opus", "Singles/Artist - Track.mp3"],
    "by-source-type": ["Likes/J Dilla/Song Title.flac", "Playlists/Beat Tape/001 - Artist - Track.opus", "Singles/Artist/Track.mp3"],
    "scdl-default": ["scdl chooses the original output folders and filenames"],
    "library-clean": [
      "Likes/J Dilla/Song Title.flac",
      "Playlists/Beat Tape/001 - Artist - Track.opus",
      "Artists/Artist/Track.mp3",
      "Profiles/Profile Name/Uploads/Track Title.m4a",
    ],
  };
  return previews[mode] || fallback;
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
  renderUrlInfo();
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
  $("download-delay").value = settings.download_delay_seconds ?? 2;
  $("max-rate-backoff").value = settings.max_rate_limit_backoff_seconds ?? 900;
  $("max-consecutive-rate-limits").value = settings.max_consecutive_rate_limits ?? 8;
  fillSelect($("default-profile-type"), profileTypeLabels(), settings.default_profile_download_type || "uploads");
  $("original-art").checked = !!settings.original_art;
  $("add-description").checked = !!settings.add_description;
  fillSelect($("artist-priority"), settings.artist_priority_modes, settings.artist_metadata_priority || "smart-auto");
  $("preserve-original-metadata").checked = !!settings.preserve_original_metadata;
  $("force-metadata-toggle").checked = !!settings.force_metadata;
  $("save-sidecar-json").checked = !!settings.save_sidecar_json;
  $("embed-soundcloud-tags").checked = !!settings.embed_soundcloud_tags;
  $("parse-artist-title").checked = !!settings.parse_artist_from_title;
  $("search-tags-enabled").checked = !!settings.search_tags_enabled;
  fillSelect($("organization-mode"), settings.organization_modes, settings.organization_mode || "library-clean");
  $("use-playlist-folders").checked = !!settings.use_playlist_folders;
  $("likes-folder").checked = !!settings.put_likes_in_likes_folder;
  $("singles-folder").checked = !!settings.put_singles_in_singles_folder;
  $("sanitize-filenames").checked = !!settings.sanitize_filenames;
  $("include-track-id").checked = !!settings.include_track_id_in_filename;
  $("include-upload-date").checked = !!settings.include_upload_date_in_filename;
  renderOrganizationPreview(settings);
  $("auth-status-badge").textContent = settings.auth_configured ? `Auth: ${settings.auth_source}` : "Auth missing";
  $("auth-status-badge").className = settings.auth_configured ? "pill ok" : "pill warn";
  renderUrlInfo();
}

function statusClass(status) {
  return `status-${String(status || "pending").toLowerCase().replaceAll(" ", "-")}`;
}

function badgeForStatus(status) {
  const normalized = String(status || "Pending").toLowerCase();
  let className = "neutral";
  if (normalized === "running" || normalized === "done") className = "ok";
  if (normalized === "failed" || normalized === "cancelled") className = "bad";
  if (normalized === "skipped" || normalized.includes("rate limited")) className = "warn";
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
  const rateLimited = queue.items.find((item) => String(item.status).toLowerCase().includes("rate limited"));
  $("queue-pill").textContent = rateLimited ? "Paused - rate limited" : queue.paused ? "Queue paused" : "Queue active";
  $("queue-pill").className = rateLimited ? "pill warn" : queue.paused ? "pill neutral" : "pill ok";
  const list = $("queue-list");
  if (!queue.items.length) {
    list.className = "queue-list empty-state";
    list.textContent = "No downloads queued yet.";
    renderLikesCurrent(queue);
    return;
  }
  list.className = "queue-list";
  list.innerHTML = queue.items.map(renderQueueItem).join("");
  renderLikesCurrent(queue);
}

function renderLikesCurrent(queue) {
  const running = queue.items.find((item) => item.is_likes_sync && item.status === "Running");
  const pending = queue.items.find((item) => item.is_likes_sync && item.status === "Pending");
  const rateLimited = queue.items.find((item) => item.is_likes_sync && String(item.status).toLowerCase().includes("rate limited"));
  const item = running || pending || rateLimited;
  const node = $("likes-current");
  if (!item) {
    node.className = "mini-log empty-state";
    node.textContent = "No Likes Sync running.";
    return;
  }
  node.className = "mini-log log-view";
  node.textContent = `${item.status}: ${item.target}\n${(item.logs || []).slice(-8).join("\n")}`;
}

function renderQueueItem(item) {
  const badges = (item.summary?.badges || []).map(qualityBadge).join("");
  const metadata = item.metadata_records?.[0] || {};
  const files = (item.files || [])
    .slice(0, 6)
    .map((file) => `<span class="quality-badge neutral">${escapeHtml(file.name)} | ${escapeHtml(file.size_label)}</span>`)
    .join("");
  const command = (item.command || []).join(" ");
  const isRateLimited = String(item.status).toLowerCase().includes("rate limited");
  const canRetry = ["Failed", "Cancelled", "Skipped", "Done"].includes(item.status) || isRateLimited;
  const canCancel = ["Pending", "Running"].includes(item.status);
  const jobType = item.job_type || item.preset_name;
  const retryAt = item.rate_limit_retry_at ? `Safe to resume after ${item.rate_limit_retry_at}.` : "Try again later; archive will skip completed tracks.";
  const rateLimitNote = isRateLimited
    ? `<div class="rate-limit-note">
        <strong>SoundCloud rate-limited this job.</strong>
        <span>${escapeHtml(retryAt)}</span>
        ${item.last_rate_limit_backoff ? `<span>Current capped backoff: ${escapeHtml(item.last_rate_limit_backoff)}s.</span>` : ""}
      </div>`
    : "";
  return `
    <article class="queue-item ${statusClass(item.status)}" data-id="${escapeHtml(item.id)}">
      <div class="queue-head">
        <div class="queue-title">
          <strong>${escapeHtml(jobType)}</strong>
          <span>${escapeHtml(item.target)}</span>
          <span>${escapeHtml(item.url_kind || "download")}${item.profile_type ? ` / ${escapeHtml(item.profile_type)}` : ""}</span>
        </div>
        <div class="queue-actions">
          ${badgeForStatus(item.status)}
          ${isRateLimited ? `<button class="small-button resume-later" data-id="${escapeHtml(item.id)}" type="button">Resume Later</button>` : ""}
          ${canRetry ? `<button class="small-button retry-item" data-id="${escapeHtml(item.id)}" type="button">${isRateLimited ? "Retry Now" : "Retry"}</button>` : ""}
          ${isRateLimited ? `<button class="danger-button cancel-item" data-id="${escapeHtml(item.id)}" type="button">Stop Job</button>` : ""}
          ${canCancel ? `<button class="danger-button cancel-item" data-id="${escapeHtml(item.id)}" type="button">Cancel</button>` : ""}
        </div>
      </div>
      <div class="progress-shell"><div class="progress-bar"></div></div>
      ${rateLimitNote}
      <div class="summary-grid">${badges}${files}</div>
      ${metadata.title ? `<p class="metadata-line">${escapeHtml(metadata.artist || metadata.uploader || "Unknown Artist")} - ${escapeHtml(metadata.title)}</p>` : ""}
      <details>
        <summary>Logs and command</summary>
        <pre class="log-view">${escapeHtml(command + "\n\n" + (item.logs || []).join("\n"))}</pre>
        <button class="small-button copy-command" data-id="${escapeHtml(item.id)}" type="button">Copy Command</button>
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
          <span>${escapeHtml(file.metadata?.artist || file.folder)} / ${escapeHtml(file.extension.toUpperCase())}</span>
          ${file.metadata?.tags?.length ? `<span>${escapeHtml(file.metadata.tags.slice(0, 6).join(", "))}</span>` : ""}
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
    ["history", data.history?.ok, data.history?.path || ""],
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

function renderStats(data) {
  $("stat-archive").textContent = data.archive_count ?? 0;
  $("stat-history").textContent = data.history_count ?? 0;
  $("stat-metadata").textContent = data.metadata_count ?? 0;
  $("stat-processed").textContent = data.total_processed ?? 0;
  $("stat-downloaded").textContent = data.downloaded ?? 0;
  $("stat-skipped").textContent = data.skipped ?? 0;
  $("stat-failed").textContent = data.failed ?? 0;
  $("stat-rate-limited").textContent = data.rate_limited ?? 0;
  $("stat-remaining").textContent = data.remaining_unknown ?? 0;
  const failures = data.recent_failures || [];
  const failureNode = $("recent-failures");
  if (!failures.length) {
    failureNode.className = "history-list empty-state";
    failureNode.textContent = "No recent failures.";
    return;
  }
  failureNode.className = "history-list";
  failureNode.innerHTML = failures
    .map(
      (item) => `
        <div class="history-row">
          <div>
            <strong>${escapeHtml(item.job_type || item.preset_name)}</strong>
            <span>${escapeHtml(item.target_url || item.target)}</span>
            ${item.last_error ? `<span>${escapeHtml(item.last_error)}</span>` : ""}
          </div>
          <div>${badgeForStatus(item.status)}</div>
        </div>
      `,
    )
    .join("");
}

function renderHistory(data) {
  state.historyPage = data.page || 1;
  state.historyTotal = data.total || 0;
  $("history-page").textContent = `Page ${state.historyPage} (${state.historyTotal} total)`;
  $("history-prev").disabled = state.historyPage <= 1;
  $("history-next").disabled = state.historyPage * (data.page_size || 25) >= state.historyTotal;
  const list = $("history-list");
  if (!data.items?.length) {
    list.className = "history-list empty-state";
    list.textContent = "No matching history yet.";
    return;
  }
  list.className = "history-list";
  list.innerHTML = data.items
    .map((item) => {
      const metadata = item.metadata_records?.[0] || {};
      const headline = metadata.title
        ? `${metadata.artist || metadata.uploader || "Unknown Artist"} - ${metadata.title}`
        : item.job_type || item.preset_name;
      const tagLine = [metadata.genre, ...(metadata.tags || []).slice(0, 5)].filter(Boolean).join(", ");
      return `
        <div class="history-row">
          <div>
            <strong>${escapeHtml(headline)}</strong>
            <span>${escapeHtml(metadata.output_path || item.target_url || item.target)}</span>
            ${tagLine ? `<span>${escapeHtml(tagLine)}</span>` : ""}
            ${item.last_error ? `<span>${escapeHtml(item.last_error)}</span>` : ""}
          </div>
          <div>
            ${badgeForStatus(item.status)}
            <span>${escapeHtml(item.updated_at || "")}</span>
          </div>
        </div>
      `;
    })
    .join("");
}

async function loadInitial() {
  const [presets, settings, queue, health, recent, archive, stats, history] = await Promise.all([
    api("/api/presets"),
    api("/api/settings"),
    api("/api/queue"),
    api("/api/health"),
    api("/api/recent"),
    api("/api/archive"),
    api("/api/stats"),
    api("/api/history?page=1&page_size=25"),
  ]);
  state.presets = presets.presets;
  state.settings = settings;
  renderPresets();
  renderSettings();
  renderQueue(queue);
  renderHealth(health);
  renderRecent(recent);
  renderArchive(archive);
  renderStats(stats);
  renderHistory(history);
}

async function addToQueue(autostart = false) {
  const preset = selectedPreset();
  if (preset === "check-qualities") {
    await checkQualities();
    return;
  }
  const info = await inspectCurrentUrl();
  if (info?.is_profile && !$("profile-type").value) {
    throw new Error("Profile URLs require a download type. Choose Uploads, All Tracks + Reposts, Likes, Playlists, or Reposts.");
  }
  const data = await api("/api/queue", {
    method: "POST",
    body: JSON.stringify({
      urls: currentUrls(),
      preset,
      autostart,
      archive_enabled: archiveForDownload(),
      profile_type: info?.is_profile ? currentProfileType() : null,
    }),
  });
  renderQueue(data.queue);
  toast(autostart ? "Download started" : "Added to queue", `${data.items.length} item(s)`);
}

async function checkQualities() {
  const urls = currentUrls().split(/\s+/).filter(Boolean);
  if (!urls.length) throw new Error("Paste a SoundCloud URL first");
  const info = await inspectCurrentUrl();
  if (!info?.is_track) {
    throw new Error("Check Qualities is for individual track URLs. For profiles, choose a profile download type and start a download.");
  }
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

async function saveSettings(clearToken = false, options = {}) {
  const tokenValue = $("auth-token").value.trim();
  const payload = {
    clear_auth_token: clearToken,
    auth_token: clearToken ? "" : tokenValue,
    archive_enabled: $("archive-enabled").checked,
    name_format: $("name-format").value.trim(),
    playlist_name_format: $("playlist-format").value.trim(),
    no_playlist_folder: !$("use-playlist-folders").checked,
    original_art: $("original-art").checked,
    add_description: $("add-description").checked,
    artist_metadata_priority: $("artist-priority").value,
    preserve_original_metadata: $("preserve-original-metadata").checked,
    force_metadata: $("force-metadata-toggle").checked,
    save_sidecar_json: $("save-sidecar-json").checked,
    embed_soundcloud_tags: $("embed-soundcloud-tags").checked,
    parse_artist_from_title: $("parse-artist-title").checked,
    search_tags_enabled: $("search-tags-enabled").checked,
    organization_mode: $("organization-mode").value,
    use_playlist_folders: $("use-playlist-folders").checked,
    put_likes_in_likes_folder: $("likes-folder").checked,
    put_singles_in_singles_folder: $("singles-folder").checked,
    sanitize_filenames: $("sanitize-filenames").checked,
    include_track_id_in_filename: $("include-track-id").checked,
    include_upload_date_in_filename: $("include-upload-date").checked,
    max_concurrent_downloads: Number($("max-concurrent").value || 1),
    download_delay_seconds: Number($("download-delay").value || 0),
    max_rate_limit_backoff_seconds: Number($("max-rate-backoff").value || 900),
    max_consecutive_rate_limits: Number($("max-consecutive-rate-limits").value || 8),
    default_profile_download_type: $("default-profile-type").value,
    default_preset: selectedPreset(),
  };
  state.settings = await api("/api/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  renderSettings();
  if (!options.silent) {
    toast("Settings saved", "Future queue items will use the new settings.", "ok");
  }
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

async function refreshStats() {
  renderStats(await api("/api/stats"));
}

async function refreshHistory(page = state.historyPage) {
  const params = new URLSearchParams({
    status: $("history-filter").value,
    search: $("history-search").value,
    page: String(page),
    page_size: "25",
  });
  renderHistory(await api(`/api/history?${params}`));
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
  $("preset-select").addEventListener("change", () => {
    renderPresetPanel();
    inspectCurrentUrl().catch(() => {});
  });
  $("url-input").addEventListener("input", scheduleUrlInspect);
  $("profile-type").addEventListener("change", () => renderUrlInfo());
  $("check-button").addEventListener("click", () => checkQualities().catch((error) => toast("Quality check failed", error.message, "bad")));
  $("add-button").addEventListener("click", () => addToQueue(false).catch((error) => toast("Could not add", error.message, "bad")));
  $("start-download-button").addEventListener("click", () => addToQueue(true).catch((error) => toast("Could not start", error.message, "bad")));
  $("queue-start").addEventListener("click", async () => renderQueue(await api("/api/queue/start", { method: "POST", body: "{}" })));
  $("queue-pause").addEventListener("click", async () => renderQueue(await api("/api/queue/pause", { method: "POST", body: "{}" })));
  $("retry-failed").addEventListener("click", async () => renderQueue(await api("/api/queue/retry-failed", { method: "POST", body: "{}" })));
  $("stop-after-current").addEventListener("click", async () => {
    renderQueue(await api("/api/queue/stop-after-current", { method: "POST", body: "{}" }));
    toast("Stop requested", "Pending work was cancelled; a running item will finish first.", "ok");
  });
  $("likes-start").addEventListener("click", () =>
    api("/api/likes/resume", { method: "POST", body: "{}" })
      .then((data) => {
        renderQueue(data.queue);
        renderStats(data.stats);
        toast("Likes Sync started", "Archive resume is enabled.", "ok");
      })
      .catch((error) => toast("Likes Sync failed", error.message, "bad")),
  );
  $("likes-retry-failed").addEventListener("click", () =>
    api("/api/likes/retry-failed", { method: "POST", body: "{}" })
      .then((data) => {
        renderQueue(data.queue);
        renderStats(data.stats);
        toast("Retrying failed Likes Sync", "Previously archived tracks will be skipped.", "ok");
      })
      .catch((error) => toast("Retry failed", error.message, "bad")),
  );
  $("clear-completed").addEventListener("click", async () => renderQueue(await api("/api/queue/clear-completed", { method: "POST", body: "{}" })));
  $("clear-all").addEventListener("click", () =>
    confirmModal("Clear all queue items", "Running downloads will be cancelled and the queue will be emptied.", async () => {
      renderQueue(await api("/api/queue/clear-all", { method: "POST", body: JSON.stringify({ confirm: true }) }));
      toast("Queue cleared", "", "ok");
    }),
  );
  $("save-settings").addEventListener("click", () => saveSettings(false).catch((error) => toast("Settings failed", error.message, "bad")));
  $("organization-mode").addEventListener("change", () => {
    $("organization-preview").innerHTML = previewForMode($("organization-mode").value).map((line) => `<code>${escapeHtml(line)}</code>`).join("");
  });
  $("test-auth").addEventListener("click", async () => {
    try {
      const tokenValue = $("auth-token").value.trim();
      if (tokenValue && tokenValue !== "********") {
        await saveSettings(false, { silent: true });
      }
      const result = await api("/api/auth/test", { method: "POST", body: "{}" });
      toast(result.ok ? "Auth works" : "Auth failed", result.user || result.message, result.ok ? "ok" : "bad");
      $("auth-status-badge").textContent = result.ok ? "Auth valid" : "Auth failed";
      $("auth-status-badge").className = result.ok ? "pill ok" : "pill bad";
    } catch (error) {
      toast("Auth check failed", error.message, "bad");
    }
  });
  $("clear-token").addEventListener("click", () =>
    confirmModal("Clear auth token", "Private tracks, likes, and some original downloads may stop working.", () => saveSettings(true)),
  );
  $("refresh-recent").addEventListener("click", () => refreshRecent().catch((error) => toast("Refresh failed", error.message, "bad")));
  $("refresh-health").addEventListener("click", () => refreshHealth().catch((error) => toast("Health check failed", error.message, "bad")));
  $("refresh-history").addEventListener("click", () => refreshHistory(1).catch((error) => toast("History failed", error.message, "bad")));
  $("history-filter").addEventListener("change", () => refreshHistory(1).catch((error) => toast("History failed", error.message, "bad")));
  $("history-search").addEventListener("input", () => {
    clearTimeout(window.historySearchTimer);
    window.historySearchTimer = setTimeout(() => refreshHistory(1).catch(() => {}), 350);
  });
  $("history-prev").addEventListener("click", () => refreshHistory(Math.max(1, state.historyPage - 1)));
  $("history-next").addEventListener("click", () => refreshHistory(state.historyPage + 1));
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
      await inspectCurrentUrl();
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
    const copyCommand = event.target.closest(".copy-command");
    const resumeLater = event.target.closest(".resume-later");
    if (retry) {
      renderQueue(await api(`/api/queue/${retry.dataset.id}/retry`, { method: "POST", body: "{}" }));
    }
    if (cancel) {
      renderQueue(await api(`/api/queue/${cancel.dataset.id}/cancel`, { method: "POST", body: "{}" }));
    }
    if (resumeLater) {
      renderQueue(await api("/api/queue/pause", { method: "POST", body: "{}" }));
      toast("Paused safely", "Resume later will reuse archive and history.", "ok");
    }
    if (copyCommand) {
      const item = state.queue.items.find((queueItem) => queueItem.id === copyCommand.dataset.id);
      await navigator.clipboard.writeText((item?.command || []).join(" "));
      toast("Command copied", "Auth token is masked.", "ok");
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
      refreshStats().catch(() => {});
      refreshHistory().catch(() => {});
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
