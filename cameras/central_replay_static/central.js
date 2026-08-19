const runListEl = document.getElementById("run-list");
const runTitleEl = document.getElementById("run-title");
const runMetaEl = document.getElementById("run-meta");
const runVideoEl = document.getElementById("run-video");
const terminalOutputEl = document.getElementById("terminal-output");
const terminalStateEl = document.getElementById("terminal-state");
const terminalCountEl = document.getElementById("terminal-count");
const catalogStateEl = document.getElementById("catalog-state");
const workstationFilterEl = document.getElementById("workstation-filter");
const statusFilterEl = document.getElementById("status-filter");
const tagFilterEl = document.getElementById("tag-filter");
const outcomeFilterEl = document.getElementById("outcome-filter");
const refreshRunsEl = document.getElementById("refresh-runs");
const runDetailsEl = document.getElementById("run-details");
const artifactListEl = document.getElementById("artifact-list");
const videoHeightScaleEl = document.getElementById("video-height-scale");
const videoHeightScaleValueEl = document.getElementById("video-height-scale-value");

let workstations = [];
let runIndex = [];
let activeRunId = "";
let activeEvents = [];
let terminalAutoFollow = true;

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

function getRequestedRunId() {
  const params = new URLSearchParams(window.location.search);
  const value = params.get("run_id");
  return value ? value.trim() : "";
}

function updateLocation() {
  const params = new URLSearchParams(window.location.search);
  if (activeRunId) {
    params.set("run_id", activeRunId);
  } else {
    params.delete("run_id");
  }
  const nextUrl = `${window.location.pathname}${params.toString() ? `?${params}` : ""}`;
  window.history.replaceState({}, "", nextUrl);
}

function formatStatus(status) {
  return (status || "unknown").replaceAll("_", " ");
}

function formatSeconds(value) {
  return `${Number(value || 0).toFixed(1)}s`;
}

function renderWorkstationFilter() {
  workstationFilterEl.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = "All workstations";
  workstationFilterEl.appendChild(allOption);
  for (const item of workstations) {
    const option = document.createElement("option");
    option.value = item.workstation_id;
    option.textContent = item.machine_alias || item.hostname || item.workstation_id;
    workstationFilterEl.appendChild(option);
  }
}

