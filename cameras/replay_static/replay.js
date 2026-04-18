const runListEl = document.getElementById("run-list");
const runTitleEl = document.getElementById("run-title");
const runMetaEl = document.getElementById("run-meta");
const runVideoEl = document.getElementById("run-video");
const terminalOutputEl = document.getElementById("terminal-output");
const terminalStateEl = document.getElementById("terminal-state");
const terminalCountEl = document.getElementById("terminal-count");
const refreshCatalogButtonEl = document.getElementById("refresh-catalog");
const refreshLiveButtonEl = document.getElementById("refresh-live");
const catalogStateEl = document.getElementById("catalog-state");
const sidebarCopyEl = document.getElementById("sidebar-copy");
const replayPanelEl = document.getElementById("replay-panel");
const livePanelEl = document.getElementById("live-panel");
const modeReplayEl = document.getElementById("mode-replay");
const modeLiveEl = document.getElementById("mode-live");
const liveImageEl = document.getElementById("live-image");
const liveStateEl = document.getElementById("live-state");
const liveCountEl = document.getElementById("live-count");
const liveMetaEl = document.getElementById("live-meta");
const videoHeightScaleEl = document.getElementById("video-height-scale");
const videoHeightScaleValueEl = document.getElementById("video-height-scale-value");

let runIndex = [];
let liveProfiles = [];
let activeRunId = "";
let activeProfileId = "";
let activeEvents = [];
let terminalAutoFollow = true;
let activeMode = "replay";
let liveRefreshMs = 1000;
let liveRefreshTimer = null;

function forceMutedPlayback() {
  runVideoEl.muted = true;
  runVideoEl.defaultMuted = true;
  runVideoEl.volume = 0;
}

function applyVideoHeightScale(value) {
  const scale = Math.max(1, Number(value || 150) / 100);
  document.documentElement.style.setProperty("--video-pane-row", `${scale}fr`);
  if (videoHeightScaleValueEl) {
    videoHeightScaleValueEl.textContent = `${scale.toFixed(1)}x`;
  }
  if (videoHeightScaleEl) {
    videoHeightScaleEl.value = String(Math.round(scale * 100));
  }
}

function getQueryParam(name) {
  const params = new URLSearchParams(window.location.search);
  const value = params.get(name);
  return value ? value.trim() : "";
}

function getRequestedRunId() {
  return getQueryParam("run_id");
}

function getRequestedProfileId() {
  return getQueryParam("profile");
}

function getRequestedMode() {
  const mode = getQueryParam("mode");
  return mode === "live" ? "live" : "replay";
}

function updateLocation() {
  const params = new URLSearchParams(window.location.search);
  params.set("mode", activeMode);
  if (activeMode === "live") {
    if (activeProfileId) {
      params.set("profile", activeProfileId);
    } else {
      params.delete("profile");
    }
  } else {
    params.delete("profile");
  }

  if (activeRunId) {
    params.set("run_id", activeRunId);
  } else {
    params.delete("run_id");
  }

  const nextUrl = `${window.location.pathname}${params.toString() ? `?${params}` : ""}`;
  window.history.replaceState({}, "", nextUrl);
}

function formatSeconds(value) {
  return `${value.toFixed(1)}s`;
}

function formatStatus(status) {
  return (status || "unknown").replaceAll("_", " ");
}

function formatSegmentSummary(item) {
  const segmentCount = Number(item.segment_count || 0);
  const idleCount = Number(item.idle_segment_count || 0);
  if (!segmentCount) {
    return "Segments: pending";
  }
  return `Segments: ${segmentCount} total, ${idleCount} idle`;
}

