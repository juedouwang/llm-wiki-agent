"use strict";

const state = {
  projects: [],
  selectedProjectId: null,
  detail: null,
  filter: "",
};

const ids = [
  "system-banner", "project-count", "project-search", "project-list", "project-id",
  "project-title", "project-meta", "project-health", "summary-grid", "artifact-count",
  "artifact-grid", "gaps-list", "coverage-totals", "coverage-panel", "files-table",
  "claim-count", "claims-list", "planning-panel", "run-count", "runs-list",
  "viewer-title", "evidence-viewer", "markdown-viewer", "refresh-button", "close-viewer",
];
const ui = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function appendText(parent, tag, className, text) {
  const node = element(tag, className, text);
  parent.appendChild(node);
  return node;
}

function statusCode(value) {
  const normalized = String(value || "draft").toLowerCase();
  return ["draft", "verified", "stale", "conflicting", "rejected"].includes(normalized)
    ? normalized
    : "draft";
}

function statusChip(value) {
  const code = statusCode(value);
  return element("span", `status-chip status-${code}`, code.toUpperCase());
}

function emptyState(text, error) {
  return element("div", `empty-state${error ? " error-state" : ""}`, text);
}

function number(value, fallback = 0) {
  return Number.isFinite(Number(value)) ? Number(value) : fallback;
}

