const runListEl = document.getElementById("run-list");
const runTitleEl = document.getElementById("run-title");
const runMetaEl = document.getElementById("run-meta");
const runVideoEl = document.getElementById("run-video");
const terminalOutputEl = document.getElementById("terminal-output");
const terminalStateEl = document.getElementById("terminal-state");
const terminalCountEl = document.getElementById("terminal-count");
const refreshCatalogButtonEl = document.getElementById("refresh-catalog");
const catalogStateEl = document.getElementById("catalog-state");

let runIndex = [];
let activeRunId = "";
let activeEvents = [];
let terminalAutoFollow = true;

function formatSeconds(value) {
  return `${value.toFixed(1)}s`;
}

function formatStatus(status) {
  return (status || "unknown").replaceAll("_", " ");
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
      <span class="status-chip ${item.replay_status || "unknown"}">${formatStatus(item.replay_status)}</span>
    `;
    button.addEventListener("click", () => {
      loadRun(item.run_id);
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
  // Keep a small tolerance so sub-pixel layout differences do not disable
  // auto-follow when the user is effectively already at the bottom.
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
  renderRunList(runIndex);

  const response = await fetch(`/api/runs/${runId}`);
  if (!response.ok) {
    throw new Error(`Failed to load run ${runId}`);
  }

  const payload = await response.json();
  activeEvents = payload.events || [];
  const run = payload.run;

  runTitleEl.textContent = `${run.label} (${run.video_filename || "missing video"})`;
  runMetaEl.textContent = `${run.started_at_local || "Unknown start"} | ${run.trace_filename || "No trace"} | ${formatStatus(run.replay_status)} | ${run.stop_reason || "unknown stop reason"}`;
  if (run.has_video) {
    runVideoEl.src = `/api/runs/${runId}/video`;
    runVideoEl.load();
  } else {
    runVideoEl.removeAttribute("src");
    runVideoEl.load();
  }
  terminalAutoFollow = true;
  renderTerminalAt(0);
  renderRunList(runIndex);
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
  if (!activeRunId && runIndex.length) {
    activeRunId = runIndex[0].run_id;
  }
  if (activeRunId && !runIndex.some((item) => item.run_id === activeRunId)) {
    activeRunId = runIndex.length ? runIndex[0].run_id : "";
  }
  renderRunList(runIndex);
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

runVideoEl.addEventListener("timeupdate", () => {
  renderTerminalAt(runVideoEl.currentTime || 0);
});

runVideoEl.addEventListener("seeked", () => {
  renderTerminalAt(runVideoEl.currentTime || 0);
});

runVideoEl.addEventListener("loadedmetadata", () => {
  renderTerminalAt(runVideoEl.currentTime || 0);
});

terminalOutputEl.addEventListener("scroll", () => {
  // Once the user scrolls upward, pause auto-follow until they return to the
  // bottom of the terminal. This makes replay scrubbing readable instead of
  // constantly snapping back to the newest printed line.
  syncTerminalAutoFollow();
});

refreshCatalogButtonEl.addEventListener("click", () => {
  refreshCatalog().catch((error) => {
    catalogStateEl.textContent = error.message;
  });
});

loadRunIndex().catch((error) => {
  runListEl.innerHTML = `<div class="empty-state">${error.message}</div>`;
  catalogStateEl.textContent = error.message;
});
