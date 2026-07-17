"use strict";

const statusMeta = {
  completed: { label: "\u5df2\u5b8c\u6210", icon: "\u2713", tone: "#b9ff45" },
  partial: { label: "\u90e8\u5206\u5b8c\u6210", icon: "\u21bb", tone: "#65e7da" },
  awaiting_review: { label: "\u7b49\u5f85\u9a8c\u6536", icon: "\u25d0", tone: "#ffae5d" },
  in_progress: { label: "\u6267\u884c\u4e2d", icon: "\u25cf", tone: "#ffd95d" },
  authorized: { label: "\u5df2\u6388\u6743", icon: "\u25c6", tone: "#7da9ff" },
  needs_changes: { label: "\u9700\u8981\u4fee\u6539", icon: "!", tone: "#ff6b68" },
  blocked: { label: "\u963b\u585e", icon: "\u00d7", tone: "#ff6b68" },
  not_started: { label: "\u672a\u5f00\u59cb", icon: "\u25a1", tone: "#7d8781" }
};

let snapshot = null;
let selectedTaskId = null;
let selectedUnitId = null;
let milestoneFilter = "all";
const byId = (id) => document.getElementById(id);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function append(parent, ...children) {
  children.filter(Boolean).forEach((child) => parent.appendChild(child));
  return parent;
}

function statusClass(status) {
  return `status-${Object.hasOwn(statusMeta, status) ? status : "not_started"}`;
}

function setTone(element, status) {
  element.style.setProperty("--tone", statusMeta[status]?.tone || "#7d8781");
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function shortTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false
      }).format(date);
}

function toast(message) {
  const target = byId("toast");
  target.textContent = message;
  target.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => target.classList.remove("show"), 1800);
}

function statusChip(status, label) {
  return node(
    "span",
    `status-chip ${statusClass(status)}`,
    label || statusMeta[status]?.label || status
  );
}

function renderSummary() {
  const root = byId("summary-grid");
  root.replaceChildren();
  const repo = snapshot.repository;
  const summary = snapshot.summary;
  const counts = summary.counts;
  const cards = [
    {
      label: "CURRENT BRANCH",
      value: repo.branch,
      note: repo.clean
        ? "\u5de5\u4f5c\u533a\u5e72\u51c0"
        : `${repo.status_entries.length + repo.omitted_status_count} \u9879\u5f85\u63d0\u4ea4\u6539\u52a8`,
      accent: "#65e7da"
    },
    {
      label: "ACCEPTED MILESTONES",
      value: `${summary.accepted_milestone_count} / ${summary.milestone_count}`,
      note: "R0 \u5230 R6 \u7684\u9a8c\u6536\u8f68\u9053",
      accent: "#b9ff45"
    },
    {
      label: "TASK / UNIT PROGRESS",
      value: `${counts.completed} \u2713`,
      note: `${summary.unit_count || 0} \u4e2a\u5355\u5143 \u00b7 ${counts.partial} \u90e8\u5206\u5b8c\u6210`,
      accent: "#ffae5d"
    },
    {
      label: "CHECKPOINTS / ARTIFACTS",
      value: summary.checkpoint_count,
      note: `${summary.artifact_count || 0} \u4efd\u5141\u8bb8\u9884\u89c8 \u00b7 HEAD ${repo.head.short_hash}`,
      accent: "#7da9ff"
    }
  ];
  cards.forEach((item) => {
    const card = node("article", "summary-card");
    card.style.setProperty("--accent", item.accent);
    append(
      card,
      node("span", "label", item.label),
      node("strong", "", item.value),
      node("small", "", item.note)
    );
    root.appendChild(card);
  });
}