function setActiveMode(mode) {
  activeMode = mode === "live" ? "live" : "replay";
  modeReplayEl.classList.toggle("active", activeMode === "replay");
  modeReplayEl.setAttribute("aria-selected", activeMode === "replay" ? "true" : "false");
  modeLiveEl.classList.toggle("active", activeMode === "live");
  modeLiveEl.setAttribute("aria-selected", activeMode === "live" ? "true" : "false");
  replayPanelEl.hidden = activeMode !== "replay";
  livePanelEl.hidden = activeMode !== "live";
  refreshCatalogButtonEl.hidden = activeMode !== "replay";
  refreshLiveButtonEl.hidden = activeMode !== "live";
  sidebarCopyEl.textContent =
    activeMode === "live"
      ? "Pick a configured camera profile to open a local live preview."
      : "Pick a captured run to inspect its video and trace.";
  renderSidebar();
  updateLocation();
  if (activeMode === "live") {
    scheduleLiveRefresh(true);
  } else {
    cancelLiveRefresh();
  }
}

function renderRunList(items) {
  runListEl.innerHTML = "";
  if (!items.length) {
    runListEl.innerHTML = '<div class="empty-state">No cataloged runs were found.</div>';
    return;
  }

  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-card${item.run_id === activeRunId ? " active" : ""}`;
    button.innerHTML = `
      <h3>${item.label}</h3>
      <p>${item.started_at_local || "Unknown start"}</p>
      <p>Video: ${item.video_filename || "Missing video"}</p>
      <p>Trace: ${item.trace_filename || "Missing trace"}</p>
      <p>Gate: ${item.process_gate || "unknown"}</p>
      <p>${formatSegmentSummary(item)}</p>
      <span class="status-chip ${item.replay_status || "unknown"}">${formatStatus(item.replay_status)}</span>
    `;
    button.addEventListener("click", () => {
      loadRun(item.run_id);
    });
    runListEl.appendChild(button);
  }
}

function renderLiveProfiles(items) {
  runListEl.innerHTML = "";
  if (!items.length) {
    runListEl.innerHTML = '<div class="empty-state">No camera profiles are configured.</div>';
    return;
  }

  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-card${item.id === activeProfileId ? " active" : ""}`;
    button.innerHTML = `
      <h3>${item.label}</h3>
      <p>Profile: ${item.id}</p>
      <p>Source: ${item.source || "Missing source"}</p>
      <p>Frame rate: ${item.framerate || "default"}</p>
      <p>Size: ${item.video_size || "default"}</p>
      <span class="status-chip ready">live preview</span>
    `;
    button.addEventListener("click", () => {
      loadLiveProfile(item.id);
    });
    runListEl.appendChild(button);
  }
}

function renderSidebar() {
  if (activeMode === "live") {
    renderLiveProfiles(liveProfiles);
    return;
  }
  renderRunList(runIndex);
}

function visibleLinesAt(currentTimeSec) {
  const visible = [];
  for (const event of activeEvents) {
    if (event.elapsed_sec <= currentTimeSec + 0.001) {
      visible.push(event.line);
    } else {
      break;
    }
  }
  return visible;
}

function isTerminalNearBottom() {
  const remaining = terminalOutputEl.scrollHeight - terminalOutputEl.scrollTop - terminalOutputEl.clientHeight;
  return remaining <= 8;
}

function syncTerminalAutoFollow() {
  terminalAutoFollow = isTerminalNearBottom();
}

function renderTerminalAt(currentTimeSec) {
  const shouldFollow = terminalAutoFollow || isTerminalNearBottom();
  const visible = visibleLinesAt(currentTimeSec);
  terminalOutputEl.textContent = visible.join("\n");
  terminalStateEl.textContent = `Trace lines at playback time ${formatSeconds(currentTimeSec)}`;
  terminalCountEl.textContent = `${visible.length} of ${activeEvents.length} lines`;
  if (shouldFollow) {
    terminalOutputEl.scrollTop = terminalOutputEl.scrollHeight;
    terminalAutoFollow = true;
  }
}