function formatBytes(value) {
  const bytes = number(value, 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GiB`;
}

async function getJson(path) {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const code = payload.error || `HTTP ${response.status}`;
    throw new Error(code);
  }
  return payload;
}

function setBanner(message, error = false) {
  ui["system-banner"].textContent = message;
  ui["system-banner"].classList.toggle("error-state", error);
}

function renderProjects() {
  clear(ui["project-list"]);
  const needle = state.filter.trim().toLowerCase();
  const projects = state.projects.filter((project) => {
    const haystack = `${project.name || ""} ${project.project_id || ""}`.toLowerCase();
    return !needle || haystack.includes(needle);
  });
  ui["project-count"].textContent = String(state.projects.length);
  if (!projects.length) {
    ui["project-list"].appendChild(emptyState(state.projects.length ? "No matching projects." : "No registered projects yet."));
    return;
  }
  projects.forEach((project) => {
    const button = element("button", "project-button");
    button.type = "button";
    button.setAttribute("role", "listitem");
    if (project.project_id === state.selectedProjectId) button.classList.add("active");
    appendText(button, "strong", "", project.name || project.project_id);
    const gapCount = Array.isArray(project.gaps) ? project.gaps.length : 0;
    appendText(button, "span", "", `${project.project_id} · ${gapCount} gap${gapCount === 1 ? "" : "s"}`);
    button.addEventListener("click", () => selectProject(project.project_id));
    ui["project-list"].appendChild(button);
  });
}

function summaryCard(label, value, note) {
  const card = element("article", "summary-card");
  appendText(card, "div", "label", label);
  appendText(card, "div", "value", value);
  appendText(card, "div", "note", note);
  return card;
}

function renderSummary(detail) {
  clear(ui["summary-grid"]);
  const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
  const available = artifacts.filter((item) => item.availability === "available").length;
  const totals = detail.coverage && detail.coverage.totals ? detail.coverage.totals : {};
  const cards = [
    ["Knowledge", `${available} / 15`, "available product artifacts"],
    ["Files", number(detail.files && detail.files.count), `${formatBytes(totals.byte_count)} inventoried`],
    ["Claims", number(detail.claims && detail.claims.count), "with Evidence trace where declared"],
    ["Tasks", number(detail.planning && detail.planning.tasks && detail.planning.tasks.length), "not execution authorization"],
    ["Runs", number(detail.runs && detail.runs.count), "duration, errors, and usage ledger"],
  ];
  cards.forEach((item) => ui["summary-grid"].appendChild(summaryCard(item[0], item[1], item[2])));
}

function renderArtifacts(detail) {
  clear(ui["artifact-grid"]);
  const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
  ui["artifact-count"].textContent = `${artifacts.filter((item) => item.availability === "available").length} / 15`;
  artifacts.forEach((artifact) => {
    const button = element("button", "artifact-button");
    button.type = "button";
    button.appendChild(statusChip(artifact.status_code || artifact.status));
    const copy = element("span", "artifact-copy");
    appendText(copy, "strong", "", artifact.title || artifact.path);
    appendText(copy, "span", "", artifact.path);
    button.appendChild(copy);
    button.addEventListener("click", () => loadKnowledge(artifact.path));
    ui["artifact-grid"].appendChild(button);
  });
  const daily = detail.daily_plans && detail.daily_plans.today;
  if (daily) {
    const button = element("button", "artifact-button");
    button.type = "button";
    button.appendChild(statusChip(daily.status_code || daily.status));
    const copy = element("span", "artifact-copy");
    appendText(copy, "strong", "", "Today's plan");
    appendText(copy, "span", "", daily.path);
    button.appendChild(copy);
    button.addEventListener("click", () => loadKnowledge(daily.path));
    ui["artifact-grid"].appendChild(button);
  }
}

function gatherGaps(detail) {
  const gaps = [];
  const add = (items) => {
    if (Array.isArray(items)) items.forEach((item) => gaps.push(item));
  };
  add(detail.gaps);
  add(detail.planning && detail.planning.gaps);
  add(detail.runs && detail.runs.gaps);
  (detail.artifacts || []).forEach((item) => {
    if (item.availability !== "available") gaps.push(item);
  });
  const today = detail.daily_plans && detail.daily_plans.today;
  if (today && today.availability !== "available") gaps.push(today);
  return gaps;
}

function renderGaps(detail) {
  clear(ui["gaps-list"]);
  const gaps = gatherGaps(detail);
  if (!gaps.length) {
    ui["gaps-list"].appendChild(emptyState("No structural data gaps are currently reported."));
    return;
  }
  gaps.slice(0, 40).forEach((gap) => {
    const item = element("article", "gap-item");
    appendText(item, "strong", "", gap.path || gap.reason_code || "Data gap");
    appendText(item, "p", "small", gap.reason || "Data is not currently available.");
    ui["gaps-list"].appendChild(item);
  });
}

function renderCoverage(detail) {
  clear(ui["coverage-totals"]);
  clear(ui["coverage-panel"]);
  clear(ui["files-table"]);
  const coverage = detail.coverage;
  if (!coverage) {
    ui["coverage-panel"].appendChild(emptyState("Inventory coverage is unavailable. Run inventory before treating the project as scanned."));
  } else {
    const totals = coverage.totals || {};
    [
      `${number(totals.file_count)} files`,
      formatBytes(totals.byte_count),
      `${number(totals.failed_file_count)} failed`,
    ].forEach((text) => ui["coverage-totals"].appendChild(element("span", "metric-pill", text)));
    const axes = coverage.coverage || {};
    ["research_role", "processing_status", "read_depth", "reason"].forEach((axisName) => {
      const axis = element("article", "coverage-axis");
      appendText(axis, "strong", "", axisName.replaceAll("_", " "));
      const list = element("ul");
      Object.entries(axes[axisName] || {}).slice(0, 12).forEach(([name, metrics]) => {
        const row = element("li");
        appendText(row, "span", "", name);
        appendText(row, "strong", "", number(metrics.file_count));
        list.appendChild(row);
      });
      axis.appendChild(list);
      ui["coverage-panel"].appendChild(axis);
    });
  }
  const files = detail.files && Array.isArray(detail.files.preview) ? detail.files.preview : [];
  if (!files.length) {
    const row = element("tr");
    const cell = element("td", "", "No current Manifest file rows.");
    cell.colSpan = 5;
    row.appendChild(cell);
    ui["files-table"].appendChild(row);
    return;
  }
  files.forEach((file) => {
    const row = element("tr");
    [file.path, file.research_role, file.processing_status, file.read_depth, `${file.reason_code}: ${file.reason}`]
      .forEach((value) => row.appendChild(element("td", "", value || "—")));
    ui["files-table"].appendChild(row);
  });
}

function renderClaims(detail) {
  clear(ui["claims-list"]);
  const claims = detail.claims && Array.isArray(detail.claims.items) ? detail.claims.items : [];
  ui["claim-count"].textContent = String(number(detail.claims && detail.claims.count));
  if (!claims.length) {
    ui["claims-list"].appendChild(emptyState("No Claim detail pages are available."));
    return;
  }
  claims.forEach((claim) => {
    const item = element("article", "claim-item");
    const button = element("button", "claim-button");
    button.type = "button";
    const heading = element("div", "section-heading compact");
    const copy = element("div");
    appendText(copy, "strong", "", claim.title || claim.path);
    appendText(copy, "p", "small", claim.path);
    heading.appendChild(copy);
    heading.appendChild(statusChip(claim.status_code || claim.status));
    button.appendChild(heading);
    const refs = Array.isArray(claim.evidence_trace) ? claim.evidence_trace : [];
    appendText(button, "p", "small", `${refs.length} directional Evidence reference${refs.length === 1 ? "" : "s"}`);
    button.addEventListener("click", () => showClaim(claim));
    item.appendChild(button);
    ui["claims-list"].appendChild(item);
  });
}

function planningCard(title, body, detailText) {
  const card = element("article", "planning-card");
  appendText(card, "h3", "", title);
  appendText(card, "p", "", body || "Not available");
  if (detailText) appendText(card, "p", "small", detailText);
  return card;
}

function renderPlanning(detail) {
  clear(ui["planning-panel"]);
  const planning = detail.planning || {};
  const goal = planning.goal;
  ui["planning-panel"].appendChild(planningCard(
    "Goal",
    goal ? (goal.title || goal.objective || "Draft project goal") : "No Goal artifact",
    goal ? `${goal.display_status || goal.status || "DRAFT"} · draft=${Boolean(goal.is_draft)}` : "Explicit missing input",
  ));
  const tasks = Array.isArray(planning.tasks) ? planning.tasks : [];
  const taskCard = planningCard("Tasks", `${tasks.length} tracked task${tasks.length === 1 ? "" : "s"}`, "Task presence does not authorize execution.");
  if (tasks.length) {
    const list = element("ul");
    tasks.slice(0, 12).forEach((task) => appendText(list, "li", "small", `${task.status || "draft"} · ${task.title || task.task_id}`));
    taskCard.appendChild(list);
  }
  ui["planning-panel"].appendChild(taskCard);
  const today = detail.daily_plans && detail.daily_plans.today;
  const dailyCard = planningCard(
    "Daily plan",
    today ? (today.title || today.path) : "No daily plan",
    today ? `${today.status || "DRAFT"} · ${today.path}` : "Explicit missing input",
  );
  if (today) {
    dailyCard.addEventListener("click", () => loadKnowledge(today.path));
    dailyCard.tabIndex = 0;
  }
  ui["planning-panel"].appendChild(dailyCard);
}

function renderRuns(detail) {
  clear(ui["runs-list"]);
  const runs = detail.runs && Array.isArray(detail.runs.items) ? detail.runs.items : [];
  ui["run-count"].textContent = String(number(detail.runs && detail.runs.count));
  if (!runs.length) {
    ui["runs-list"].appendChild(emptyState("No persisted run history."));
    return;
  }
  runs.slice(0, 30).forEach((run) => {
    const item = element("article", "run-item");
    const button = element("button", "run-button");
    button.type = "button";
    appendText(button, "strong", "", run.run_id || "run");
    appendText(button, "p", "small", `${run.status || "unknown"} · ${run.completed_at || run.updated_at || run.created_at || "time unavailable"}`);
    const grid = element("div", "run-grid");
    grid.appendChild(element("div", "run-metric", `${number(run.duration_ms)} ms`));
    grid.appendChild(element("div", "run-metric", `${number(run.stages && run.stages.length)} stages`));
    grid.appendChild(element("div", "run-metric", `${number(run.errors && run.errors.length)} errors`));
    button.appendChild(grid);
    button.addEventListener("click", () => showRun(run));
    item.appendChild(button);
    ui["runs-list"].appendChild(item);
  });
}

function renderDetail(detail) {
  state.detail = detail;
  const project = detail.project || {};
  ui["project-id"].textContent = project.project_id || state.selectedProjectId || "Project";
  ui["project-title"].textContent = project.name || project.project_id || "Research project";
  ui["project-meta"].textContent = project.registered_at ? `Registered ${project.registered_at}` : "Registered project; inventory is not implied.";
  const gaps = gatherGaps(detail);
  ui["project-health"].textContent = gaps.length ? "DRAFT" : "VERIFIED";
  ui["project-health"].className = `status-chip status-${gaps.length ? "draft" : "verified"}`;
  renderSummary(detail);
  renderArtifacts(detail);
  renderGaps(detail);
  renderCoverage(detail);
  renderClaims(detail);
  renderPlanning(detail);
  renderRuns(detail);
  renderProjects();
}

async function loadKnowledge(path) {
  if (!state.selectedProjectId) return;
  ui["viewer-title"].textContent = path;
  ui["markdown-viewer"].textContent = "Loading validated Markdown…";
  clear(ui["evidence-viewer"]);
  try {
    const payload = await getJson(`/api/projects/${encodeURIComponent(state.selectedProjectId)}/knowledge?path=${encodeURIComponent(path)}`);
    ui["viewer-title"].textContent = payload.title || payload.path;
    if (payload.availability !== "available") {
      ui["markdown-viewer"].textContent = `${payload.status || "DRAFT"}\n\n${payload.reason || "Knowledge page is unavailable."}`;
      return;
    }
    const frontmatter = JSON.stringify(payload.frontmatter || {}, null, 2);
    ui["markdown-viewer"].textContent = `--- validated frontmatter ---\n${frontmatter}\n--- body ---\n${payload.body || ""}`;
  } catch (error) {
    ui["markdown-viewer"].textContent = `Unable to load page: ${error.message}`;
  }
}

function showClaim(claim) {
  ui["viewer-title"].textContent = claim.title || claim.path;
  clear(ui["evidence-viewer"]);
  const trace = Array.isArray(claim.evidence_trace) ? claim.evidence_trace : [];
  if (!trace.length) {
    ui["evidence-viewer"].appendChild(emptyState(claim.legacy_evidence_ids && claim.legacy_evidence_ids.length
      ? "Legacy Schema v1 Evidence IDs are shown without inferred stance."
      : "No directional Evidence references declared."));
  }
  trace.forEach((entry) => {
    const sourcePath = entry.recorded_path || (entry.source && entry.source.current_path) || "source unavailable";
    const locator = JSON.stringify(entry.locator || {});
    const text = `${entry.stance || "stance unavailable"} · ${entry.evidence_id}\n${sourcePath}\nLocator ${locator}\n${entry.currentness_note || ""}`;
    ui["evidence-viewer"].appendChild(element("div", "locator-card", text));
  });
  loadKnowledge(claim.path);
}

function showRun(run) {
  ui["viewer-title"].textContent = run.run_id || "Run record";
  clear(ui["evidence-viewer"]);
  const errors = Array.isArray(run.errors) ? run.errors : [];
  errors.forEach((error) => ui["evidence-viewer"].appendChild(element("div", "locator-card", JSON.stringify(error, null, 2))));
  ui["markdown-viewer"].textContent = JSON.stringify({
    status: run.status,
    duration_ms: run.duration_ms,
    usage: run.usage,
    stages: run.stages,
    artifacts: run.artifacts,
  }, null, 2);
}

async function selectProject(projectId) {
  state.selectedProjectId = projectId;
  renderProjects();
  setBanner("Loading validated local project state…");
  try {
    const detail = await getJson(`/api/projects/${encodeURIComponent(projectId)}`);
    renderDetail(detail);
    setBanner("Loopback-only view. Source files were not reopened; Evidence currentness is registry-level unless separately verified.");
  } catch (error) {
    state.detail = null;
    setBanner(`Project is unavailable: ${error.message}`, true);
  }
}

async function loadSnapshot() {
  setBanner("Loading registered projects…");
  try {
    const snapshot = await getJson("/api/snapshot");
    state.projects = Array.isArray(snapshot.projects) ? snapshot.projects : [];
    renderProjects();
    if (state.selectedProjectId && state.projects.some((item) => item.project_id === state.selectedProjectId)) {
      await selectProject(state.selectedProjectId);
    } else if (state.projects.length) {
      await selectProject(state.projects[0].project_id);
    } else {
      setBanner("No registered projects. Registration creates storage only; it is not a completed scan.");
    }
  } catch (error) {
    setBanner(`Cockpit data unavailable: ${error.message}`, true);
  }
}

ui["project-search"].addEventListener("input", (event) => {
  state.filter = event.target.value || "";
  renderProjects();
});
ui["refresh-button"].addEventListener("click", loadSnapshot);
ui["close-viewer"].addEventListener("click", () => {
  ui["viewer-title"].textContent = "Markdown and Evidence locator";
  clear(ui["evidence-viewer"]);
  ui["markdown-viewer"].textContent = "Select a knowledge artifact or Claim trace.";
});

loadSnapshot();