function renderActive() {
  const active = snapshot.active_unit;
  const statusTarget = byId("active-status");
  const content = byId("active-content");
  content.classList.remove("skeleton-block");
  content.replaceChildren();
  const status = active?.status || "not_started";
  statusTarget.className = `status-chip ${statusClass(status)}`;
  statusTarget.textContent = active ? active.status_label : "\u7b49\u5f85\u4f60\u6388\u6743";
  if (!active) {
    append(
      content,
      activeCell(
        "NEXT ACTION",
        "\u5f53\u524d\u6ca1\u6709\u6d3b\u8dc3\u4efb\u52a1",
        "\u53ea\u6709\u5728\u7528\u6237\u786e\u8ba4\u5173\u952e\u95ee\u9898\u540e\uff0c\u624d\u4f1a\u521b\u5efa\u4efb\u52a1\u5206\u652f\u5e76\u5f00\u59cb\u5b9e\u65bd\u3002"
      )
    );
    return;
  }
  const changes = snapshot.repository.current_changes;
  const progress = active.progress;
  append(
    content,
    activeCell(
      "AUTHORIZED UNIT",
      `${active.unit_id} / ${active.title}`,
      progress?.summary || "\u7528\u6237\u5df2\u6279\u51c6\u672c\u5355\u5143\u8303\u56f4\uff0c\u4e0d\u4f1a\u81ea\u52a8\u8fdb\u5165\u4e0b\u4e00\u4efb\u52a1\u3002"
    ),
    activeCell(
      "BRANCH DIFF",
      `${changes.file_count} \u4e2a\u6587\u4ef6`,
      `+${changes.additions} / -${changes.deletions} \u00b7 ${snapshot.repository.branch}`,
      true
    ),
    activeCell(
      "GIT POSITION",
      `${snapshot.repository.ahead} ahead / ${snapshot.repository.behind} behind`,
      `HEAD ${snapshot.repository.head.short_hash}`,
      true
    )
  );
}

function activeCell(label, title, description, mono = false) {
  const cell = node("div", "active-cell");
  append(
    cell,
    node("label", "", label),
    node("strong", mono ? "mono" : "", title),
    node("p", mono ? "mono" : "", description)
  );
  return cell;
}

function renderMilestones() {
  const root = byId("milestone-rail");
  root.replaceChildren();
  snapshot.milestones.forEach((item) => {
    const button = node(
      "button",
      `milestone ${milestoneFilter === item.id ? "active" : ""}`
    );
    button.type = "button";
    button.dataset.id = item.id;
    setTone(button, item.status);
    const title = item.title.replace(/[\u2014-]+\s*(\u5df2\u5b8c\u6210|\u672c\u6b21\u5b8c\u6210)$/u, "");
    const track = node("div", "progress-track");
    const fill = node("i");
    fill.style.setProperty("--progress", `${item.progress_percent}%`);
    track.appendChild(fill);
    const foot = node("footer");
    append(
      foot,
      node("span", "", `${item.progress_percent}%`),
      node("span", "", `${item.completed_count}/${item.task_count}`)
    );
    append(button, node("span", "number", item.id), node("h3", "", title), track, foot);
    button.addEventListener("click", () => {
      milestoneFilter = milestoneFilter === item.id ? "all" : item.id;
      renderMilestones();
      renderTasks();
    });
    root.appendChild(button);
  });
}

function populateFilters() {
  const status = byId("status-filter");
  const currentStatus = status.value;
  status.replaceChildren(new Option("\u5168\u90e8\u72b6\u6001", "all"));
  Object.entries(statusMeta).forEach(([key, value]) =>
    status.add(new Option(`${value.icon} ${value.label}`, key))
  );
  status.value = currentStatus || "all";
  const area = byId("area-filter");
  const currentArea = area.value;
  area.replaceChildren(new Option("\u5168\u90e8\u9886\u57df", "all"));
  [...new Set(snapshot.tasks.map((task) => task.area))]
    .sort()
    .forEach((value) => area.add(new Option(value, value)));
  area.value = currentArea || "all";
}

function filteredTasks() {
  const query = byId("task-search").value.trim().toLocaleLowerCase("zh-CN");
  const status = byId("status-filter").value;
  const area = byId("area-filter").value;
  return snapshot.tasks.filter((task) => {
    const unitEvidence = (task.units || []).flatMap((unit) => [
      unit.unit_id,
      unit.title,
      unit.summary,
      ...unit.records.map((record) => record.summary),
      ...unit.checks.map((check) => check.summary),
      ...unit.artifacts.map((artifact) => `${artifact.label} ${artifact.path}`)
    ]);
    const haystack = [
      task.id,
      task.title,
      task.description,
      task.area,
      ...task.commits.map((commit) => commit.subject),
      ...unitEvidence
    ]
      .join(" ")
      .toLocaleLowerCase("zh-CN");
    return (
      (!query || haystack.includes(query)) &&
      (status === "all" || task.status === status) &&
      (area === "all" || task.area === area) &&
      (milestoneFilter === "all" || task.milestones.includes(milestoneFilter))
    );
  });
}