async function loadRun(runId) {
  activeRunId = runId;
  updateLocation();
  renderSidebar();

  const response = await fetch(`/api/runs/${runId}`);
  if (!response.ok) {
    throw new Error(`Failed to load run ${runId}`);
  }

  const payload = await response.json();
  activeEvents = payload.events || [];
  const run = payload.run;
  const chapters = payload.chapters || [];
  const segments = payload.segments || [];

  runTitleEl.textContent = `${run.label} (${run.video_filename || "missing video"})`;
  runMetaEl.textContent = `${run.started_at_local || "Unknown start"} | ${run.trace_filename || "No trace"} | ${formatStatus(run.replay_status)} | ${run.stop_reason || "unknown stop reason"} | ${segments.length} segments / ${chapters.length} chapters`;
  if (run.has_video) {
    runVideoEl.src = `/api/runs/${runId}/video`;
    forceMutedPlayback();
    runVideoEl.load();
  } else {
    runVideoEl.removeAttribute("src");
    forceMutedPlayback();
    runVideoEl.load();
  }
  terminalAutoFollow = true;
  renderTerminalAt(0);
  renderSidebar();
}

async function loadRunIndex() {
  const response = await fetch("/api/runs");
  if (!response.ok) {
    throw new Error("Failed to load run index");
  }

  const payload = await response.json();
  runIndex = payload.items || [];
  const runCount = runIndex.length;
  const readyCount = runIndex.filter((item) => item.replay_status === "ready").length;
  catalogStateEl.textContent = `${readyCount} of ${runCount} cataloged runs are replay-ready`;
  const requestedRunId = getRequestedRunId();
  if (!activeRunId && requestedRunId && runIndex.some((item) => item.run_id === requestedRunId)) {
    activeRunId = requestedRunId;
  }
  if (!activeRunId && runIndex.length) {
    activeRunId = runIndex[0].run_id;
  }
  if (activeRunId && !runIndex.some((item) => item.run_id === activeRunId)) {
    activeRunId = runIndex.length ? runIndex[0].run_id : "";
  }
  renderSidebar();
  if (activeRunId) {
    await loadRun(activeRunId);
  }
}

async function refreshCatalog() {
  refreshCatalogButtonEl.disabled = true;
  catalogStateEl.textContent = "Refreshing replay catalog...";
  try {
    const response = await fetch("/api/catalog/refresh");
    if (!response.ok) {
      throw new Error("Failed to refresh replay catalog");
    }
    const payload = await response.json();
    catalogStateEl.textContent = `Catalog refreshed at ${payload.refreshed_at_utc} with ${payload.run_count} runs`;
    await loadRunIndex();
  } finally {
    refreshCatalogButtonEl.disabled = false;
  }
}

async function loadLiveProfiles() {
  const response = await fetch("/api/live/profiles");
  if (!response.ok) {
    throw new Error("Failed to load live camera profiles");
  }

  const payload = await response.json();
  liveProfiles = payload.items || [];
  liveRefreshMs = Math.max(250, Number(payload.refresh_ms || 1000));
  liveCountEl.textContent = `${liveProfiles.length} profiles`;

  const requestedProfileId = getRequestedProfileId();
  if (!activeProfileId && requestedProfileId && liveProfiles.some((item) => item.id === requestedProfileId)) {
    activeProfileId = requestedProfileId;
  }
  if (!activeProfileId) {
    activeProfileId = payload.default_profile || (liveProfiles[0] ? liveProfiles[0].id : "");
  }
  if (activeProfileId && !liveProfiles.some((item) => item.id === activeProfileId)) {
    activeProfileId = liveProfiles.length ? liveProfiles[0].id : "";
  }

  renderSidebar();
  if (activeMode === "live") {
    await loadLiveProfile(activeProfileId, { skipSchedule: true });
  }
}

function buildLiveFrameUrl() {
  const params = new URLSearchParams();
  if (activeProfileId) {
    params.set("profile", activeProfileId);
  }
  params.set("_ts", String(Date.now()));
  return `/api/live/frame.jpg?${params}`;
}

function cancelLiveRefresh() {
  if (liveRefreshTimer) {
    window.clearTimeout(liveRefreshTimer);
    liveRefreshTimer = null;
  }
}

function scheduleLiveRefresh(immediate = false) {
  cancelLiveRefresh();
  if (activeMode !== "live" || !activeProfileId) {
    return;
  }
  const delay = immediate ? 0 : liveRefreshMs;
  liveRefreshTimer = window.setTimeout(() => {
    refreshLiveFrame().catch((error) => {
      liveStateEl.textContent = error.message;
      liveMetaEl.textContent = "Live preview capture failed. Check the camera profile, ffmpeg path, or whether another process is holding the device.";
      scheduleLiveRefresh(false);
    });
  }, delay);
}

