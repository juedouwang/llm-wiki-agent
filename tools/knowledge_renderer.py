"""Deterministic rendering of the project Knowledge Schema v2.

E-07 is deliberately a narrow bridge between current E-02--E-06 analysis and
I-01--I-04 planning artifacts and the curated project Markdown tree.  It does not
read a registered source project, call an LLM, infer scientific truth, or bypass
the controlled Markdown writer.  Missing or ungrounded inputs become explicit draft
placeholders instead of silently successful empty pages.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools.controlled_markdown import (
    ControlledMarkdownError,
    ControlledMarkdownUpdatePlan,
    parse_mixed_markdown_body,
    plan_controlled_markdown_update,
)
from tools.controlled_markdown_persistence import (
    ControlledMarkdownPersistenceError,
    ControlledMarkdownWriteAuthorization,
    TrustedHostSessionContext,
    bind_controlled_markdown_authorization,
    persist_controlled_markdown_update,
)
from tools.knowledge_artifacts import (
    KNOWLEDGE_PAGE_KIND,
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeArtifactError,
    KnowledgePage,
    artifact_contract_for_path,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
)
from tools.project_layout import LayoutError, validate_project_id
from tools.project_registry import load_registered_project


RENDERER_SCHEMA_VERSION = 1
RENDERER_KIND = "llmwiki-knowledge-rendering"
RENDERER_VERSION = "knowledge-rendering-v1"

# These are the fifteen product deliverables.  ``index.md`` is a navigation
# page in addition to the fifteen deliverables, not a sixteenth product type.
PRODUCT_ARTIFACT_SPECS: tuple[dict[str, str], ...] = (
    {"key": "overview", "artifact_type": "overview", "path": "overview.md", "title": "Project overview"},
    {"key": "project_map", "artifact_type": "project_map", "path": "project-map.md", "title": "Project map"},
    {"key": "reproduction", "artifact_type": "reproduction", "path": "reproduction.md", "title": "Reproduction"},
    {"key": "architecture", "artifact_type": "architecture", "path": "architecture.md", "title": "Architecture and data flow"},
    {"key": "paper", "artifact_type": "paper", "path": "papers/index.md", "title": "Papers"},
    {"key": "method", "artifact_type": "method", "path": "methods/index.md", "title": "Methods and innovations"},
    {"key": "dataset", "artifact_type": "dataset", "path": "datasets/index.md", "title": "Datasets"},
    {"key": "experiment", "artifact_type": "experiment", "path": "experiments/index.md", "title": "Experiments"},
    {"key": "result", "artifact_type": "result", "path": "results/index.md", "title": "Results and metrics"},
    {"key": "claim", "artifact_type": "claim", "path": "claims/index.md", "title": "Confirmed claims"},
    {"key": "open_question", "artifact_type": "open_question", "path": "open-questions.md", "title": "Open questions"},
    {"key": "project_status", "artifact_type": "project_status", "path": "status.md", "title": "Project status"},
    {"key": "risk", "artifact_type": "risk", "path": "risks.md", "title": "Risks and missing information"},
    {"key": "goal", "artifact_type": "goal", "path": "goals.md", "title": "Initial goals and task suggestions"},
    {"key": "plan", "artifact_type": "plan", "path": "plans/backlog.md", "title": "Backlog"},
)
DAILY_PLAN_PATH_PREFIX = "plans/daily/"

# The user-region IDs are part of the generated skeleton.  They are intentionally
# stable so F-05A can preserve exact user bytes on later regeneration.
MIXED_REGION_IDS: dict[str, tuple[str, ...]] = {
    "claims/index.md": ("user-confirmed-claims",),
    "open-questions.md": ("user-questions",),
    "status.md": ("user-status",),
    "goals.md": ("user-goals",),
    "plans/backlog.md": ("user-backlog",),
}

_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SOURCE_ID_RE = re.compile(r"^src-[0-9a-f]{32}$")
_EVIDENCE_ID_RE = re.compile(r"^evd-[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_LONG_RE = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)
_WINDOWS_ABS_RE = re.compile(r"\b[A-Za-z]:\\[^\n\r`]*")
_UNIX_ABS_RE = re.compile(r"(?<![\w])/(?:[^\s`]|\\ )+(?:/[^\s`]*)*")


@dataclass(frozen=True)
class KnowledgePageInput:
    """One complete proposed Schema v2 page, still free of filesystem I/O."""

    path: str
    artifact_type: str
    title: str
    frontmatter: Mapping[str, Any]
    body: str
    reason_code: str = "rendered"
    completeness: str = "available"
    intent: str = "regenerate"

    @property
    def payload(self) -> bytes:
        frontmatter = serialize_knowledge_frontmatter(self.frontmatter, path=self.path)
        return (frontmatter + self.body).encode("utf-8")

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "reason_code": self.reason_code,
            "completeness": self.completeness,
            "intent": self.intent,
        }


@dataclass(frozen=True)
class KnowledgePageObservation:
    """Bounded result for one canonical page; never contains page body bytes."""

    path: str
    artifact_type: str
    title: str
    action: str
    status: str
    ownership: str
    reason_code: str
    completeness: str
    current_sha256: str | None = None
    output_sha256: str | None = None
    written: bool = False
    error_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "action": self.action,
            "status": self.status,
            "ownership": self.ownership,
            "reason_code": self.reason_code,
            "completeness": self.completeness,
            "current_sha256": self.current_sha256,
            "output_sha256": self.output_sha256,
            "written": self.written,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class KnowledgeRenderingResult:
    """Deterministic renderer report with no raw Markdown or absolute paths."""

    project_id: str
    rendered_at: str
    status: str
    pages: tuple[KnowledgePageObservation, ...]
    index_path: str = "index.md"
    knowledge_root: str | None = None
    renderer_version: str = RENDERER_VERSION

    @property
    def observations(self) -> tuple[KnowledgePageObservation, ...]:
        return self.pages

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.pages if item.written)

    @property
    def protected_paths(self) -> tuple[str, ...]:
        return tuple(
            item.path
            for item in self.pages
            if item.action in {"protected", "rejected", "blocked"}
        )

    @property
    def failures(self) -> tuple[KnowledgePageObservation, ...]:
        return tuple(item for item in self.pages if item.action in {"failed", "blocked"})

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RENDERER_SCHEMA_VERSION,
            "kind": RENDERER_KIND,
            "renderer_version": self.renderer_version,
            "project_id": self.project_id,
            "rendered_at": self.rendered_at,
            "status": self.status,
            "index_path": self.index_path,
            "pages": [item.as_dict() for item in self.pages],
            "changed_paths": list(self.changed_paths),
            "protected_paths": list(self.protected_paths),
            "failure_count": len(self.failures),
        }


# Compatibility names used by early E-07 host prototypes.
KnowledgeRenderObservation = KnowledgePageObservation
KnowledgeRenderResult = KnowledgeRenderingResult


def _utc_timestamp(value: object | None = None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
    elif isinstance(value, date):
        current = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("rendered_at must be RFC3339") from exc
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
    else:
        raise TypeError("rendered_at must be a date, datetime, RFC3339 string, or None")
    if current.microsecond:
        current = current.replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _timestamp_after(candidate: str, previous: str) -> str:
    candidate_dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    previous_dt = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    if candidate_dt <= previous_dt:
        candidate_dt = previous_dt + timedelta(seconds=1)
    return candidate_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_from_result(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("project_map", "understanding", "hierarchical_understanding", "execution_flow", "research_linkage", "experiment_chains"):
        candidate = getattr(value, name, None)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    return None


def _safe_inline(value: object, *, fallback: str = "unknown", maximum: int = 240) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.replace("\x00", "").split())
    text = _WINDOWS_ABS_RE.sub("[local path omitted]", text)
    text = _HEX_LONG_RE.sub("[fingerprint omitted]", text)
    if len(text) > maximum:
        text = text[: maximum - 1].rstrip() + "…"
    return text or fallback


def _safe_body(value: object, *, fallback: str, maximum: int = 24000) -> str:
    if not isinstance(value, str):
        return fallback.rstrip() + "\n"
    text = value.replace("\x00", "")
    text = _WINDOWS_ABS_RE.sub("[local path omitted]", text)
    text = _UNIX_ABS_RE.sub("[local path omitted]", text)
    # Do not copy machine hashes into curated Markdown.  Relative source paths
    # are retained because they are useful human locators.
    text = _HEX_LONG_RE.sub("[fingerprint omitted]", text)
    if len(text.encode("utf-8")) > maximum:
        encoded = text.encode("utf-8")[:maximum]
        text = encoded.decode("utf-8", errors="ignore").rstrip() + "\n\n[Content bounded by renderer.]\n"
    if not text.endswith("\n"):
        text += "\n"
    return text


def _slug(value: object, *, fallback: str = "item") -> str:
    text = _safe_inline(value, fallback=fallback, maximum=120).casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or fallback


def _short_key(value: object, identity: object | None = None) -> str:
    text = _safe_inline(value, fallback="item", maximum=80)
    identity_text = _safe_inline(identity, fallback="", maximum=160)
    material = text if not identity_text else f"{text}\x1f{identity_text}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(text, fallback='item')[:48].rstrip('-')}-{digest}"


def _reason_placeholder(reason_code: str, next_action: str) -> str:
    return (
        "> **DRAFT - not verified**\n"
        f"> **Reason:** `{reason_code}`\n"
        f"> **Next action:** {_safe_inline(next_action, fallback='Provide a grounded host observation.')}\n\n"
        "This page is intentionally non-empty. It does not claim that missing material was read or verified.\n"
    )


def _mixed_body(generated: str, region_ids: Sequence[str]) -> str:
    generated = generated.rstrip("\n") + "\n\n"
    blocks = [generated]
    for region_id in region_ids:
        blocks.extend(
            [
                f'<!-- llmwiki:user-region:start id="{region_id}" -->\n',
                "\n",
                f'<!-- llmwiki:user-region:end id="{region_id}" -->\n',
            ]
        )
    return "".join(blocks)


def _frontmatter(
    *,
    project_id: str,
    artifact_type: str,
    title: str,
    rendered_at: str,
    ownership: str,
    status: str = "draft",
    generated_at: str | None = None,
    updated_at: str | None = None,
    source_ids: Sequence[str] = (),
    evidence_refs: Sequence[Mapping[str, str]] = (),
    last_verified_at: str | None = None,
) -> dict[str, Any]:
    # The serializer/validator is the authority for all field and ID checks.
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "kind": KNOWLEDGE_PAGE_KIND,
        "project_id": project_id,
        "artifact_type": artifact_type,
        "title": title,
        "status": status,
        "ownership": ownership,
        "source_ids": list(source_ids),
        "evidence_refs": [dict(item) for item in evidence_refs],
        "generated_at": generated_at or rendered_at,
        "updated_at": updated_at or rendered_at,
        "last_verified_at": last_verified_at,
    }


def _valid_source_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and _SOURCE_ID_RE.fullmatch(item))


def _valid_evidence_refs(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        evidence_id = item.get("evidence_id")
        stance = item.get("stance")
        if (
            isinstance(evidence_id, str)
            and _EVIDENCE_ID_RE.fullmatch(evidence_id)
            and evidence_id not in seen
            and stance in {"supporting", "opposing", "context"}
        ):
            result.append({"evidence_id": evidence_id, "stance": stance})
            seen.add(evidence_id)
    return tuple(result)


def _override_frontmatter(
    base: dict[str, Any], override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not override:
        return base
    # Only fields in the current contract can be supplied by a host.  Unknown
    # fields are ignored here; serialize_knowledge_frontmatter remains the final
    # strict gate and never receives caller-controlled extras.
    for key in ("title", "status", "ownership", "generated_at", "updated_at", "last_verified_at"):
        if key in override and isinstance(override[key], str):
            base[key] = override[key]
    if "source_ids" in override:
        base["source_ids"] = list(_valid_source_ids(override["source_ids"]))
    if "evidence_refs" in override:
        base["evidence_refs"] = list(_valid_evidence_refs(override["evidence_refs"]))
    return base


def _spec(path: str) -> dict[str, str]:
    for item in PRODUCT_ARTIFACT_SPECS:
        if item["path"] == path:
            return item
    raise KeyError(path)


def _detail_path(collection: str, title: object, identity: object) -> str:
    # Identity is part of the filename digest: two caller-declared entities with
    # the same display title must remain distinct rather than being merged.
    display = title if title not in (None, "") else identity
    return f"{collection}/{_short_key(display, identity)}.md"


def _status_for_payload(payload: Mapping[str, Any] | None) -> tuple[str, str, str]:
    if payload is None:
        return "draft", "missing-machine-artifact", "missing"
    derivation = _mapping(payload.get("derivation"))
    if derivation.get("source_content_read") is False and derivation.get("semantic_input") is False:
        return "draft", "metadata-only-machine-artifact", "available"
    return "draft", "grounded-host-observation", "available"


def _list_lines(values: Iterable[object], *, empty: str = "- None recorded") -> str:
    rows = [_safe_inline(item, fallback="unknown") for item in values]
    return "\n".join(f"- {row}" for row in rows) if rows else empty


def _table(rows: Sequence[Sequence[object]], headers: Sequence[str]) -> str:
    if not rows:
        return "- None recorded\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_safe_inline(item) for item in row) + " |")
    return "\n".join(out) + "\n"

@dataclass(frozen=True)
class _RenderingContext:
    project_id: str
    rendered_at: str
    plan_date: str
    project_name: str
    onboarding: Mapping[str, Any]
    machine: Mapping[str, Mapping[str, Any] | None]
    machine_reasons: Mapping[str, str]
    host_data: Mapping[str, Any]


def _load_machine_artifacts(
    workspace_root: str | Path,
    project_id: str,
) -> tuple[dict[str, Mapping[str, Any] | None], dict[str, str]]:
    # Imports stay local so renderer imports do not widen adapter initialization.
    from tools.execution_flow import load_current_execution_flow
    from tools.experiment_chains import load_current_experiment_chains
    from tools.hierarchical_understanding import load_current_hierarchical_understanding
    from tools.project_map import load_current_project_map
    from tools.research_goals import load_goal
    from tools.research_linkage import load_current_research_linkage
    from tools.research_planning import load_initial_plan
    from tools.research_state import load_project_state
    from tools.research_tasks import load_tasks

    loaders: tuple[tuple[str, Callable[..., Any]], ...] = (
        ("project_map", load_current_project_map),
        ("hierarchical", load_current_hierarchical_understanding),
        ("execution_flow", load_current_execution_flow),
        ("research_linkage", load_current_research_linkage),
        ("experiment_chains", load_current_experiment_chains),
        ("goal", load_goal),
        ("tasks", load_tasks),
        ("project_state", load_project_state),
        ("initial_plan", load_initial_plan),
    )
    payloads: dict[str, Mapping[str, Any] | None] = {}
    reasons: dict[str, str] = {}
    for key, loader in loaders:
        try:
            loaded = loader(workspace_root, project_id)
            if isinstance(loaded, Mapping):
                payloads[key] = dict(loaded)
            elif hasattr(loaded, "as_dict"):
                payloads[key] = dict(loaded.as_dict())
            else:  # pragma: no cover - defensive adapter guard
                raise TypeError(f"{key} loader returned an unsupported value")
            reasons[key] = "current-machine-artifact"
        except (LayoutError, OSError, ValueError, TypeError):
            payloads[key] = None
            reasons[key] = f"{key}-unavailable-or-stale"
    return payloads, reasons


def _page_overrides(host_data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = host_data.get("pages", {})
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        for path, value in raw.items():
            if isinstance(path, str) and isinstance(value, Mapping):
                result[path] = value
        return result
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for value in raw:
            if isinstance(value, Mapping) and isinstance(value.get("path"), str):
                result[str(value["path"])] = value
    return result


def _host_items(host_data: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    raw = host_data.get(key, ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _project_map_body(context: _RenderingContext) -> tuple[str, str, str]:
    payload = context.machine.get("project_map")
    if payload is None:
        return (
            "# Project map\n\n" + _reason_placeholder(
                context.machine_reasons["project_map"],
                "Run the deterministic E-02 project-map stage against the current Manifest.",
            ),
            context.machine_reasons["project_map"],
            "missing",
        )
    directories = payload.get("directories", [])
    distributions = _mapping(payload.get("distributions"))
    candidates = _mapping(payload.get("candidates"))
    directory_rows: list[tuple[object, ...]] = []
    if isinstance(directories, Sequence):
        for item in directories[:80]:
            row = _mapping(item)
            directory_rows.append(
                (
                    row.get("path", "unknown"),
                    row.get("recursive_file_count", 0),
                    row.get("recursive_byte_count", 0),
                )
            )
    language_rows: list[tuple[object, ...]] = []
    languages = distributions.get("languages", [])
    if isinstance(languages, Sequence):
        for item in languages[:40]:
            row = _mapping(item)
            language_rows.append((row.get("value", "unknown"), row.get("file_count", 0), row.get("byte_count", 0)))
    sections = [
        "# Project map\n\n",
        "This page is rendered from the current deterministic project-map artifact. It contains project-relative locations only.\n\n",
        "## Directory structure\n\n",
        _table(directory_rows, ("Directory", "Files", "Bytes")),
        "\n## Language distribution\n\n",
        _table(language_rows, ("Language", "Files", "Bytes")),
        "\n## Candidate entrypoints\n\n",
    ]
    for key, label in (
        ("entrypoints", "Entrypoints"),
        ("dependencies", "Dependencies"),
        ("configurations", "Configuration"),
        ("run_scripts", "Run scripts"),
        ("key_files", "Key files"),
    ):
        values = candidates.get(key, [])
        sections.append(f"### {label}\n\n")
        if isinstance(values, Sequence) and values:
            for item in values[:40]:
                row = _mapping(item)
                sections.append(
                    f"- `{_safe_inline(row.get('path'))}` — {_safe_inline(', '.join(str(v) for v in row.get('reason_codes', []) if isinstance(v, str)), fallback='metadata candidate')}\n"
                )
        else:
            sections.append("- No candidate was established.\n")
        sections.append("\n")
    return "".join(sections), "current-project-map", "available"


def _overview_body(context: _RenderingContext) -> tuple[str, str, str]:
    project_map = context.machine.get("project_map")
    hierarchy = context.machine.get("hierarchical")
    goal = context.onboarding.get("final_goal")
    stage = context.onboarding.get("current_stage")
    question = context.onboarding.get("important_question")
    if project_map is None and hierarchy is None:
        placeholder = _reason_placeholder(
            "project-understanding-unavailable",
            "Run inventory and E-02/E-03, or supply a grounded host project summary.",
        )
    else:
        placeholder = ""
    summary = "No grounded project-level semantic summary is available."
    if hierarchy is not None:
        summary = _safe_inline(_mapping(hierarchy.get("project")).get("summary"), fallback=summary, maximum=1200)
    manifest = _mapping(project_map.get("manifest")) if project_map is not None else {}
    file_count = manifest.get("ordinary_file_count", "unknown")
    language_count = 0
    if project_map is not None:
        languages = _mapping(project_map.get("distributions")).get("languages", [])
        language_count = len(languages) if isinstance(languages, Sequence) else 0
    body = (
        f"# {_safe_inline(context.project_name, fallback='Project')}\n\n"
        f"{placeholder}"
        "## Current bounded understanding\n\n"
        f"{summary}\n\n"
        "## Research direction\n\n"
        f"- **Final goal:** {_safe_inline(goal, fallback='DRAFT — not supplied')}\n"
        f"- **Current stage:** {_safe_inline(stage, fallback='DRAFT — not supplied')}\n"
        f"- **Most important question:** {_safe_inline(question, fallback='DRAFT — not supplied')}\n\n"
        "## Deterministic coverage snapshot\n\n"
        f"- Ordinary files represented by the current machine artifact: **{file_count}**\n"
        f"- Languages represented: **{language_count}**\n"
        "- Semantic claims remain draft unless separately grounded and verified.\n\n"
        "## Key entry points\n\n"
    )
    candidates = _mapping(project_map.get("candidates")) if project_map is not None else {}
    entrypoints = candidates.get("entrypoints", [])
    if isinstance(entrypoints, Sequence) and entrypoints:
        body += "\n".join(f"- `{_safe_inline(_mapping(item).get('path'))}`" for item in entrypoints[:20]) + "\n"
    else:
        body += "- No grounded entrypoint is available yet.\n"
    completeness = "available" if project_map is not None or hierarchy is not None else "missing"
    reason = "current-project-understanding" if completeness == "available" else "project-understanding-unavailable"
    return body, reason, completeness


def _reproduction_body(context: _RenderingContext) -> tuple[str, str, str]:
    project_map = context.machine.get("project_map")
    candidates = _mapping(project_map.get("candidates")) if project_map is not None else {}
    configurations = candidates.get("configurations", [])
    dependencies = candidates.get("dependencies", [])
    run_scripts = candidates.get("run_scripts", [])
    has_steps = any(isinstance(value, Sequence) and value for value in (configurations, dependencies, run_scripts))
    body = "# Reproduction\n\n"
    if not has_steps:
        body += _reason_placeholder(
            "reproduction-commands-not-grounded",
            "Supply policy-authorized environment, data-preparation, dependency, and run-command observations.",
        )
    body += (
        "## Environment and dependencies\n\n"
        + _list_lines(
            [f"`{_safe_inline(_mapping(item).get('path'))}`" for item in dependencies[:30]]
            if isinstance(dependencies, Sequence)
            else ()
        )
        + "\n\n## Configuration candidates\n\n"
        + _list_lines(
            [f"`{_safe_inline(_mapping(item).get('path'))}`" for item in configurations[:30]]
            if isinstance(configurations, Sequence)
            else ()
        )
        + "\n\n## Run commands\n\n"
        + _list_lines(
            [f"`{_safe_inline(_mapping(item).get('path'))}`" for item in run_scripts[:30]]
            if isinstance(run_scripts, Sequence)
            else ()
        )
        + "\n\n## Known missing conditions\n\n"
        "- The deterministic renderer does not infer commands from unread source content.\n"
        "- Dataset acquisition, environment versions, and expected outputs require grounded observations.\n"
    )
    return body, "reproduction-metadata-only" if has_steps else "reproduction-commands-not-grounded", "available" if has_steps else "missing"


def _architecture_body(context: _RenderingContext) -> tuple[str, str, str]:
    flow = context.machine.get("execution_flow")
    if flow is None:
        return (
            "# Architecture and data flow\n\n" + _reason_placeholder(
                context.machine_reasons["execution_flow"],
                "Run E-04 or supply grounded call/data-flow observations.",
            ),
            context.machine_reasons["execution_flow"],
            "missing",
        )
    nodes = flow.get("nodes", [])
    edges = flow.get("edges", [])
    entrypoints = set(flow.get("entrypoints", [])) if isinstance(flow.get("entrypoints"), Sequence) else set()
    node_rows: list[tuple[object, ...]] = []
    if isinstance(nodes, Sequence):
        for item in nodes[:80]:
            row = _mapping(item)
            node_rows.append((row.get("label"), row.get("kind"), row.get("certainty"), "yes" if row.get("id") in entrypoints or row.get("entrypoint") else "no"))
    edge_rows: list[tuple[object, ...]] = []
    if isinstance(edges, Sequence):
        for item in edges[:100]:
            row = _mapping(item)
            edge_rows.append((row.get("source"), row.get("relation", row.get("kind")), row.get("target"), row.get("certainty")))
    uncertainties = flow.get("uncertainties", [])
    body = (
        "# Architecture and data flow\n\n"
        f"{_safe_inline(flow.get('summary'), fallback='No flow summary is available.', maximum=1200)}\n\n"
        "## Entrypoints and nodes\n\n"
        + _table(node_rows, ("Node", "Kind", "Certainty", "Entrypoint"))
        + "\n## Directed flow\n\n"
        + _table(edge_rows, ("From", "Relation", "To", "Certainty"))
        + "\n## Uncertainty\n\n"
        + _list_lines(uncertainties if isinstance(uncertainties, Sequence) else ())
        + "\n"
    )
    return body, "current-execution-flow", "available"


def _entity_collection(context: _RenderingContext, kinds: set[str]) -> list[Mapping[str, Any]]:
    linkage = context.machine.get("research_linkage")
    entities = linkage.get("entities", []) if linkage is not None else []
    result: list[Mapping[str, Any]] = []
    if isinstance(entities, Sequence):
        for item in entities:
            row = _mapping(item)
            if row.get("kind") in kinds:
                result.append(row)
    return result


def _entity_detail_page(
    context: _RenderingContext,
    *,
    collection: str,
    artifact_type: str,
    item: Mapping[str, Any],
) -> KnowledgePageInput:
    title = _safe_inline(item.get("title"), fallback=f"{artifact_type.title()} candidate", maximum=160)
    identity = item.get("id", title)
    path = _detail_path(collection, title, identity)
    certainty = _safe_inline(item.get("certainty"), fallback="uncertain")
    assertion = _safe_inline(item.get("assertion_class"), fallback="metadata")
    summary = _safe_inline(item.get("summary"), fallback="No grounded summary is available.", maximum=1600)
    location = item.get("path")
    body = (
        f"# {title}\n\n"
        f"> **DRAFT:** assertion class `{assertion}`, certainty `{certainty}`.\n\n"
        "## Summary\n\n"
        f"{summary}\n\n"
        "## Project relationship\n\n"
        f"- Project-relative source candidate: `{_safe_inline(location, fallback='not supplied')}`\n"
        f"- Evidence references with directional stance: **{len(_valid_evidence_refs(item.get('evidence_refs')))}**\n\n"
        "## Verification gap\n\n"
        "This page is not a verified scientific conclusion. Supply current Source/Evidence bindings and an explicit lifecycle decision before verification.\n"
    )
    frontmatter = _frontmatter(
        project_id=context.project_id,
        artifact_type=artifact_type,
        title=title,
        rendered_at=context.rendered_at,
        ownership="generated",
        source_ids=_valid_source_ids(item.get("source_ids")),
        evidence_refs=_valid_evidence_refs(item.get("evidence_refs")),
    )
    return KnowledgePageInput(
        path=path,
        artifact_type=artifact_type,
        title=title,
        frontmatter=frontmatter,
        body=body,
        reason_code="research-linkage-candidate",
        completeness="available",
    )


def _collection_index_body(
    title: str,
    items: Sequence[KnowledgePageInput],
    *,
    reason_code: str,
    next_action: str,
    extra: str = "",
) -> tuple[str, str, str]:
    body = f"# {title}\n\n"
    if not items:
        body += _reason_placeholder(reason_code, next_action)
    body += "## Entries\n\n"
    if items:
        for item in sorted(items, key=lambda value: value.path):
            body += f"- [{item.title}]({item.path.split('/', 1)[-1]}) — `{item.completeness}`\n"
    else:
        body += "- No grounded entry is available.\n"
    if extra:
        body += "\n" + extra.rstrip() + "\n"
    return body, reason_code if not items else "grounded-collection-candidates", "missing" if not items else "available"


def _experiment_detail_pages(context: _RenderingContext) -> tuple[list[KnowledgePageInput], list[KnowledgePageInput], list[KnowledgePageInput]]:
    chains = context.machine.get("experiment_chains")
    experiment_pages: dict[str, KnowledgePageInput] = {}
    result_pages: dict[str, KnowledgePageInput] = {}
    claim_pages: dict[str, KnowledgePageInput] = {}
    if chains is None:
        return [], [], []

    configs = chains.get("configs", [])
    runs = chains.get("runs", [])
    explicit_chains = chains.get("chains", [])
    if isinstance(configs, Sequence):
        for item in configs:
            row = _mapping(item)
            title = _safe_inline(row.get("title", row.get("name", row.get("id"))), fallback="Experiment configuration")
            path = _detail_path("experiments", title, row.get("id", title))
            body = (
                f"# {title}\n\n"
                "> **DRAFT:** grounded experiment configuration observation.\n\n"
                "## Configuration\n\n"
                f"- Identity: `{_safe_inline(row.get('id'), fallback='not supplied')}`\n"
                f"- Project-relative location: `{_safe_inline(row.get('path'), fallback='not supplied')}`\n"
                f"- Certainty: `{_safe_inline(row.get('certainty'), fallback='uncertain')}`\n\n"
                "## Structured settings\n\n"
                f"```json\n{_bounded_json(row.get('configuration', row.get('settings', {})))}\n```\n"
            )
            experiment_pages[path] = _detail_input(context, path, "experiment", title, body, row, "experiment-configuration")
    if isinstance(runs, Sequence):
        for item in runs:
            row = _mapping(item)
            title = _safe_inline(row.get("title", row.get("name", row.get("id"))), fallback="Experiment run")
            path = _detail_path("experiments", title, row.get("id", title))
            body = (
                f"# {title}\n\n"
                "> **DRAFT:** grounded experiment-run observation.\n\n"
                "## Run state\n\n"
                f"- Run ID: `{_safe_inline(row.get('id'), fallback='not supplied')}`\n"
                f"- Configuration ID: `{_safe_inline(row.get('config_id'), fallback='not supplied')}`\n"
                f"- Status: `{_safe_inline(row.get('status'), fallback='unknown')}`\n"
                f"- Project-relative location: `{_safe_inline(row.get('path'), fallback='not supplied')}`\n\n"
                "## Conditions\n\n"
                f"```json\n{_bounded_json(row.get('conditions', {}))}\n```\n"
            )
            experiment_pages[path] = _detail_input(context, path, "experiment", title, body, row, "experiment-run")
    if isinstance(explicit_chains, Sequence):
        for item in explicit_chains:
            row = _mapping(item)
            title = _safe_inline(row.get("title", row.get("id")), fallback="Experiment chain")
            path = _detail_path("experiments", title, row.get("id", title))
            body = (
                f"# {title}\n\n"
                "> **DRAFT:** host-declared configuration, run, result, and Claim chain.\n\n"
                "## Chain\n\n"
                f"- Configuration: `{_safe_inline(row.get('config_id'), fallback='not supplied')}`\n"
                f"- Run: `{_safe_inline(row.get('run_id'), fallback='not supplied')}`\n"
                f"- Result: `{_safe_inline(row.get('result_id'), fallback='not supplied')}`\n"
                f"- Claim: `{_safe_inline(row.get('claim_id'), fallback='not supplied')}`\n"
            )
            experiment_pages[path] = _detail_input(context, path, "experiment", title, body, row, "experiment-chain")

    results = chains.get("results", [])
    if isinstance(results, Sequence):
        for item in results:
            row = _mapping(item)
            title = _safe_inline(row.get("title", row.get("name", row.get("id"))), fallback="Experiment result")
            path = _detail_path("results", title, row.get("id", title))
            body = (
                f"# {title}\n\n"
                "> **DRAFT:** result values remain bound to the stated conditions and supplied Evidence.\n\n"
                "## Conditions\n\n"
                f"```json\n{_bounded_json(row.get('conditions', {}))}\n```\n\n"
                "## Metrics\n\n"
                f"```json\n{_bounded_json(row.get('metrics', {}))}\n```\n\n"
                "## Evidence coverage\n\n"
                f"- Declared Evidence IDs: **{len(row.get('evidence_ids', [])) if isinstance(row.get('evidence_ids'), Sequence) else 0}**\n"
            )
            result_pages[path] = _detail_input(context, path, "result", title, body, row, "experiment-result")

    claims = chains.get("claims", [])
    if isinstance(claims, Sequence):
        for item in claims:
            row = _mapping(item)
            title = _safe_inline(row.get("title", row.get("claim", row.get("id"))), fallback="Claim candidate")
            path = _detail_path("claims", title, row.get("id", title))
            body = (
                f"# {title}\n\n"
                "> **DRAFT:** this renderer does not infer support, opposition, conflict, or verification.\n\n"
                "## Claim statement\n\n"
                f"{_safe_inline(row.get('statement', row.get('summary')), fallback='No grounded Claim statement was supplied.', maximum=1800)}\n\n"
                "## Experiment interpretation\n\n"
                f"- Declared certainty: `{_safe_inline(row.get('certainty'), fallback='uncertain')}`\n"
                f"- Declared Evidence IDs without renderer-inferred stance: **{len(row.get('evidence_ids', [])) if isinstance(row.get('evidence_ids'), Sequence) else 0}**\n\n"
                "Verification requires a separate F-02/F-04 proof and controlled lifecycle decision.\n"
            )
            claim_pages[path] = _detail_input(context, path, "claim", title, body, row, "claim-candidate")
    return list(experiment_pages.values()), list(result_pages.values()), list(claim_pages.values())


def _bounded_json(value: object, maximum: int = 4000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    except (TypeError, ValueError):
        return "{}"
    text = _HEX_LONG_RE.sub("[fingerprint omitted]", text)
    text = _WINDOWS_ABS_RE.sub("[local path omitted]", text)
    text = _UNIX_ABS_RE.sub("[local path omitted]", text)
    encoded = text.encode("utf-8")
    if len(encoded) > maximum:
        text = encoded[:maximum].decode("utf-8", errors="ignore").rstrip() + "\n…"
    return text


def _detail_input(
    context: _RenderingContext,
    path: str,
    artifact_type: str,
    title: str,
    body: str,
    item: Mapping[str, Any],
    reason_code: str,
) -> KnowledgePageInput:
    return KnowledgePageInput(
        path=path,
        artifact_type=artifact_type,
        title=title,
        frontmatter=_frontmatter(
            project_id=context.project_id,
            artifact_type=artifact_type,
            title=title,
            rendered_at=context.rendered_at,
            ownership="generated",
            source_ids=_valid_source_ids(item.get("source_ids")),
            evidence_refs=_valid_evidence_refs(item.get("evidence_refs")),
        ),
        body=_safe_body(body, fallback=f"# {title}\n\n" + _reason_placeholder("empty-detail", "Supply grounded detail.")),
        reason_code=reason_code,
        completeness="available",
    )


def _questions(context: _RenderingContext) -> tuple[list[str], list[str]]:
    questions: list[str] = []
    risks: list[str] = []
    for key in ("research_linkage", "experiment_chains"):
        payload = context.machine.get(key)
        values = payload.get("gaps", []) if payload is not None else []
        if isinstance(values, Sequence):
            questions.extend(_safe_inline(value) for value in values[:80])
    flow = context.machine.get("execution_flow")
    if flow is not None and isinstance(flow.get("uncertainties"), Sequence):
        risks.extend(_safe_inline(value) for value in flow.get("uncertainties", [])[:80])
    hierarchy = context.machine.get("hierarchical")
    if hierarchy is not None:
        omissions = _mapping(hierarchy.get("omissions"))
        for key, value in omissions.items():
            if isinstance(value, int) and value:
                risks.append(f"Hierarchical understanding omitted {value} {key} because of deterministic bounds.")
    for key, reason in context.machine_reasons.items():
        if context.machine.get(key) is None:
            risks.append(f"Machine artifact `{key}` is unavailable: `{reason}`.")
    for item in _host_items(context.host_data, "open_questions"):
        questions.append(_safe_inline(item.get("question", item.get("title")), fallback="Host-declared open question", maximum=500))
    return list(dict.fromkeys(questions)), list(dict.fromkeys(risks))


def _open_questions_body(context: _RenderingContext) -> tuple[str, str, str]:
    questions, _ = _questions(context)
    body = "# Open questions\n\n"
    if not questions:
        body += _reason_placeholder(
            "open-questions-not-grounded",
            "Record explicit questions, their impact, required Evidence, and related tasks.",
        )
    body += "## Generated question register\n\n"
    if questions:
        for index, question in enumerate(questions[:100], 1):
            body += (
                f"### Q{index}\n\n"
                f"- **Question:** {question}\n"
                "- **Impact:** DRAFT — not assessed\n"
                "- **Evidence needed:** DRAFT — not specified\n"
                "- **Related task:** DRAFT — not linked\n\n"
            )
    else:
        body += "- No explicit question has been grounded.\n\n"
    body += "## User-maintained questions\n\n"
    body = _mixed_body(body, ("user-questions",))
    return body, "machine-gaps" if questions else "open-questions-not-grounded", "available" if questions else "missing"


def _status_body(context: _RenderingContext) -> tuple[str, str, str]:
    machine_rows: list[tuple[object, ...]] = []
    for key in ("project_map", "hierarchical", "execution_flow", "research_linkage", "experiment_chains"):
        present = context.machine.get(key) is not None
        machine_rows.append((key, "current" if present else "missing/stale", context.machine_reasons[key]))
    chains = context.machine.get("experiment_chains")
    coverage = _mapping(chains.get("coverage")) if chains is not None else {}
    body = (
        "# Project status\n\n"
        "This snapshot is rebuildable from current machine artifacts. It is not a scientific verification decision.\n\n"
        "## Understanding stages\n\n"
        + _table(machine_rows, ("Stage artifact", "State", "Reason"))
        + "\n## Experiment snapshot\n\n"
        f"- Complete chains: **{coverage.get('complete_chains', 0)}**\n"
        f"- Results: **{coverage.get('results', 0)}**\n"
        f"- Claims: **{coverage.get('claims', 0)}**\n"
        f"- Unlinked results: **{coverage.get('unlinked_results', 0)}**\n\n"
        "## User-confirmed status\n\n"
    )
    return _mixed_body(body, ("user-status",)), "current-machine-status", "available"


def _risks_body(context: _RenderingContext) -> tuple[str, str, str]:
    questions, risks = _questions(context)
    hierarchy = context.machine.get("hierarchical")
    metadata_only = 0
    if hierarchy is not None:
        metadata_only = int(_mapping(hierarchy.get("coverage")).get("metadata_only_files", 0) or 0)
    body = "# Risks and missing information\n\n"
    if not risks and not questions and not metadata_only:
        body += _reason_placeholder(
            "risk-assessment-not-grounded",
            "Review unread files, extraction failures, reproduction gaps, privacy boundaries, and technical uncertainty.",
        )
    body += (
        "## Reading and evidence gaps\n\n"
        f"- Metadata-only files in current hierarchical understanding: **{metadata_only}**\n"
        f"- Open research gaps/questions: **{len(questions)}**\n\n"
        "## Deterministic risk register\n\n"
        + _list_lines(risks)
        + "\n\n## Fixed safety boundaries\n\n"
        "- Sensitive or policy-denied raw content is not copied into curated Markdown.\n"
        "- Missing C-07 research-binary metadata remains deferred and is not claimed as processed.\n"
        "- Draft summaries must not be treated as verified Claims.\n"
    )
    return body, "deterministic-risk-register", "available" if risks or metadata_only else "missing"


def _planning_reference(value: object) -> str:
    """Render a planning reference without exposing machine indexes or local paths."""

    if not isinstance(value, str):
        return "unknown"
    if value.startswith("machine:") or value.startswith("indexes/"):
        return "Core-managed current machine state"
    if value.startswith("knowledge:"):
        return _safe_inline(
            value.removeprefix("knowledge:"),
            fallback="knowledge artifact",
        )
    if value.startswith("evidence:") or value.startswith("evd-"):
        return "current Evidence reference"
    return _safe_inline(value, fallback="unknown", maximum=512)


def _goals_body(context: _RenderingContext) -> tuple[str, str, str]:
    payload = context.machine.get("goal")
    if payload is not None:
        criteria = payload.get("success_criteria", ())
        dependencies = payload.get("dependencies", ())
        draft_reasons = payload.get("draft_reasons", ())
        milestones = payload.get("milestones", ())
        criteria_values = (
            criteria
            if isinstance(criteria, Sequence)
            and not isinstance(criteria, (str, bytes, bytearray))
            else ()
        )
        dependency_values = (
            dependencies
            if isinstance(dependencies, Sequence)
            and not isinstance(dependencies, (str, bytes, bytearray))
            else ()
        )
        reason_values = (
            draft_reasons
            if isinstance(draft_reasons, Sequence)
            and not isinstance(draft_reasons, (str, bytes, bytearray))
            else ()
        )
        milestone_rows: list[tuple[object, ...]] = []
        if isinstance(milestones, Sequence) and not isinstance(
            milestones, (str, bytes, bytearray)
        ):
            for item in milestones:
                if isinstance(item, Mapping):
                    milestone_rows.append(
                        (
                            item.get("milestone_id", "unknown"),
                            item.get("title", "unknown"),
                            item.get("status", "draft"),
                            item.get("deadline") or "not supplied",
                        )
                    )
        body = (
            "# Initial goals and milestones\n\n"
            "> This page projects the strict current Goal artifact. "
            "DRAFT means user confirmation is still required.\n\n"
            "## Current Goal\n\n"
            f"- **Status:** "
            f"{_safe_inline(str(payload.get('status', 'draft')).upper())}\n"
            f"- **Goal ID:** `"
            f"{_safe_inline(payload.get('goal_id'), fallback='primary')}`\n"
            f"- **Goal:** "
            f"{_safe_inline(payload.get('goal'), fallback='DRAFT - awaiting user confirmation', maximum=1000)}\n"
            f"- **Current phase:** "
            f"{_safe_inline(payload.get('current_stage'), fallback='DRAFT - awaiting user confirmation')}\n"
            f"- **Deadline:** "
            f"{_safe_inline(payload.get('deadline'), fallback='not supplied')}\n\n"
            "### Success criteria\n\n"
            + _list_lines(
                criteria_values,
                empty="- DRAFT - awaiting user confirmation",
            )
            + "\n\n### Dependencies\n\n"
            + _list_lines(dependency_values, empty="- None recorded")
            + "\n\n### Draft reasons\n\n"
            + _list_lines(reason_values, empty="- awaiting-user-confirmation")
            + "\n\n## Milestones\n\n"
            + _table(
                milestone_rows,
                ("Milestone", "Title", "Status", "Deadline"),
            )
            + "\n## User-confirmed goals and milestones\n\n"
        )
        reason = (
            "current-goal-draft"
            if payload.get("status") == "draft"
            else "current-goal-artifact"
        )
        return _mixed_body(body, ("user-goals",)), reason, "available"

    goal = context.onboarding.get("final_goal")
    stage = context.onboarding.get("current_stage")
    question = context.onboarding.get("important_question")
    deadline = context.onboarding.get("deadline")
    hours = context.onboarding.get("daily_available_hours")
    supplied = any(
        value not in (None, "")
        for value in (goal, stage, question, deadline, hours)
    )
    body = (
        "# Initial goals and milestones\n\n"
        + (
            ""
            if supplied
            else _reason_placeholder(
                "goal-artifact-not-supplied",
                "Run initial planning, then confirm the project goal, success "
                "criteria, current stage, dependencies, and deadline.",
            )
        )
        + "## Onboarding fallback (DRAFT)\n\n"
        f"- **Goal:** "
        f"{_safe_inline(goal, fallback='DRAFT - awaiting user confirmation')}\n"
        "- **Success criteria:** DRAFT - awaiting user confirmation\n"
        f"- **Current phase:** "
        f"{_safe_inline(stage, fallback='DRAFT - awaiting user confirmation')}\n"
        f"- **Most important question:** "
        f"{_safe_inline(question, fallback='DRAFT - awaiting user confirmation')}\n"
        f"- **Deadline:** {_safe_inline(deadline, fallback='not supplied')}\n"
        f"- **Daily available time:** "
        f"{_safe_inline(str(hours) if hours is not None else None, fallback='not supplied')}\n\n"
        "## User-confirmed goals and milestones\n\n"
    )
    return (
        _mixed_body(body, ("user-goals",)),
        "onboarding-goal-fallback" if supplied else "goal-artifact-not-supplied",
        "available" if supplied else "missing",
    )


def _backlog_body(context: _RenderingContext) -> tuple[str, str, str]:
    payload = context.machine.get("tasks")
    if payload is not None:
        raw_tasks = payload.get("tasks", ())
        tasks = (
            tuple(item for item in raw_tasks if isinstance(item, Mapping))
            if isinstance(raw_tasks, Sequence)
            and not isinstance(raw_tasks, (str, bytes, bytearray))
            else ()
        )
        body = (
            "# Backlog\n\n"
            "This page projects the strict current task protocol. A task is "
            "executable only when its machine status is `ready` or "
            "`in_progress`; this page does not grant execution "
            "authorization.\n\n"
            "## Current tasks\n\n"
        )
        if not tasks:
            body += _reason_placeholder(
                "task-collection-empty",
                "Add or confirm a bounded task before execution.",
            )
        for task in tasks[:100]:
            task_id = _safe_inline(
                task.get("task_id"),
                fallback="unknown-task",
            )
            title = _safe_inline(
                task.get("title"),
                fallback="Untitled task",
                maximum=500,
            )
            inputs = task.get("inputs", ())
            allowed = task.get("allowed_paths", ())
            denied = task.get("denied_paths", ())
            dependencies = task.get("dependencies", ())
            dod = task.get("dod", ())
            verification = task.get("verification", ())
            artifacts = task.get("artifacts", ())
            blockers = task.get("draft_reasons", ())
            evidence = task.get("evidence", ())
            evidence_count = (
                len(evidence)
                if isinstance(evidence, Sequence)
                and not isinstance(evidence, (str, bytes, bytearray))
                else 0
            )
            timebox = task.get("timebox_minutes")
            body += (
                f"### `{task_id}` - {title}\n\n"
                f"- **status:** `"
                f"{_safe_inline(task.get('status'), fallback='draft')}`\n"
                f"- **why_now:** "
                f"{_safe_inline(task.get('why_now'), fallback='DRAFT - reason not confirmed', maximum=1000)}\n"
                f"- **timebox:** "
                f"{_safe_inline(str(timebox) if timebox is not None else None, fallback='not supplied')} minutes\n"
                f"- **Evidence refs:** {evidence_count}\n\n"
                "#### Inputs\n\n"
                + _list_lines(
                    (_planning_reference(item) for item in inputs),
                    empty="- None recorded",
                )
                + "\n\n#### Allowed source paths\n\n"
                + _list_lines(allowed, empty="- DRAFT - not confirmed")
                + "\n\n#### Denied source paths\n\n"
                + _list_lines(denied, empty="- None recorded")
                + "\n\n#### Dependencies\n\n"
                + _list_lines(dependencies, empty="- None")
                + "\n\n#### Definition of Done\n\n"
                + _list_lines(dod, empty="- DRAFT - not confirmed")
                + "\n\n#### Verification\n\n"
                + _list_lines(
                    verification,
                    empty="- DRAFT - not confirmed",
                )
                + "\n\n#### Expected artifacts\n\n"
                + _list_lines(
                    (_planning_reference(item) for item in artifacts),
                    empty="- DRAFT - not confirmed",
                )
                + "\n\n#### Draft blockers\n\n"
                + _list_lines(blockers, empty="- None")
                + "\n\n"
            )
        if len(tasks) > 100:
            body += (
                f"- {len(tasks) - 100} additional task(s) omitted from this "
                "bounded view.\n\n"
            )
        body += "## User-maintained backlog\n\n"
        return (
            _mixed_body(body, ("user-backlog",)),
            "current-task-protocol",
            "available",
        )

    questions, risks = _questions(context)
    suggestions = [
        "Ground the highest-impact open question with current Evidence",
        "Record reproducible environment and run commands",
        "Resolve unavailable or stale machine-understanding artifacts",
    ]
    if not questions:
        suggestions[0] = "Identify and record the highest-impact open research question"
    if not risks:
        suggestions[2] = (
            "Review reading coverage and record missing or policy-limited material"
        )
    body = (
        "# Backlog\n\n"
        + _reason_placeholder(
            "task-artifact-unavailable-or-stale",
            "Run initial planning to create a strict DRAFT task collection.",
        )
        + "All fallback suggestions are **DRAFT** and are not executable.\n\n"
        "## Generated fallback suggestions\n\n"
    )
    for index, suggestion in enumerate(suggestions, 1):
        focus = (
            questions[0]
            if questions
            else risks[0]
            if risks
            else "Initial project understanding is incomplete"
        )
        body += (
            f"### TASK-{index:02d}: {suggestion}\n\n"
            f"- **why_now:** {_safe_inline(focus)}\n"
            "- **status:** `draft`\n"
            "- **verification:** DRAFT - not confirmed\n\n"
        )
    body += "## User-maintained backlog\n\n"
    return (
        _mixed_body(body, ("user-backlog",)),
        "task-artifact-unavailable-or-stale",
        "missing",
    )


def _daily_plan_body(context: _RenderingContext) -> tuple[str, str, str]:
    payload = context.machine.get("initial_plan")
    if payload is not None and payload.get("plan_date") == context.plan_date:
        task_ids = payload.get("task_ids", ())
        inputs = payload.get("inputs", ())
        outputs = payload.get("outputs", ())
        verification = payload.get("verification", ())
        blockers = payload.get("blockers", ())
        body = (
            f"# Daily plan - {context.plan_date}\n\n"
            f"> **{_safe_inline(str(payload.get('status', 'draft')).upper())} "
            "/ review required:** this plan is bound to the current "
            "project-state snapshot. It does not authorize execution.\n\n"
            "## Plan\n\n"
            f"- **Goal ID:** `"
            f"{_safe_inline(payload.get('goal_id'), fallback='primary')}`\n"
            f"- **why_now:** "
            f"{_safe_inline(payload.get('why_now'), fallback='Review the current project state', maximum=1200)}\n"
            f"- **timebox:** "
            f"{_safe_inline(str(payload.get('timebox_minutes')), fallback='not supplied')} minutes\n\n"
            "### Selected task IDs\n\n"
            + _list_lines(task_ids, empty="- No task selected")
            + "\n\n### Inputs\n\n"
            + _list_lines(
                (_planning_reference(item) for item in inputs),
                empty="- None recorded",
            )
            + "\n\n### Expected outputs\n\n"
            + _list_lines(
                (_planning_reference(item) for item in outputs),
                empty="- None recorded",
            )
            + "\n\n### Verification\n\n"
            + _list_lines(
                verification,
                empty="- DRAFT - not confirmed",
            )
            + "\n\n### Blockers\n\n"
            + _list_lines(blockers, empty="- None recorded")
            + "\n\n## User adjustments\n\n"
        )
        return (
            _mixed_body(body, ("user-daily-plan",)),
            "current-initial-plan-draft",
            "available",
        )

    reason = (
        "initial-plan-date-mismatch"
        if payload is not None
        else "initial-plan-unavailable-or-stale"
    )
    questions, risks = _questions(context)
    focus = (
        questions[0]
        if questions
        else risks[0]
        if risks
        else "Confirm the project's next evidence-backed action"
    )
    hours = context.onboarding.get("daily_available_hours")
    timebox = (
        f"{hours} hours"
        if isinstance(hours, (int, float)) and not isinstance(hours, bool)
        else "DRAFT - timebox not supplied"
    )
    body = (
        f"# Daily plan - {context.plan_date}\n\n"
        + _reason_placeholder(
            reason,
            "Run initial planning for this date and review the resulting DRAFT.",
        )
        + "> **DRAFT / review required:** this fallback does not authorize "
        "execution.\n\n"
        "## Fallback focus\n\n"
        f"- **why_now:** {_safe_inline(focus, maximum=600)}\n"
        f"- **timebox:** {timebox}\n"
        "- **verification:** confirm the task protocol and source currentness\n\n"
        "## User adjustments\n\n"
    )
    return _mixed_body(body, ("user-daily-plan",)), reason, "missing"


def _apply_host_page_override(
    page: KnowledgePageInput,
    override: Mapping[str, Any],
    *,
    context: _RenderingContext,
) -> KnowledgePageInput:
    title = _safe_inline(override.get("title"), fallback=page.title, maximum=180)
    ownership = override.get("ownership", page.frontmatter.get("ownership", "generated"))
    if ownership not in {"generated", "mixed"}:
        ownership = page.frontmatter.get("ownership", "generated")
    body = _safe_body(override.get("body"), fallback=page.body)
    if ownership == "mixed":
        region_ids = MIXED_REGION_IDS.get(page.path)
        if page.path.startswith(DAILY_PLAN_PATH_PREFIX):
            region_ids = ("user-daily-plan",)
        if not region_ids:
            ownership = "generated"
        elif "llmwiki:user-region:start" not in body:
            body = _mixed_body(body, region_ids)
    frontmatter = _frontmatter(
        project_id=context.project_id,
        artifact_type=page.artifact_type,
        title=title,
        rendered_at=context.rendered_at,
        ownership=str(ownership),
        source_ids=_valid_source_ids(override.get("source_ids")),
        evidence_refs=_valid_evidence_refs(override.get("evidence_refs")),
    )
    # E-07 never grants a verified/conflicting/rejected lifecycle transition.
    # Such changes must be composed separately with F-02/F-04 authorization.
    return KnowledgePageInput(
        path=page.path,
        artifact_type=page.artifact_type,
        title=title,
        frontmatter=frontmatter,
        body=body,
        reason_code=_safe_inline(override.get("reason_code"), fallback="explicit-host-rendering-data", maximum=80),
        completeness=_safe_inline(override.get("completeness"), fallback=page.completeness, maximum=32),
    )

def _canonical_page_for_singleton(
    context: _RenderingContext,
    *,
    spec: Mapping[str, str],
    body_result: tuple[str, str, str],
    ownership: str = "generated",
) -> KnowledgePageInput:
    body, reason_code, completeness = body_result
    if ownership == "mixed":
        region_ids = MIXED_REGION_IDS.get(spec["path"], ())
        if region_ids:
            body = body if "llmwiki:user-region:start" in body else _mixed_body(body, region_ids)
        else:
            ownership = "generated"
    return KnowledgePageInput(
        path=spec["path"],
        artifact_type=spec["artifact_type"],
        title=spec["title"],
        frontmatter=_frontmatter(
            project_id=context.project_id,
            artifact_type=spec["artifact_type"],
            title=spec["title"],
            rendered_at=context.rendered_at,
            ownership=ownership,
        ),
        body=_safe_body(body, fallback=f"# {spec['title']}\n\n" + _reason_placeholder(reason_code, "Supply grounded project information.")),
        reason_code=reason_code,
        completeness=completeness,
    )


def _index_body(context: _RenderingContext, pages: Mapping[str, KnowledgePageInput]) -> str:
    state_lines: list[str] = []
    for spec in PRODUCT_ARTIFACT_SPECS:
        page = pages[spec["path"]]
        state = "available" if page.completeness == "available" else "DRAFT / missing or bounded"
        confirmation = "user confirmation required" if page.artifact_type in {"goal", "plan", "claim", "open_question", "project_status"} else ""
        state_lines.append(
            f"| {spec['title']} | [{spec['path']}]({spec['path']}) | {state} | {confirmation or '—'} |"
        )
    daily_paths = sorted(path for path in pages if path.startswith(DAILY_PLAN_PATH_PREFIX))
    if daily_paths:
        daily = daily_paths[0]
        daily_line = f"- Today's plan: [{daily}]({daily}) — {pages[daily].completeness}\n"
    else:
        daily_line = "- Today's plan: missing\n"
    return (
        f"# {_safe_inline(context.project_name, fallback='Project')} knowledge index\n\n"
        "This index is generated from the current machine artifacts and explicit host rendering observations. Draft pages are not verified scientific conclusions.\n\n"
        "## Fifteen product deliverables\n\n"
        "| Deliverable | Canonical page | State | Confirmation |\n"
        "|---|---|---|---|\n"
        + "\n".join(state_lines)
        + "\n\n"
        + daily_line
        + "\n## Navigation\n\n"
        "- Project overview: [overview.md](overview.md)\n"
        "- Project map: [project-map.md](project-map.md)\n"
        "- Reproduction: [reproduction.md](reproduction.md)\n"
        "- Architecture: [architecture.md](architecture.md)\n"
        "- Status and risks: [status.md](status.md), [risks.md](risks.md)\n"
        "- Goals and plans: [goals.md](goals.md), [plans/backlog.md](plans/backlog.md)\n\n"
        "## Regeneration boundary\n\n"
        "User-owned content and protected mixed-page regions are preserved by the controlled Markdown writer. Schema v1, future-schema, malformed, and non-draft lifecycle pages are reported rather than silently rewritten.\n"
    )


def build_project_knowledge_pages(
    workspace_root: str | Path,
    project_id: str,
    *,
    rendered_at: object | None = None,
    plan_date: object | None = None,
    rendering_data: Mapping[str, Any] | None = None,
    host_data: Mapping[str, Any] | None = None,
    machine_artifacts: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> tuple[KnowledgePageInput, ...]:
    """Build the complete proposed page set without reading or writing Markdown.

    The machine inputs are the current E-02--E-06 analysis artifacts,
    I-01--I-04 planning artifacts, and the registration record.
    ``rendering_data``/``host_data`` is an explicit host observation boundary;
    it is never inferred from source files by this module.
    """

    normalized_project_id = validate_project_id(project_id)
    timestamp = _utc_timestamp(rendered_at)
    day = (
        _utc_timestamp(plan_date)[:10]
        if plan_date is not None
        else timestamp[:10]
    )
    # Validate the date string before placing it in a canonical path.
    day = date.fromisoformat(day).isoformat()
    registration = load_registered_project(workspace_root, normalized_project_id)
    record = _mapping(registration.record)
    onboarding = _mapping(record.get("onboarding"))
    artifact_keys = (
        "project_map",
        "hierarchical",
        "execution_flow",
        "research_linkage",
        "experiment_chains",
        "goal",
        "tasks",
        "project_state",
        "initial_plan",
    )
    if machine_artifacts is None:
        machine, machine_reasons = _load_machine_artifacts(workspace_root, normalized_project_id)
    else:
        machine = {}
        machine_reasons = {}
        for key in artifact_keys:
            value = machine_artifacts.get(key)
            machine[key] = dict(value) if isinstance(value, Mapping) else None
            machine_reasons[key] = (
                "explicit-machine-artifact"
                if isinstance(value, Mapping)
                else f"{key}-unavailable-or-stale"
            )
    raw_host = host_data if host_data is not None else rendering_data
    host = dict(raw_host) if isinstance(raw_host, Mapping) else {}
    context = _RenderingContext(
        project_id=normalized_project_id,
        rendered_at=timestamp,
        plan_date=day,
        project_name=_safe_inline(record.get("name"), fallback=normalized_project_id, maximum=180),
        onboarding=onboarding,
        machine=machine,
        machine_reasons=machine_reasons,
        host_data=host,
    )

    pages: dict[str, KnowledgePageInput] = {}
    singleton_builders: dict[str, Callable[[_RenderingContext], tuple[str, str, str]]] = {
        "overview.md": _overview_body,
        "project-map.md": _project_map_body,
        "reproduction.md": _reproduction_body,
        "architecture.md": _architecture_body,
        "open-questions.md": _open_questions_body,
        "status.md": _status_body,
        "risks.md": _risks_body,
        "goals.md": _goals_body,
        "plans/backlog.md": _backlog_body,
    }
    for spec in PRODUCT_ARTIFACT_SPECS:
        path = spec["path"]
        if path in singleton_builders:
            ownership = "mixed" if path in MIXED_REGION_IDS else "generated"
            pages[path] = _canonical_page_for_singleton(
                context,
                spec=spec,
                body_result=singleton_builders[path](context),
                ownership=ownership,
            )

    experiment_pages, result_pages, claim_pages = _experiment_detail_pages(context)
    detail_pages: list[KnowledgePageInput] = []
    linkage_kinds: dict[str, tuple[str, set[str]]] = {
        "papers": ("paper", {"paper"}),
        "methods": ("method", {"method", "innovation", "implementation"}),
        "datasets": ("dataset", {"dataset"}),
        "experiments": ("experiment", {"experiment", "notebook"}),
        "results": ("result", {"result", "metric"}),
        "claims": ("claim", {"claim"}),
    }
    for collection, (artifact_type, kinds) in linkage_kinds.items():
        for item in _entity_collection(context, kinds):
            detail_pages.append(
                _entity_detail_page(context, collection=collection, artifact_type=artifact_type, item=item)
            )
    detail_pages.extend(experiment_pages)
    detail_pages.extend(result_pages)
    detail_pages.extend(claim_pages)
    # Explicit host entities are additive and are not merged solely by title.
    for collection, artifact_type in (
        ("papers", "paper"),
        ("methods", "method"),
        ("datasets", "dataset"),
        ("experiments", "experiment"),
        ("results", "result"),
        ("claims", "claim"),
    ):
        for item in _host_items(host, collection):
            detail_pages.append(_entity_detail_page(context, collection=collection, artifact_type=artifact_type, item=item))
    for page in detail_pages:
        pages.setdefault(page.path, page)

    # Collection indexes are always present, even when no detail page is
    # grounded.  This is the key E-07 guarantee for the fixed 15-item fixture.
    collection_specs = {
        "papers/index.md": ("paper", "Papers", "papers", "papers-not-grounded", "Supply a paper/claim observation with current Evidence."),
        "methods/index.md": ("method", "Methods and innovations", "methods", "methods-not-grounded", "Supply a method-to-implementation observation."),
        "datasets/index.md": ("dataset", "Datasets", "datasets", "datasets-not-grounded", "Supply dataset source, split, preprocessing, and format observations."),
        "experiments/index.md": ("experiment", "Experiments", "experiments", "experiments-not-grounded", "Supply configuration, run, and status observations."),
        "results/index.md": ("result", "Results and metrics", "results", "results-not-grounded", "Supply result conditions, metrics, and Evidence."),
        "claims/index.md": ("claim", "Confirmed claims", "claims", "claims-not-grounded", "Supply a Claim plus current supporting/opposing Evidence and a lifecycle decision."),
    }
    for path, (artifact_type, title, collection, missing_reason, next_action) in collection_specs.items():
        entries = [item for item in pages.values() if item.path.startswith(collection + "/") and item.path != path]
        extra = ""
        if collection == "claims":
            extra = "## User-confirmed claims\n\n"
        body, reason, completeness = _collection_index_body(title, entries, reason_code=missing_reason, next_action=next_action, extra=extra)
        pages[path] = _canonical_page_for_singleton(
            context,
            spec={"path": path, "artifact_type": artifact_type, "title": title},
            body_result=(body, reason, completeness),
            ownership="mixed" if path in MIXED_REGION_IDS else "generated",
        )

    daily_path = f"{DAILY_PLAN_PATH_PREFIX}{day}.md"
    daily_body, daily_reason, daily_completeness = _daily_plan_body(context)
    pages[daily_path] = KnowledgePageInput(
        path=daily_path,
        artifact_type="plan",
        title=f"Daily plan — {day}",
        frontmatter=_frontmatter(
            project_id=normalized_project_id,
            artifact_type="plan",
            title=f"Daily plan — {day}",
            rendered_at=timestamp,
            ownership="mixed",
        ),
        body=_safe_body(daily_body, fallback=_reason_placeholder(daily_reason, "Confirm today's plan.")),
        reason_code=daily_reason,
        completeness=daily_completeness,
    )

    # Apply explicit host page bodies after deterministic fallbacks, then build
    # the unified index from the final proposed collection.
    overrides = _page_overrides(host)
    for path, override in overrides.items():
        if path == "index.md":
            continue
        if path in pages:
            pages[path] = _apply_host_page_override(pages[path], override, context=context)
            continue
        # A host may provide a canonical detail page not represented by E-05/E-06.
        try:
            from tools.knowledge_artifacts import artifact_contract_for_path
            contract = artifact_contract_for_path(path)
        except (KnowledgeArtifactError, ValueError, TypeError):
            continue
        if contract.page_role not in {"detail", "daily_plan"}:
            continue
        title = _safe_inline(override.get("title"), fallback=contract.slug or path, maximum=180)
        page = KnowledgePageInput(
            path=path,
            artifact_type=contract.artifact_type,
            title=title,
            frontmatter=_frontmatter(
                project_id=normalized_project_id,
                artifact_type=contract.artifact_type,
                title=title,
                rendered_at=timestamp,
                ownership="generated",
            ),
            body=_safe_body(override.get("body"), fallback=_reason_placeholder("explicit-page-body-missing", "Supply a bounded host rendering.")),
            reason_code="explicit-host-rendering-data",
            completeness="available" if override.get("body") else "missing",
        )
        pages[path] = _apply_host_page_override(page, override, context=context)

    index_spec = {"path": "index.md", "artifact_type": "project_index", "title": f"{context.project_name} knowledge index"}
    pages["index.md"] = KnowledgePageInput(
        path="index.md",
        artifact_type="project_index",
        title=index_spec["title"],
        frontmatter=_frontmatter(
            project_id=normalized_project_id,
            artifact_type="project_index",
            title=index_spec["title"],
            rendered_at=timestamp,
            ownership="generated",
        ),
        body=_safe_body(_index_body(context, pages), fallback="# Knowledge index\n\n" + _reason_placeholder("index-build-failed", "Rebuild the renderer input set.")),
        reason_code="unified-index",
        completeness="available",
    )
    return tuple(pages[path] for path in sorted(pages))



def _knowledge_read_target(root: Path, relative_path: str) -> Path:
    """Resolve a canonical knowledge path without following redirections.

    Registration creates the empty directory skeleton.  Rendering never creates
    missing directories, and this read-side guard keeps a malicious symlink in the
    curated tree from making the renderer read outside the configured root.
    """

    contract = artifact_contract_for_path(relative_path)
    root = root.expanduser().resolve()
    target = root / contract.path
    try:
        target.relative_to(root)
    except ValueError as exc:  # pragma: no cover - contract validation is primary
        raise KnowledgeArtifactError("knowledge path escapes configured root") from exc
    current = root
    for component in Path(contract.path).parts:
        current = current / component
        if not current.exists() and not current.is_symlink():
            continue
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise KnowledgeArtifactError("knowledge path could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise KnowledgeArtifactError("knowledge path must not traverse a symlink")
    return target


def _schema_failure_reason(payload: bytes) -> str:
    """Return a stable, body-free reason for an unreadable existing page."""

    prefix = payload[:2048].decode("utf-8", errors="ignore")
    match = re.search(r"(?m)^schema_version:\s*([0-9]+)\s*$", prefix)
    if match:
        version = match.group(1)
        if version == "1":
            return "legacy-schema-v1-read-only"
        return "unsupported-or-future-schema"
    return "malformed-knowledge-page"


def _semantic_page_key(payload: bytes, path: str) -> tuple[dict[str, Any], str]:
    """Compare pages while ignoring renderer timestamps only."""

    parsed = parse_knowledge_page(payload, path=path)
    frontmatter = parsed.frontmatter.as_dict()
    frontmatter.pop("generated_at", None)
    frontmatter.pop("updated_at", None)
    return frontmatter, parsed.body


def _page_observation(
    page: KnowledgePageInput,
    *,
    action: str,
    status: str,
    reason_code: str | None = None,
    current_sha256: str | None = None,
    output_sha256: str | None = None,
    written: bool = False,
    error_code: str | None = None,
) -> KnowledgePageObservation:
    return KnowledgePageObservation(
        path=page.path,
        artifact_type=page.artifact_type,
        title=page.title,
        action=action,
        status=status,
        ownership=str(page.frontmatter.get("ownership", "generated")),
        reason_code=reason_code or page.reason_code,
        completeness=page.completeness,
        current_sha256=current_sha256,
        output_sha256=output_sha256,
        written=written,
        error_code=error_code,
    )


def _current_bound_page(
    page: KnowledgePageInput,
    current: KnowledgePage,
    *,
    rendered_at: str,
) -> KnowledgePageInput:
    """Keep F-05 immutable fields and lifecycle state on an existing draft page."""

    frontmatter = dict(page.frontmatter)
    current_frontmatter = current.frontmatter
    frontmatter.update(
        {
            "project_id": current_frontmatter.project_id,
            "artifact_type": current_frontmatter.artifact_type,
            "ownership": current_frontmatter.ownership,
            "status": current_frontmatter.status,
            "generated_at": current_frontmatter.generated_at,
            "last_verified_at": current_frontmatter.last_verified_at,
            "updated_at": _timestamp_after(rendered_at, current_frontmatter.updated_at),
        }
    )
    return KnowledgePageInput(
        path=page.path,
        artifact_type=page.artifact_type,
        title=page.title,
        frontmatter=frontmatter,
        body=page.body,
        reason_code=page.reason_code,
        completeness=page.completeness,
        intent=page.intent,
    )


def _renderer_error_code(exc: BaseException) -> str:
    """Map implementation exceptions to a bounded stable machine code."""

    name = type(exc).__name__
    known = {
        "ControlledMarkdownAuthorizationError": "authorization-rejected",
        "ControlledMarkdownWriteRejectedError": "write-rejected",
        "ControlledMarkdownWriteConflictError": "revision-conflict",
        "ControlledMarkdownCommitUnknownError": "commit-state-unknown",
        "ControlledMarkdownAuditStateError": "audit-state-invalid",
        "ControlledMarkdownPersistenceError": "persistence-failed",
        "ControlledMarkdownError": "controlled-plan-rejected",
        "KnowledgeArtifactError": "knowledge-page-invalid",
        "OSError": "knowledge-io-failed",
        "ValueError": "renderer-input-invalid",
        "TypeError": "renderer-input-invalid",
    }
    return known.get(name, "renderer-operation-failed")


def render_project_knowledge(
    workspace_root: str | Path,
    project_id: str,
    *,
    rendered_at: object | None = None,
    plan_date: object | None = None,
    rendering_data: Mapping[str, Any] | None = None,
    host_data: Mapping[str, Any] | None = None,
    machine_artifacts: Mapping[str, Mapping[str, Any] | None] | None = None,
    persist: bool = True,
    host_context: TrustedHostSessionContext | None = None,
    decision_id_prefix: str | None = None,
    decision_id: str | None = None,
    authorized_at: object | None = None,
    authorization_factory: Callable[
        [ControlledMarkdownUpdatePlan], ControlledMarkdownWriteAuthorization
    ] | None = None,
) -> KnowledgeRenderingResult:
    """Build and optionally persist the complete project knowledge package.

    Rendering is deterministic and source-read-only.  Every changed page is
    planned by F-05A and, when ``persist`` is true, published only through the
    F-05B exact-CAS writer with an explicit trusted host authorization.  Existing
    legacy/future/malformed, user-owned, and non-draft pages are reported and
    left untouched.
    """

    normalized_project_id = validate_project_id(project_id)
    timestamp = _utc_timestamp(rendered_at)
    pages = build_project_knowledge_pages(
        workspace_root,
        normalized_project_id,
        rendered_at=timestamp,
        plan_date=plan_date,
        rendering_data=rendering_data,
        host_data=host_data,
        machine_artifacts=machine_artifacts,
    )
    registration = load_registered_project(workspace_root, normalized_project_id)
    knowledge_root = registration.layout.knowledge_root

    if not isinstance(persist, bool):
        raise TypeError("persist must be a bool")
    if persist and authorization_factory is None:
        if type(host_context) is not TrustedHostSessionContext:
            raise ValueError(
                "persist=True requires an explicit TrustedHostSessionContext or authorization_factory"
            )
        if authorized_at is None:
            raise ValueError("persist=True requires an explicit authorized_at timestamp")
        if decision_id_prefix is None and decision_id is None:
            raise ValueError("persist=True requires an explicit decision_id or decision_id_prefix")
    if authorization_factory is not None and not callable(authorization_factory):
        raise TypeError("authorization_factory must be callable")

    observations: list[KnowledgePageObservation] = []
    for page in pages:
        current: bytes | None = None
        current_sha: str | None = None
        try:
            target = _knowledge_read_target(knowledge_root, page.path)
            if target.exists():
                if not target.is_file():
                    observations.append(
                        _page_observation(
                            page,
                            action="protected",
                            status="protected",
                            reason_code="knowledge-target-not-regular-file",
                        )
                    )
                    continue
                current = target.read_bytes()
                current_sha = hashlib.sha256(current).hexdigest()
        except (KnowledgeArtifactError, OSError, ValueError, TypeError) as exc:
            observations.append(
                _page_observation(
                    page,
                    action="protected",
                    status="protected",
                    reason_code="knowledge-target-unavailable",
                    error_code=_renderer_error_code(exc),
                )
            )
            continue

        parsed_current: KnowledgePage | None = None
        if current is not None:
            try:
                parsed_current = parse_knowledge_page(current, path=page.path)
            except (KnowledgeArtifactError, TypeError, ValueError, UnicodeError) as exc:
                observations.append(
                    _page_observation(
                        page,
                        action="protected",
                        status="protected",
                        reason_code=_schema_failure_reason(current),
                        current_sha256=current_sha,
                        error_code=_renderer_error_code(exc),
                    )
                )
                continue
            current_frontmatter = parsed_current.frontmatter
            if current_frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
                observations.append(
                    _page_observation(
                        page,
                        action="protected",
                        status="protected",
                        reason_code=(
                            "legacy-schema-v1-read-only"
                            if current_frontmatter.schema_version == 1
                            else "unsupported-or-future-schema"
                        ),
                        current_sha256=current_sha,
                    )
                )
                continue
            if current_frontmatter.ownership == "user":
                observations.append(
                    _page_observation(
                        page,
                        action="protected",
                        status=current_frontmatter.status,
                        reason_code="user-owned-page",
                        current_sha256=current_sha,
                    )
                )
                continue
            if current_frontmatter.status != "draft":
                observations.append(
                    _page_observation(
                        page,
                        action="protected",
                        status=current_frontmatter.status,
                        reason_code="non-draft-lifecycle-page",
                        current_sha256=current_sha,
                    )
                )
                continue
            try:
                proposed_mixed = parse_mixed_markdown_body(page.body)
                if current_frontmatter.ownership == "mixed":
                    current_mixed = parse_mixed_markdown_body(parsed_current.body)
                    if current_mixed.region_ids != proposed_mixed.region_ids:
                        observations.append(
                            _page_observation(
                                page,
                                action="protected",
                                status=current_frontmatter.status,
                                reason_code="mixed-region-contract-mismatch",
                                current_sha256=current_sha,
                            )
                        )
                        continue
            except ControlledMarkdownError:
                # Non-mixed pages are allowed to contain ordinary Markdown.  Only
                # parse mixed content when the existing page explicitly declares it.
                if current_frontmatter.ownership == "mixed":
                    observations.append(
                        _page_observation(
                            page,
                            action="protected",
                            status=current_frontmatter.status,
                            reason_code="mixed-region-contract-invalid",
                            current_sha256=current_sha,
                            error_code="mixed-region-invalid",
                        )
                    )
                    continue
            page = _current_bound_page(page, parsed_current, rendered_at=timestamp)

        try:
            proposed = page.payload
            plan = plan_controlled_markdown_update(
                path=page.path,
                current=current,
                proposed=proposed,
                intent=page.intent,
                expected_current_sha256=current_sha,
            )
            if current is not None:
                try:
                    if _semantic_page_key(current, page.path) == _semantic_page_key(plan.output_bytes, page.path):
                        observations.append(
                            _page_observation(
                                page,
                                action="unchanged",
                                status=parsed_current.frontmatter.status if parsed_current else "draft",
                                current_sha256=current_sha,
                                output_sha256=current_sha,
                            )
                        )
                        continue
                except (KnowledgeArtifactError, TypeError, ValueError, UnicodeError):
                    # The plan itself already validated the output; a defensive
                    # comparison failure should not weaken the controlled write.
                    pass
            if not persist:
                observations.append(
                    _page_observation(
                        page,
                        action="planned",
                        status="draft",
                        current_sha256=current_sha,
                        output_sha256=plan.output_sha256,
                    )
                )
                continue

            if authorization_factory is not None:
                authorization = authorization_factory(plan)
            else:
                assert host_context is not None  # validated above
                if decision_id_prefix is not None:
                    raw_decision_id = f"{decision_id_prefix}-{hashlib.sha256(page.path.encode('utf-8')).hexdigest()[:12]}"
                else:
                    assert decision_id is not None
                    raw_decision_id = f"{decision_id}-{hashlib.sha256(page.path.encode('utf-8')).hexdigest()[:12]}"
                authorization = bind_controlled_markdown_authorization(
                    plan,
                    host_context=host_context,
                    decision_id=raw_decision_id,
                    authorized_at=authorized_at,
                )
            write_result = persist_controlled_markdown_update(
                workspace_root,
                normalized_project_id,
                path=page.path,
                proposed=page.payload,
                intent=page.intent,
                expected_current_sha256=current_sha,
                authorization=authorization,
            )
            observations.append(
                _page_observation(
                    page,
                    action="written",
                    status="draft",
                    current_sha256=current_sha,
                    output_sha256=write_result.output_sha256,
                    written=True,
                )
            )
        except (ControlledMarkdownPersistenceError, ControlledMarkdownError, KnowledgeArtifactError, OSError, TypeError, ValueError) as exc:
            observations.append(
                _page_observation(
                    page,
                    action="failed",
                    status="failed",
                    current_sha256=current_sha,
                    error_code=_renderer_error_code(exc),
                )
            )

    has_failures = any(item.action == "failed" for item in observations)
    has_protected = any(item.action == "protected" for item in observations)
    status = "failed" if has_failures else "partial" if has_protected else "succeeded"
    return KnowledgeRenderingResult(
        project_id=normalized_project_id,
        rendered_at=timestamp,
        status=status,
        pages=tuple(observations),
        knowledge_root=None,
    )


# Compatibility aliases used by early hosts and the Core facade.
render_knowledge = render_project_knowledge


# Compatibility alias retained for early host adapters.
generate_knowledge_artifacts = build_project_knowledge_pages