function renderRunList() {
  runListEl.innerHTML = "";
  if (!runIndex.length) {
    runListEl.innerHTML = '<div class="empty-state">No central runs matched the current filters.</div>';
    return;
  }
  for (const item of runIndex) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-card${item.central_run_id === activeRunId ? " active" : ""}`;
    button.innerHTML = `
      <h3>${item.label}</h3>
      <p>${item.machine_alias || item.workstation_hostname || item.workstation_id}</p>
      <p>${item.started_at_local || "Unknown start"}</p>
      <p>Video: ${item.video_filename || "Missing"}</p>
      <p>Trace: ${item.trace_filename || "Missing"}</p>
      <span class="status-chip ${item.replay_status || "unknown"}">${formatStatus(item.replay_status)}</span>
    `;
    button.addEventListener("click", () => {
      loadRun(item.central_run_id).catch((error) => {
        catalogStateEl.textContent = error.message;
      });
    });
    runListEl.appendChild(button);
  }
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

function renderRunDetails(payload) {
  const run = payload.run;
  const detailRows = [
    ["Central run", run.central_run_id],
    ["Local run", run.local_run_id || "n/a"],
    ["Workstation", payload.workstation.machine_alias || payload.workstation.hostname || payload.workstation.workstation_id],
    ["Camera", payload.camera_profile.profile_label || payload.camera_profile.profile_key || payload.camera_profile.camera_profile_id],
    ["Status", formatStatus(run.replay_status)],
    ["Duration", formatSeconds(run.duration_sec)],
    ["Stop reason", run.stop_reason || "n/a"],
    ["Ingested", run.last_ingested_utc || "n/a"],
  ];
  runDetailsEl.innerHTML = detailRows.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("");
}

function renderArtifacts(items) {
  artifactListEl.innerHTML = "";
  if (!items.length) {
    artifactListEl.innerHTML = '<li class="empty-state">No artifacts recorded for this run.</li>';
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const link = item.media_url ? `<a href="${item.media_url}" target="_blank" rel="noreferrer">${item.original_filename}</a>` : item.original_filename;
    li.innerHTML = `
      <strong>${item.artifact_type}</strong>
      <span>${link}</span>
      <span>${item.size_bytes} bytes</span>
    `;
    artifactListEl.appendChild(li);
  }
}

async function loadWorkstations() {
  const response = await fetch("/api/workstations");
  if (!response.ok) {
    throw new Error("Failed to load workstations");
  }
  const payload = await response.json();
  workstations = payload.items || [];
  renderWorkstationFilter();
}

async function loadRuns() {
  const params = new URLSearchParams();
  const workstationId = workstationFilterEl.value || "";
  const replayStatus = statusFilterEl.value || "";
  const query = tagFilterEl.value.trim();
  const outcome = outcomeFilterEl.value || "";
  if (workstationId) {
    params.set("workstation_id", workstationId);
  }
  if (replayStatus) {
    params.set("replay_status", replayStatus);
  }
  if (query) {
    params.set("query", query);
  }
  if (outcome) {
    params.set("outcome", outcome);
  }
  params.set("limit", "200");
  const response = await fetch(`/api/runs?${params.toString()}`);
  if (!response.ok) {
    throw new Error("Failed to load central runs");
  }
  const payload = await response.json();
  runIndex = payload.items || [];
  catalogStateEl.textContent = `${runIndex.length} central runs loaded`;
  const requestedRunId = getRequestedRunId();
  if (!activeRunId && requestedRunId && runIndex.some((item) => item.central_run_id === requestedRunId)) {
    activeRunId = requestedRunId;
  }
  if (activeRunId && !runIndex.some((item) => item.central_run_id === activeRunId)) {
    activeRunId = "";
  }
  if (!activeRunId && runIndex.length) {
    activeRunId = runIndex[0].central_run_id;
  }
  renderRunList();
  if (activeRunId) {
    await loadRun(activeRunId);
  } else {
    runTitleEl.textContent = "No run selected";
    runMetaEl.textContent = "Pick a centrally ingested run to open replay.";
    runVideoEl.removeAttribute("src");
    runVideoEl.load();
    activeEvents = [];
    renderTerminalAt(0);
    runDetailsEl.innerHTML = "";
    artifactListEl.innerHTML = "";
  }
}

async function loadRun(centralRunId) {
  activeRunId = centralRunId;
  updateLocation();
  renderRunList();

  const detailResponse = await fetch(`/api/runs/${centralRunId}`);
  if (!detailResponse.ok) {
    throw new Error(`Failed to load run ${centralRunId}`);
  }
  const detailPayload = await detailResponse.json();
  if (detailPayload.run.trace_events_url) {
    const eventsResponse = await fetch(detailPayload.run.trace_events_url);
    if (!eventsResponse.ok) {
      throw new Error(`Failed to load trace events for ${centralRunId}`);
    }
    const eventsPayload = await eventsResponse.json();
    activeEvents = eventsPayload.items || [];
  } else {
    activeEvents = [];
  }

  runTitleEl.textContent = `${detailPayload.run.label} (${detailPayload.run.video_filename || "missing video"})`;
  runMetaEl.textContent =
    `${detailPayload.workstation.machine_alias || detailPayload.workstation.hostname || detailPayload.workstation.workstation_id} | ` +
    `${detailPayload.run.started_at_local || "Unknown start"} | ${formatStatus(detailPayload.run.replay_status)}`;
  if (detailPayload.run.video_url) {
    runVideoEl.src = detailPayload.run.video_url;
    forceMutedPlayback();
    runVideoEl.load();
  } else {
    runVideoEl.removeAttribute("src");
    forceMutedPlayback();
    runVideoEl.load();
  }
  terminalAutoFollow = true;
  renderTerminalAt(0);
  renderRunDetails(detailPayload);
  renderArtifacts(detailPayload.artifacts || []);
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
  terminalAutoFollow = isTerminalNearBottom();
});

workstationFilterEl.addEventListener("change", () => {
  activeRunId = "";
  loadRuns().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

statusFilterEl.addEventListener("change", () => {
  activeRunId = "";
  loadRuns().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

tagFilterEl.addEventListener("search", () => {
  activeRunId = "";
  loadRuns().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

tagFilterEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    activeRunId = "";
    loadRuns().catch((error) => {
      catalogStateEl.textContent = error.message;
    });
  }
});

outcomeFilterEl.addEventListener("change", () => {
  activeRunId = "";
  loadRuns().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

refreshRunsEl.addEventListener("click", () => {
  loadRuns().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

if (videoHeightScaleEl) {
  videoHeightScaleEl.addEventListener("input", () => {
    applyVideoHeightScale(videoHeightScaleEl.value);
  });
}

Promise.all([loadWorkstations(), loadRuns()]).catch((error) => {
  runListEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
  catalogStateEl.textContent = error.message;
});

forceMutedPlayback();
applyVideoHeightScale(videoHeightScaleEl ? videoHeightScaleEl.value : 150);