async function refreshLiveFrame() {
  if (!activeProfileId) {
    liveStateEl.textContent = "No live profile selected";
    liveMetaEl.textContent = "Pick a configured camera profile from the left panel.";
    return;
  }

  const profile = liveProfiles.find((item) => item.id === activeProfileId);
  if (!profile) {
    throw new Error(`Unknown live profile: ${activeProfileId}`);
  }

  liveStateEl.textContent = `Refreshing live preview for ${profile.label}...`;
  liveMetaEl.textContent = `${profile.source || "Missing source"} | ${profile.video_size || "default size"} | ${profile.framerate || "default fps"}`;

  const response = await fetch(buildLiveFrameUrl(), { cache: "no-store" });
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || "Failed to refresh live preview");
  }

  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const previousUrl = liveImageEl.dataset.objectUrl;
  liveImageEl.src = objectUrl;
  liveImageEl.dataset.objectUrl = objectUrl;
  if (previousUrl) {
    URL.revokeObjectURL(previousUrl);
  }
  liveStateEl.textContent = `Live preview active: ${profile.label}`;
  scheduleLiveRefresh(false);
}

async function loadLiveProfile(profileId, options = {}) {
  activeProfileId = profileId || "";
  updateLocation();
  renderSidebar();
  const profile = liveProfiles.find((item) => item.id === activeProfileId);
  if (!profile) {
    liveStateEl.textContent = "No live profile selected";
    liveMetaEl.textContent = "Pick a configured camera profile from the left panel.";
    return;
  }

  runTitleEl.textContent = `${profile.label} (live preview)`;
  runMetaEl.textContent = `${profile.source || "Missing source"} | refresh every ${(liveRefreshMs / 1000).toFixed(1)}s`;
  liveStateEl.textContent = `Preparing live preview for ${profile.label}`;
  liveMetaEl.textContent = `${profile.source || "Missing source"} | ${profile.video_size || "default size"} | ${profile.framerate || "default fps"}`;
  if (!options.skipSchedule) {
    scheduleLiveRefresh(true);
    return;
  }
  await refreshLiveFrame();
}

runVideoEl.addEventListener("timeupdate", () => {
  renderTerminalAt(runVideoEl.currentTime || 0);
});

runVideoEl.addEventListener("seeked", () => {
  renderTerminalAt(runVideoEl.currentTime || 0);
});

runVideoEl.addEventListener("loadedmetadata", () => {
  forceMutedPlayback();
  renderTerminalAt(runVideoEl.currentTime || 0);
});

runVideoEl.addEventListener("volumechange", () => {
  forceMutedPlayback();
});

terminalOutputEl.addEventListener("scroll", () => {
  syncTerminalAutoFollow();
});

refreshCatalogButtonEl.addEventListener("click", () => {
  refreshCatalog().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

refreshLiveButtonEl.addEventListener("click", () => {
  scheduleLiveRefresh(true);
});

if (videoHeightScaleEl) {
  videoHeightScaleEl.addEventListener("input", () => {
    applyVideoHeightScale(videoHeightScaleEl.value);
  });
}

modeReplayEl.addEventListener("click", () => {
  setActiveMode("replay");
});

modeLiveEl.addEventListener("click", () => {
  setActiveMode("live");
});

window.addEventListener("beforeunload", () => {
  cancelLiveRefresh();
  const objectUrl = liveImageEl.dataset.objectUrl;
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
  }
});

Promise.all([loadRunIndex(), loadLiveProfiles()])
  .then(() => {
    forceMutedPlayback();
    applyVideoHeightScale(videoHeightScaleEl ? videoHeightScaleEl.value : 150);
    setActiveMode(getRequestedMode());
    return null;
  })
  .catch((error) => {
    runListEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
    catalogStateEl.textContent = error.message;
  });