function renderTasks() {
  const tasks = filteredTasks();
  const root = byId("task-list");
  root.replaceChildren();
  byId("visible-count").textContent = `${tasks.length} / ${snapshot.tasks.length}`;
  if (!tasks.length) {
    const empty = node("div", "empty-state");
    append(
      empty,
      node("div", "empty-glyph", "\u2205"),
      node("p", "", "\u6ca1\u6709\u5339\u914d\u5f53\u524d\u7b5b\u9009\u6761\u4ef6\u7684\u4efb\u52a1\u3002")
    );
    root.appendChild(empty);
    return;
  }
  tasks.forEach((task) => {
    const button = node(
      "button",
      `task-row ${selectedTaskId === task.id ? "selected" : ""}`
    );
    button.type = "button";
    button.dataset.id = task.id;
    setTone(button, task.status);
    const main = node("span", "task-main");
    append(
      main,
      node("strong", "", task.title),
      node(
        "small",
        "",
        `${task.area} \u00b7 ${task.priority || "--"}/${task.size || "--"} \u00b7 ${(task.units || []).length} units \u00b7 ${task.milestones.join(" / ") || "--"}`
      )
    );
    const state = node("span", "task-state");
    append(
      state,
      node("i"),
      node("span", "", `${statusMeta[task.status]?.icon || ""} ${task.status_label}`)
    );
    append(button, node("span", "task-id", task.id), main, state);
    button.addEventListener("click", () => {
      selectedTaskId = task.id;
      if (!(task.units || []).some((unit) => unit.unit_id === selectedUnitId)) {
        selectedUnitId = defaultUnit(task)?.unit_id || null;
      }
      renderTasks();
      renderTaskDetail(task);
    });
    root.appendChild(button);
  });
}

function defaultUnit(task) {
  const units = task.units || [];
  return (
    units.find((unit) => unit.unit_id === selectedUnitId) ||
    units.find((unit) => unit.unit_id === snapshot.active_unit?.unit_id) ||
    units[units.length - 1] ||
    null
  );
}

function openUnit(taskId, unitId) {
  const task = snapshot.tasks.find((item) => item.id === taskId);
  if (!task || !(task.units || []).some((unit) => unit.unit_id === unitId)) {
    toast("\u8be5\u5355\u5143\u5c1a\u65e0\u53ef\u7528\u8be6\u60c5");
    return;
  }
  selectedTaskId = taskId;
  selectedUnitId = unitId;
  milestoneFilter = "all";
  byId("task-search").value = "";
  byId("status-filter").value = "all";
  byId("area-filter").value = "all";
  renderMilestones();
  renderTasks();
  renderTaskDetail(task);
  const detail = byId("task-detail");
  detail.tabIndex = -1;
  detail.focus({ preventScroll: true });
  detail.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderTaskDetail(task) {
  const root = byId("task-detail");
  root.className = "task-detail";
  root.replaceChildren();
  root.style.setProperty("--tone", statusMeta[task.status]?.tone || "#7d8781");
  const header = node("div", "detail-header");
  const titleWrap = node("div");
  append(
    titleWrap,
    node("span", "detail-id", `${task.id} \u00b7 ${task.priority || "--"}/${task.size || "--"}`),
    node("h3", "", task.title)
  );
  append(header, titleWrap, statusChip(task.status, task.status_label));
  root.appendChild(header);

  const units = task.units || [];
  if (units.length) {
    const selected = defaultUnit(task);
    selectedUnitId = selected?.unit_id || null;
    root.appendChild(unitSelectorBlock(task, units));
    if (selected) root.appendChild(unitDetailBlock(selected));
  }
  if (task.description) root.appendChild(detailBlock("SCOPE / \u4efb\u52a1\u8303\u56f4", task.description));
  if (task.acceptance) root.appendChild(detailBlock("DEFINITION OF DONE / \u9a8c\u6536\u6807\u51c6", task.acceptance));
  if (task.delta) root.appendChild(deltaBlock(task.delta));
  root.appendChild(changeBlock(task));
  if (task.commits.length) root.appendChild(commitBlock(task.commits, "TASK COMMITS / \u4efb\u52a1\u63d0\u4ea4\u8bc1\u636e"));
  if (task.checkpoints.length) root.appendChild(checkpointBlock(task.checkpoints, "TASK CHECKPOINTS / \u4efb\u52a1\u68c0\u67e5\u70b9"));
}

function unitSelectorBlock(task, units) {
  const block = node("section", "detail-block unit-selector-block");
  block.appendChild(node("h4", "", "UNITS / \u6267\u884c\u5355\u5143"));
  const grid = node("div", "unit-button-grid");
  units.forEach((unit) => {
    const button = node(
      "button",
      `unit-button ${unit.unit_id === selectedUnitId ? "selected" : ""}`
    );
    button.type = "button";
    button.dataset.unitId = unit.unit_id;
    setTone(button, unit.status);
    append(
      button,
      node("strong", "", unit.unit_id),
      node("span", "", unit.title),
      node("small", "", `${statusMeta[unit.status]?.icon || ""} ${unit.status_label}`)
    );
    button.addEventListener("click", () => {
      selectedUnitId = unit.unit_id;
      renderTaskDetail(task);
    });
    grid.appendChild(button);
  });
  block.appendChild(grid);
  return block;
}

function unitDetailBlock(unit) {
  const block = node("section", "unit-detail");
  setTone(block, unit.status);
  const header = node("div", "unit-detail-header");
  const title = node("div");
  append(title, node("span", "detail-id", unit.unit_id), node("h4", "", unit.title));
  append(header, title, statusChip(unit.status, unit.status_label));
  block.appendChild(header);
  if (unit.summary) block.appendChild(node("p", "unit-summary", unit.summary));

  const metrics = node("div", "unit-metrics");
  [
    [unit.records.length, "RECORDS"],
    [unit.checks.length, "CHECKS"],
    [unit.commits.length, "COMMITS"],
    [unit.artifacts.length, "ARTIFACTS"]
  ].forEach(([value, label]) => {
    const metric = node("div");
    append(metric, node("strong", "", formatNumber(value)), node("span", "", label));
    metrics.appendChild(metric);
  });
  block.appendChild(metrics);

  if (unit.records.length) block.appendChild(unitRecordsBlock(unit.records));
  if (unit.commits.length) block.appendChild(commitBlock(unit.commits, "EXPLICIT COMMITS / \u663e\u5f0f\u63d0\u4ea4"));
  if (unit.checkpoints.length) block.appendChild(checkpointBlock(unit.checkpoints, "UNIT CHECKPOINTS / \u5355\u5143\u68c0\u67e5\u70b9"));
  if (unit.artifacts.length) block.appendChild(artifactBlock(unit));
  if (!unit.records.length && !unit.commits.length && !unit.checkpoints.length && !unit.artifacts.length) {
    block.appendChild(node("p", "unit-empty", "\u8be5\u5355\u5143\u5df2\u767b\u8bb0\uff0c\u4f46\u5c1a\u65e0\u53ef\u5c55\u793a\u7684\u9a8c\u8bc1\u8bc1\u636e\u3002"));
  }
  return block;
}

function unitRecordsBlock(records) {
  const section = node("section", "unit-evidence-section");
  section.appendChild(node("h5", "", "PROGRESS RECORDS / \u8fdb\u5ea6\u8bb0\u5f55"));
  [...records].reverse().forEach((record) => {
    const mapped = record.status === "passed"
      ? "completed"
      : record.status === "failed"
        ? "needs_changes"
        : record.status;
    const item = node("article", `unit-record ${statusClass(mapped)}`);
    setTone(item, mapped);
    const header = node("header");
    append(
      header,
      node("strong", "", record.status),
      node("time", "", `${shortTime(record.recorded_at)} \u00b7 ${record.commit || "--"}`)
    );
    append(item, header, node("p", "", record.summary || "--"));
    if (record.checks.length) {
      const checks = node("ul", "check-list");
      record.checks.forEach((check) => {
        const checkItem = node("li", `check-${check.status}`);
        append(
          checkItem,
          node("strong", "", `${check.id} \u00b7 ${check.status}`),
          node("span", "", check.summary || "--")
        );
        checks.appendChild(checkItem);
      });
      item.appendChild(checks);
    }
    section.appendChild(item);
  });
  return section;
}

function artifactBlock(unit) {
  const section = node("section", "unit-evidence-section");
  section.appendChild(node("h5", "", "ALLOWLISTED ARTIFACTS / \u5141\u8bb8\u9884\u89c8"));
  const list = node("div", "artifact-list");
  unit.artifacts.forEach((artifact) => {
    const button = node("button", "artifact-button");
    button.type = "button";
    append(
      button,
      node("strong", "", artifact.label),
      node("span", "", artifact.description || artifact.path),
      node("code", "", `${artifact.commit} \u00b7 ${artifact.path}`)
    );
    button.addEventListener("click", () => openArtifact(unit, artifact));
    list.appendChild(button);
  });
  section.appendChild(list);
  return section;
}

function detailBlock(title, text) {
  const block = node("section", "detail-block");
  append(block, node("h4", "", title), node("p", "", text));
  return block;
}

function deltaBlock(delta) {
  const block = node("section", "detail-block");
  block.appendChild(node("h4", "", "BEFORE \u2192 AFTER / \u80fd\u529b\u5dee\u5f02"));
  const parts = delta.split("\u2192");
  const box = node("div", "delta-box");
  append(
    box,
    node("div", "delta-side", parts[0]?.trim() || "\u5b9e\u65bd\u524d"),
    node("div", "delta-arrow", "\u2192"),
    node("div", "delta-side after", parts.slice(1).join("\u2192").trim() || "\u5b9e\u65bd\u540e")
  );
  block.appendChild(box);
  return block;
}

function changeBlock(task) {
  const change = task.id === snapshot.active_unit?.task_id
    ? snapshot.repository.current_changes
    : task.changes;
  const block = node("section", "detail-block");
  block.appendChild(node("h4", "", "GIT CHANGESET / \u4ee3\u7801\u5dee\u5f02"));
  const metrics = node("div", "metric-strip");
  [
    [change.file_count, "FILES", ""],
    [`+${change.additions}`, "ADDED", "positive"],
    [`-${change.deletions}`, "DELETED", "negative"]
  ].forEach(([value, label, className]) => {
    const metric = node("div", "mini-metric");
    append(metric, node("strong", className, value), node("span", "", label));
    metrics.appendChild(metric);
  });
  block.appendChild(metrics);
  if (change.files.length) {
    const list = node("ul", "file-list");
    change.files.forEach((file) => {
      const item = node("li");
      const numbers = node("span", "diff-numbers");
      append(
        numbers,
        node("b", "", file.binary ? "BIN" : `+${file.additions}`),
        node("em", "", file.binary ? "" : `-${file.deletions}`)
      );
      append(item, node("code", "", file.path), numbers);
      list.appendChild(item);
    });
    block.appendChild(list);
  } else {
    block.appendChild(node("p", "", "\u5c1a\u65e0\u53ef\u5c55\u793a\u7684 Git \u6539\u52a8\u3002"));
  }
  return block;
}

function commitBlock(commits, title = "COMMITS / \u63d0\u4ea4\u8bc1\u636e") {
  const block = node("section", "detail-block");
  block.appendChild(node("h4", "", title));
  const list = node("ul", "commit-list");
  commits.forEach((commit) => {
    const item = node("li");
    append(
      item,
      node("span", "", commit.subject || "\u5df2\u663e\u5f0f\u767b\u8bb0\u7684\u63d0\u4ea4"),
      node("code", "", commit.short_hash || commit.commit)
    );
    list.appendChild(item);
  });
  block.appendChild(list);
  return block;
}

function checkpointBlock(checkpoints, title = "CHECKPOINTS / \u68c0\u67e5\u70b9") {
  const block = node("section", "detail-block");
  block.appendChild(node("h4", "", title));
  const tags = node("div", "tag-list");
  checkpoints.forEach((checkpoint) => tags.appendChild(node("span", "tag", checkpoint.name)));
  block.appendChild(tags);
  return block;
}

function renderValidation() {
  const root = byId("validation-list");
  root.replaceChildren();
  const records = [...snapshot.progress_records].reverse();
  if (!records.length) {
    root.appendChild(node("div", "empty-state", "\u5c1a\u65e0\u672c\u5730\u9a8c\u8bc1\u8bb0\u5f55\u3002"));
    return;
  }
  records.slice(0, 12).forEach((record) => {
    const mapped = record.status === "passed"
      ? "completed"
      : record.status === "failed"
        ? "needs_changes"
        : record.status;
    const item = node("button", `timeline-item ${statusClass(mapped)}`);
    item.type = "button";
    item.dataset.unitId = record.unit_id;
    setTone(item, mapped);
    append(
      item,
      node("strong", "", `${record.unit_id} \u00b7 ${record.status}`),
      node("p", "", record.summary || "--"),
      node("time", "", `${shortTime(record.recorded_at)} \u00b7 ${record.commit || "--"}`)
    );
    item.addEventListener("click", () => openUnit(record.task_id, record.unit_id));
    root.appendChild(item);
  });
}

function renderCheckpoints() {
  const root = byId("checkpoint-list");
  root.replaceChildren();
  snapshot.checkpoints.slice(0, 12).forEach((checkpoint) => {
    const tagName = checkpoint.unit_id ? "button" : "article";
    const item = node(tagName, `checkpoint-item ${checkpoint.unit_id ? "clickable" : ""}`);
    if (checkpoint.unit_id) {
      item.type = "button";
      item.addEventListener("click", () => openUnit(checkpoint.task_id, checkpoint.unit_id));
    }
    append(
      item,
      node("code", "", checkpoint.name),
      node("p", "", checkpoint.subject || "--"),
      node("time", "", `${shortTime(checkpoint.committed_at)} \u00b7 ${checkpoint.commit}`)
    );
    root.appendChild(item);
  });
}

function ensureArtifactViewer() {
  let dialog = byId("artifact-viewer");
  if (dialog) return dialog;
  dialog = node("dialog", "artifact-viewer");
  dialog.id = "artifact-viewer";
  const shell = node("section", "artifact-viewer-shell");
  const header = node("header");
  const heading = node("div");
  append(
    heading,
    node("span", "section-kicker", "READ-ONLY GIT BLOB"),
    node("h2", "artifact-viewer-title", "--")
  );
  const close = node("button", "artifact-close", "\u00d7");
  close.type = "button";
  close.setAttribute("aria-label", "Close artifact preview");
  close.addEventListener("click", () => dialog.close());
  append(header, heading, close);
  const meta = node("p", "artifact-viewer-meta", "--");
  const content = node("pre", "artifact-viewer-content", "--");
  append(shell, header, meta, content);
  dialog.appendChild(shell);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  document.body.appendChild(dialog);
  return dialog;
}

async function openArtifact(unit, artifact) {
  const route = artifact.route || artifact.url;
  if (!/^\/api\/artifacts\/[0-9a-f]{64}$/.test(route || "")) {
    toast("\u9884\u89c8\u8def\u7531\u672a\u901a\u8fc7\u5141\u8bb8\u5217\u8868\u9a8c\u8bc1");
    return;
  }
  const dialog = ensureArtifactViewer();
  dialog.querySelector(".artifact-viewer-title").textContent = `${unit.unit_id} / ${artifact.label}`;
  dialog.querySelector(".artifact-viewer-meta").textContent = `${artifact.commit} \u00b7 ${artifact.path} \u00b7 max ${formatNumber(artifact.byte_limit)} bytes`;
  const content = dialog.querySelector(".artifact-viewer-content");
  content.textContent = "\u6b63\u5728\u4ece\u663e\u5f0f Git \u63d0\u4ea4\u52a0\u8f7d\u2026";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  try {
    const response = await fetch(route, {
      cache: "no-store",
      headers: { Accept: "text/plain" }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    content.textContent = await response.text();
  } catch (error) {
    content.textContent = "\u9884\u89c8\u5931\u8d25\uff1a\u6587\u4ef6\u53ef\u80fd\u8d85\u9650\u3001\u975e UTF-8\u3001\u672a\u5728\u663e\u5f0f\u63d0\u4ea4\u4e2d\uff0c\u6216\u5df2\u4ece\u5141\u8bb8\u5217\u8868\u79fb\u9664\u3002";
    console.error(error);
  }
}

function renderAll() {
  renderSummary();
  renderActive();
  renderMilestones();
  populateFilters();
  renderTasks();
  renderValidation();
  renderCheckpoints();
  byId("updated-at").textContent = shortTime(snapshot.generated_at);
  if (selectedTaskId) {
    const task = snapshot.tasks.find((item) => item.id === selectedTaskId);
    if (task) renderTaskDetail(task);
  }
}

async function loadSnapshot(showToast = false) {
  byId("live-label").textContent = "SYNC";
  try {
    const response = await fetch("/api/status", {
      cache: "no-store",
      headers: { Accept: "application/json" }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    snapshot = await response.json();
    renderAll();
    byId("live-label").textContent = "LIVE";
    if (showToast) toast("\u5df2\u5237\u65b0\u6700\u65b0 Git\u3001\u5355\u5143\u4e0e\u8def\u7ebf\u56fe\u72b6\u6001");
  } catch (error) {
    byId("live-label").textContent = "OFFLINE";
    toast("\u72b6\u6001\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u672c\u5730\u670d\u52a1");
    console.error(error);
  }
}

byId("refresh-button").addEventListener("click", () => loadSnapshot(true));
["task-search", "status-filter", "area-filter"].forEach((id) =>
  byId(id).addEventListener(id === "task-search" ? "input" : "change", () => snapshot && renderTasks())
);
loadSnapshot();
window.setInterval(() => loadSnapshot(false), 10000);
