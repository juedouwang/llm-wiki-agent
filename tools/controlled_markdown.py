#!/usr/bin/env python3
"""Deterministic in-memory controlled Markdown update planning (F-05A).

The host Agent supplies every semantic choice and the complete proposed Knowledge
Schema v2 page.  This module only validates canonical project-relative identity,
exact caller-supplied base revision, ownership/update intent, timestamps, and
explicit protected user regions.  It performs no filesystem I/O, persistence, Source/Evidence access,
semantic synthesis, lifecycle authorization, or CLI/MCP/Web publication.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from tools.knowledge_artifacts import (
    KNOWLEDGE_SCHEMA_VERSION,
    ArtifactType,
    KnowledgeArtifactError,
    KnowledgeFrontmatter,
    KnowledgeOwnership,
    artifact_contract_for_path,
    parse_knowledge_page,
    serialize_knowledge_frontmatter,
)
from tools.project_layout import CURRENT_SCHEMA_VERSION


CONTROLLED_MARKDOWN_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
CONTROLLED_MARKDOWN_VERSION = "controlled-markdown-plan-v1"
CONTROLLED_MARKDOWN_PLAN_KIND = "llmwiki-controlled-markdown-update-plan"
MAX_KNOWLEDGE_PAGE_BYTES = 4 * 1024 * 1024
MAX_USER_REGIONS = 128

ControlledMarkdownIntent = Literal["regenerate", "user-edit"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REGION_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_START_RE = re.compile(
    r'<!-- llmwiki:user-region:start id="(?P<id>[a-z0-9][a-z0-9-]{0,63})" -->'
    r"(?:\r?\n)?\Z"
)
_END_RE = re.compile(
    r'<!-- llmwiki:user-region:end id="(?P<id>[a-z0-9][a-z0-9-]{0,63})" -->'
    r"(?:\r?\n)?\Z"
)
_RESERVED_MARKER = "<!-- llmwiki:user-region:"


class ControlledMarkdownError(KnowledgeArtifactError):
    """Body-free fail-closed planning rejection for later bounded audit mapping."""

    reason_code = "invalid-controlled-markdown"

    def as_dict(self) -> dict[str, object]:
        """Return a body-free, stable classification for a rejected plan."""

        return {
            "schema_version": CONTROLLED_MARKDOWN_SCHEMA_VERSION,
            "kind": "llmwiki-controlled-markdown-plan-rejection",
            "plan_version": CONTROLLED_MARKDOWN_VERSION,
            "outcome": "rejected",
            "reason_code": self.reason_code,
            "message": str(self),
        }


class ControlledMarkdownRevisionError(ControlledMarkdownError):
    """Raised when a proposal is not bound to its exact caller-supplied base bytes."""

    reason_code = "revision-conflict"


class ControlledMarkdownOwnershipError(ControlledMarkdownError):
    """Raised when an update intent would cross an ownership boundary."""

    reason_code = "ownership-denied"


class ControlledMarkdownRegionError(ControlledMarkdownError):
    """Raised when mixed-page protected user regions are malformed or unsafe."""

    reason_code = "protected-region-conflict"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ControlledMarkdownError(
            "controlled Markdown metadata must be canonical JSON-compatible"
        ) from exc


def _validate_payload(value: object, field_name: str) -> bytes:
    if type(value) is not bytes:
        raise ControlledMarkdownError(
            f"{field_name} must be exact bytes so strict UTF-8 can be enforced"
        )
    if len(value) > MAX_KNOWLEDGE_PAGE_BYTES:
        raise ControlledMarkdownError(
            f"{field_name} must contain at most {MAX_KNOWLEDGE_PAGE_BYTES} bytes"
        )
    return value


def _validate_intent(value: object) -> ControlledMarkdownIntent:
    if type(value) is not str or value not in {"regenerate", "user-edit"}:
        raise ControlledMarkdownError("intent must be 'regenerate' or 'user-edit'")
    return cast(ControlledMarkdownIntent, value)


def _validate_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ControlledMarkdownRevisionError(
            f"{field_name} must be exactly 64 lowercase hexadecimal digits"
        )
    return value


def _validate_immutable_string_tuple(
    value: object,
    field_name: str,
    *,
    item_pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ControlledMarkdownError(f"{field_name} must be an immutable tuple")
    items = cast(tuple[object, ...], value)
    for item in items:
        if type(item) is not str:
            raise ControlledMarkdownError(f"{field_name} entries must be exact strings")
        if item_pattern is not None and item_pattern.fullmatch(item) is None:
            raise ControlledMarkdownError(f"{field_name} contains an invalid identifier")
    return cast(tuple[str, ...], value)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def _frontmatter_changes(
    before: KnowledgeFrontmatter | None,
    after: KnowledgeFrontmatter,
) -> tuple[str, ...]:
    after_dict = after.as_dict()
    if before is None:
        return tuple(after_dict)
    before_dict = before.as_dict()
    return tuple(
        field_name
        for field_name in after_dict
        if before_dict[field_name] != after_dict[field_name]
    )


@dataclass(frozen=True)
class UserRegion:
    """One explicit protected user-owned body region."""

    region_id: str
    start_marker: str = field(repr=False)
    content: str = field(repr=False)
    end_marker: str = field(repr=False)

    def __post_init__(self) -> None:
        if _REGION_ID_RE.fullmatch(self.region_id) is None:
            raise ControlledMarkdownRegionError("user region id is invalid")
        start = _START_RE.fullmatch(self.start_marker)
        end = _END_RE.fullmatch(self.end_marker)
        if (
            start is None
            or end is None
            or start.group("id") != self.region_id
            or end.group("id") != self.region_id
        ):
            raise ControlledMarkdownRegionError(
                f"user region {self.region_id!r} markers are inconsistent"
            )
        if _RESERVED_MARKER in self.content:
            raise ControlledMarkdownRegionError(
                f"user region {self.region_id!r} contains a reserved marker"
            )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


BodyPart = str | UserRegion


@dataclass(frozen=True)
class ParsedMixedBody:
    """Validated mixed body split into generated text and protected user regions."""

    parts: tuple[BodyPart, ...]
    regions: tuple[UserRegion, ...]

    @property
    def region_ids(self) -> tuple[str, ...]:
        return tuple(region.region_id for region in self.regions)

    def render(self, replacements: dict[str, str] | None = None) -> str:
        replacement_map = replacements or {}
        chunks: list[str] = []
        for part in self.parts:
            if isinstance(part, str):
                chunks.append(part)
                continue
            chunks.extend(
                (
                    part.start_marker,
                    replacement_map.get(part.region_id, part.content),
                    part.end_marker,
                )
            )
        return "".join(chunks)

    def generated_skeleton(self) -> tuple[tuple[str, str, str], ...]:
        skeleton: list[tuple[str, str, str]] = []
        for part in self.parts:
            if isinstance(part, str):
                skeleton.append(("generated", "", part))
            else:
                skeleton.append(("user", part.region_id, part.start_marker + part.end_marker))
        return tuple(skeleton)


def _split_lf_lines(body: str) -> tuple[str, ...]:
    """Split only on literal LF while retaining exact CRLF/LF bytes after encoding.

    ``str.splitlines`` also treats Unicode separators such as U+2028 and U+2029 as
    line boundaries.  Markdown marker syntax is deliberately narrower: only LF or
    CRLF may delimit a protected-region marker.
    """

    chunks = body.split("\n")
    lines = [chunk + "\n" for chunk in chunks[:-1]]
    if chunks[-1]:
        lines.append(chunks[-1])
    return tuple(lines)


def parse_mixed_markdown_body(body: object) -> ParsedMixedBody:
    """Parse exact line-delimited protected user regions without guessing ownership.

    Markers are valid only as complete LF/CRLF-delimited lines and may not nest. All
    body text outside those regions is generated-owned. The caller must already have
    an explicit ``ownership: mixed`` page; this parser never infers page ownership
    from text. Unicode line/paragraph separators are ordinary content, not marker
    boundaries.
    """

    if not isinstance(body, str):
        raise TypeError("body must be a string")
    lines = _split_lf_lines(body)
    parts: list[BodyPart] = []
    generated: list[str] = []
    active_id: str | None = None
    active_start = ""
    active_content: list[str] = []
    regions: list[UserRegion] = []
    seen: set[str] = set()

    def flush_generated() -> None:
        if generated:
            parts.append("".join(generated))
            generated.clear()

    for line in lines:
        start = _START_RE.fullmatch(line)
        end = _END_RE.fullmatch(line)
        if start is not None:
            region_id = start.group("id")
            if active_id is not None:
                raise ControlledMarkdownRegionError(
                    f"user region {region_id!r} cannot nest inside {active_id!r}"
                )
            if region_id in seen:
                raise ControlledMarkdownRegionError(
                    f"duplicate user region id: {region_id}"
                )
            if len(seen) >= MAX_USER_REGIONS:
                raise ControlledMarkdownRegionError(
                    f"mixed body may contain at most {MAX_USER_REGIONS} user regions"
                )
            flush_generated()
            seen.add(region_id)
            active_id = region_id
            active_start = line
            active_content = []
            continue
        if end is not None:
            region_id = end.group("id")
            if active_id is None:
                raise ControlledMarkdownRegionError(
                    f"user region {region_id!r} has an end marker without a start"
                )
            if region_id != active_id:
                raise ControlledMarkdownRegionError(
                    f"user region end {region_id!r} does not match start {active_id!r}"
                )
            region = UserRegion(
                region_id=active_id,
                start_marker=active_start,
                content="".join(active_content),
                end_marker=line,
            )
            regions.append(region)
            parts.append(region)
            active_id = None
            active_start = ""
            active_content = []
            continue
        if _RESERVED_MARKER in line:
            raise ControlledMarkdownRegionError(
                "reserved user-region marker must be a complete valid marker line"
            )
        if active_id is None:
            generated.append(line)
        else:
            active_content.append(line)

    if active_id is not None:
        raise ControlledMarkdownRegionError(
            f"user region {active_id!r} is missing its end marker"
        )
    flush_generated()
    if not regions:
        raise ControlledMarkdownRegionError(
            "ownership 'mixed' requires at least one explicit protected user region"
        )
    return ParsedMixedBody(parts=tuple(parts), regions=tuple(regions))


def _reject_reserved_markers(body: str, ownership: KnowledgeOwnership) -> None:
    if _RESERVED_MARKER in body:
        raise ControlledMarkdownRegionError(
            f"ownership {ownership!r} must not contain reserved mixed-page markers"
        )


def _validate_ownership_intent(
    ownership: KnowledgeOwnership,
    intent: ControlledMarkdownIntent,
    *,
    creating: bool,
) -> None:
    if ownership == "generated" and intent != "regenerate":
        raise ControlledMarkdownOwnershipError(
            "ownership 'generated' accepts only intent 'regenerate'"
        )
    if ownership == "user" and intent != "user-edit":
        raise ControlledMarkdownOwnershipError(
            "ownership 'user' accepts only intent 'user-edit'"
        )
    if creating and ownership == "mixed" and intent != "regenerate":
        raise ControlledMarkdownOwnershipError(
            "a new mixed page must be created by 'regenerate' with empty user regions"
        )


def _validate_existing_frontmatter(
    current: KnowledgeFrontmatter,
    proposed: KnowledgeFrontmatter,
) -> None:
    immutable_fields = ("project_id", "artifact_type", "ownership", "generated_at")
    changed = [
        field_name
        for field_name in immutable_fields
        if getattr(current, field_name) != getattr(proposed, field_name)
    ]
    if changed:
        raise ControlledMarkdownOwnershipError(
            "controlled update cannot change immutable page fields: "
            + ", ".join(changed)
        )
    if _timestamp(proposed.updated_at) <= _timestamp(current.updated_at):
        raise ControlledMarkdownRevisionError(
            "proposed updated_at must advance strictly beyond the current page"
        )


def _merge_body(
    *,
    ownership: KnowledgeOwnership,
    intent: ControlledMarkdownIntent,
    current_body: str | None,
    proposed_body: str,
) -> tuple[
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    bool,
    bool,
]:
    creating = current_body is None
    if ownership != "mixed":
        _reject_reserved_markers(proposed_body, ownership)
        if current_body is not None:
            _reject_reserved_markers(current_body, ownership)
        body_changed = current_body != proposed_body
        return (
            proposed_body,
            (),
            (),
            (),
            (),
            ownership == "generated" and body_changed,
            ownership == "user" and body_changed,
        )

    proposed = parse_mixed_markdown_body(proposed_body)
    region_ids = proposed.region_ids
    if creating:
        non_empty = [
            region.region_id for region in proposed.regions if region.content.strip()
        ]
        if non_empty:
            raise ControlledMarkdownOwnershipError(
                "a generator cannot create user-confirmed content; new mixed-page user "
                "regions must be empty: "
                + ", ".join(non_empty)
            )
        return proposed_body, region_ids, (), (), (), True, False

    assert current_body is not None
    current = parse_mixed_markdown_body(current_body)
    if current.region_ids != proposed.region_ids:
        raise ControlledMarkdownRegionError(
            "an ordinary mixed-page update must preserve user region IDs and order"
        )

    current_content = {region.region_id: region.content for region in current.regions}
    if intent == "regenerate":
        proposed_content = {
            region.region_id: region.content for region in proposed.regions
        }
        discarded_ids = tuple(
            region_id
            for region_id in region_ids
            if proposed_content[region_id] != current_content[region_id]
        )
        output = proposed.render(current_content)
        return (
            output,
            region_ids,
            region_ids,
            (),
            discarded_ids,
            current.generated_skeleton() != proposed.generated_skeleton(),
            False,
        )

    if current.generated_skeleton() != proposed.generated_skeleton():
        raise ControlledMarkdownOwnershipError(
            "intent 'user-edit' cannot change generated-owned mixed-page content or "
            "region markers"
        )
    changed_ids = tuple(
        region.region_id
        for region in proposed.regions
        if current_content[region.region_id] != region.content
    )
    return proposed_body, region_ids, (), changed_ids, (), False, bool(changed_ids)


def _plan_id_material(
    *,
    path: str,
    project_id: str,
    artifact_type: ArtifactType,
    ownership: KnowledgeOwnership,
    intent: ControlledMarkdownIntent,
    current_sha256: str | None,
    proposed_sha256: str,
    output_sha256: str,
    updated_at: str,
    user_region_ids: tuple[str, ...],
    preserved_user_region_ids: tuple[str, ...],
    changed_user_region_ids: tuple[str, ...],
    discarded_proposed_user_region_ids: tuple[str, ...],
    changed_frontmatter_fields: tuple[str, ...],
    generated_body_changed: bool,
    user_body_changed: bool,
) -> dict[str, Any]:
    return {
        "identity_version": CONTROLLED_MARKDOWN_VERSION,
        "path": path,
        "project_id": project_id,
        "artifact_type": artifact_type,
        "ownership": ownership,
        "intent": intent,
        "current_sha256": current_sha256,
        "proposed_sha256": proposed_sha256,
        "output_sha256": output_sha256,
        "updated_at": updated_at,
        "user_region_ids": list(user_region_ids),
        "preserved_user_region_ids": list(preserved_user_region_ids),
        "changed_user_region_ids": list(changed_user_region_ids),
        "discarded_proposed_user_region_ids": list(
            discarded_proposed_user_region_ids
        ),
        "changed_frontmatter_fields": list(changed_frontmatter_fields),
        "generated_body_changed": generated_body_changed,
        "user_body_changed": user_body_changed,
    }


@dataclass(frozen=True)
class ControlledMarkdownUpdatePlan:
    """One complete, deterministic, no-I/O controlled update plan."""

    schema_version: int
    kind: str
    plan_version: str
    plan_id: str
    path: str
    project_id: str
    artifact_type: ArtifactType
    ownership: KnowledgeOwnership
    intent: ControlledMarkdownIntent
    current_sha256: str | None
    proposed_sha256: str
    output_sha256: str
    output_bytes: bytes = field(repr=False)
    updated_at: str
    user_region_ids: tuple[str, ...]
    preserved_user_region_ids: tuple[str, ...]
    changed_user_region_ids: tuple[str, ...]
    discarded_proposed_user_region_ids: tuple[str, ...]
    changed_frontmatter_fields: tuple[str, ...]
    generated_body_changed: bool
    user_body_changed: bool
    validation_complete: bool = True

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int:
            raise ControlledMarkdownError("schema_version must be an exact integer")
        for field_name in (
            "kind",
            "plan_version",
            "plan_id",
            "path",
            "project_id",
            "artifact_type",
            "ownership",
            "intent",
            "updated_at",
        ):
            if type(getattr(self, field_name)) is not str:
                raise ControlledMarkdownError(f"{field_name} must be an exact string")
        for field_name in (
            "generated_body_changed",
            "user_body_changed",
            "validation_complete",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ControlledMarkdownError(f"{field_name} must be an exact boolean")
        _validate_immutable_string_tuple(
            self.user_region_ids,
            "user_region_ids",
            item_pattern=_REGION_ID_RE,
        )
        _validate_immutable_string_tuple(
            self.preserved_user_region_ids,
            "preserved_user_region_ids",
            item_pattern=_REGION_ID_RE,
        )
        _validate_immutable_string_tuple(
            self.changed_user_region_ids,
            "changed_user_region_ids",
            item_pattern=_REGION_ID_RE,
        )
        _validate_immutable_string_tuple(
            self.discarded_proposed_user_region_ids,
            "discarded_proposed_user_region_ids",
            item_pattern=_REGION_ID_RE,
        )
        _validate_immutable_string_tuple(
            self.changed_frontmatter_fields,
            "changed_frontmatter_fields",
        )
        if self.schema_version != CONTROLLED_MARKDOWN_SCHEMA_VERSION:
            raise ControlledMarkdownError("controlled Markdown schema_version must be 1")
        if self.kind != CONTROLLED_MARKDOWN_PLAN_KIND:
            raise ControlledMarkdownError(
                f"kind must be {CONTROLLED_MARKDOWN_PLAN_KIND!r}"
            )
        if self.plan_version != CONTROLLED_MARKDOWN_VERSION:
            raise ControlledMarkdownError(
                f"plan_version must be {CONTROLLED_MARKDOWN_VERSION!r}"
            )
        if not self.validation_complete:
            raise ControlledMarkdownError("a successful update plan must be complete")
        if not self.plan_id.startswith("cmp-") or _SHA256_RE.fullmatch(
            self.plan_id[4:]
        ) is None:
            raise ControlledMarkdownError("plan_id must use 'cmp-' plus 64 lowercase hex")
        contract = artifact_contract_for_path(self.path)
        if contract.artifact_type != self.artifact_type:
            raise ControlledMarkdownError("plan artifact_type does not match path")
        if self.current_sha256 is not None:
            _validate_sha256(self.current_sha256, "current_sha256")
        _validate_sha256(self.proposed_sha256, "proposed_sha256")
        _validate_sha256(self.output_sha256, "output_sha256")
        output = _validate_payload(self.output_bytes, "output_bytes")
        if _sha256(output) != self.output_sha256:
            raise ControlledMarkdownError("output_bytes do not match output_sha256")
        parsed = parse_knowledge_page(output, path=self.path)
        if parsed.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ControlledMarkdownError("output page must use current Knowledge Schema v2")
        frontmatter_field_order = tuple(parsed.frontmatter.as_dict())
        field_positions = {
            field_name: index for index, field_name in enumerate(frontmatter_field_order)
        }
        if any(
            field_name not in field_positions
            for field_name in self.changed_frontmatter_fields
        ):
            raise ControlledMarkdownError(
                "changed_frontmatter_fields contains an unknown field"
            )
        if len(set(self.changed_frontmatter_fields)) != len(
            self.changed_frontmatter_fields
        ):
            raise ControlledMarkdownError(
                "changed_frontmatter_fields must be duplicate-free"
            )
        if tuple(
            sorted(
                self.changed_frontmatter_fields,
                key=field_positions.__getitem__,
            )
        ) != self.changed_frontmatter_fields:
            raise ControlledMarkdownError(
                "changed_frontmatter_fields must use canonical frontmatter order"
            )
        if (
            self.current_sha256 is None
            and self.changed_frontmatter_fields != frontmatter_field_order
        ):
            raise ControlledMarkdownError(
                "creation must report every canonical frontmatter field as changed"
            )
        _validate_ownership_intent(
            self.ownership,
            self.intent,
            creating=self.current_sha256 is None,
        )
        if self.ownership == "mixed":
            mixed = parse_mixed_markdown_body(parsed.body)
            if mixed.region_ids != self.user_region_ids:
                raise ControlledMarkdownError(
                    "output mixed-page regions do not match user_region_ids"
                )
            if self.current_sha256 is None and any(
                region.content.strip() for region in mixed.regions
            ):
                raise ControlledMarkdownError(
                    "a new mixed plan cannot create user-confirmed content"
                )
            if self.intent == "regenerate":
                expected_preserved = (
                    () if self.current_sha256 is None else self.user_region_ids
                )
                if self.preserved_user_region_ids != expected_preserved:
                    raise ControlledMarkdownError(
                        "mixed regeneration preserved_user_region_ids are inconsistent"
                    )
                if self.user_body_changed:
                    raise ControlledMarkdownError(
                        "mixed regeneration cannot report a user-body change"
                    )
            elif self.generated_body_changed or self.preserved_user_region_ids:
                raise ControlledMarkdownError(
                    "mixed user-edit cannot change generated body or claim preservation"
                )
        else:
            _reject_reserved_markers(parsed.body, self.ownership)
            if self.user_region_ids or self.preserved_user_region_ids:
                raise ControlledMarkdownError(
                    "non-mixed plans cannot declare user regions"
                )
            if self.ownership == "generated" and self.user_body_changed:
                raise ControlledMarkdownError(
                    "generated ownership cannot report a user-body change"
                )
            if self.ownership == "user" and self.generated_body_changed:
                raise ControlledMarkdownError(
                    "user ownership cannot report a generated-body change"
                )
        if (
            parsed.frontmatter.project_id != self.project_id
            or parsed.frontmatter.artifact_type != self.artifact_type
            or parsed.frontmatter.ownership != self.ownership
            or parsed.frontmatter.updated_at != self.updated_at
        ):
            raise ControlledMarkdownError("output page metadata does not match plan")
        if len(set(self.user_region_ids)) != len(self.user_region_ids):
            raise ControlledMarkdownError("user_region_ids must be duplicate-free")
        if len(set(self.preserved_user_region_ids)) != len(
            self.preserved_user_region_ids
        ):
            raise ControlledMarkdownError(
                "preserved_user_region_ids must be duplicate-free"
            )
        if len(set(self.changed_user_region_ids)) != len(self.changed_user_region_ids):
            raise ControlledMarkdownError(
                "changed_user_region_ids must be duplicate-free"
            )
        if len(set(self.discarded_proposed_user_region_ids)) != len(
            self.discarded_proposed_user_region_ids
        ):
            raise ControlledMarkdownError(
                "discarded_proposed_user_region_ids must be duplicate-free"
            )
        if not set(self.preserved_user_region_ids).issubset(self.user_region_ids):
            raise ControlledMarkdownError(
                "preserved_user_region_ids must be a subset of user_region_ids"
            )
        for field_name, values in (
            ("changed_user_region_ids", self.changed_user_region_ids),
            (
                "discarded_proposed_user_region_ids",
                self.discarded_proposed_user_region_ids,
            ),
        ):
            if not set(values).issubset(self.user_region_ids):
                raise ControlledMarkdownError(
                    f"{field_name} must be a subset of user_region_ids"
                )
        if self.intent == "regenerate" and self.changed_user_region_ids:
            raise ControlledMarkdownError(
                "regeneration cannot report changed user-region content"
            )
        if self.intent == "user-edit" and self.discarded_proposed_user_region_ids:
            raise ControlledMarkdownError(
                "user-edit cannot report discarded generated user-region content"
            )
        expected = _plan_id_material(
            path=self.path,
            project_id=self.project_id,
            artifact_type=self.artifact_type,
            ownership=self.ownership,
            intent=self.intent,
            current_sha256=self.current_sha256,
            proposed_sha256=self.proposed_sha256,
            output_sha256=self.output_sha256,
            updated_at=self.updated_at,
            user_region_ids=self.user_region_ids,
            preserved_user_region_ids=self.preserved_user_region_ids,
            changed_user_region_ids=self.changed_user_region_ids,
            discarded_proposed_user_region_ids=self.discarded_proposed_user_region_ids,
            changed_frontmatter_fields=self.changed_frontmatter_fields,
            generated_body_changed=self.generated_body_changed,
            user_body_changed=self.user_body_changed,
        )
        expected_id = "cmp-" + hashlib.sha256(_canonical_json(expected)).hexdigest()
        if self.plan_id != expected_id:
            raise ControlledMarkdownError("plan_id does not match the declared update")

    @property
    def output_byte_count(self) -> int:
        return len(self.output_bytes)

    @property
    def created(self) -> bool:
        return self.current_sha256 is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "plan_version": self.plan_version,
            "plan_id": self.plan_id,
            "path": self.path,
            "project_id": self.project_id,
            "artifact_type": self.artifact_type,
            "ownership": self.ownership,
            "intent": self.intent,
            "created": self.created,
            "current_sha256": self.current_sha256,
            "proposed_sha256": self.proposed_sha256,
            "output_sha256": self.output_sha256,
            "output_byte_count": self.output_byte_count,
            "updated_at": self.updated_at,
            "user_region_ids": list(self.user_region_ids),
            "preserved_user_region_ids": list(self.preserved_user_region_ids),
            "changed_user_region_ids": list(self.changed_user_region_ids),
            "discarded_proposed_user_region_ids": list(
                self.discarded_proposed_user_region_ids
            ),
            "changed_frontmatter_fields": list(self.changed_frontmatter_fields),
            "generated_body_changed": self.generated_body_changed,
            "user_body_changed": self.user_body_changed,
            "validation_complete": self.validation_complete,
        }


def _plan_controlled_markdown_update(
    *,
    path: object,
    current: object | None,
    proposed: object,
    intent: object,
    expected_current_sha256: object | None,
) -> ControlledMarkdownUpdatePlan:
    """Plan one ownership-safe Knowledge Schema v2 update without writing anything.

    ``current`` is ``None`` only for creation. Existing updates require an exact
    lowercase SHA-256 precondition over the caller-supplied base bytes. The complete
    proposed page and all semantic choices come from the host. Core canonicalizes
    frontmatter, preserves protected user regions, and returns output bytes plus an
    auditable deterministic plan. This does not prove that the base is still current
    in the filesystem; F-05B must perform a stable exact-CAS recheck before writing.

    """

    contract = artifact_contract_for_path(path)
    normalized_path = contract.path
    normalized_intent = _validate_intent(intent)
    proposed_bytes = _validate_payload(proposed, "proposed")
    proposed_page = parse_knowledge_page(proposed_bytes, path=normalized_path)
    if proposed_page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
        raise ControlledMarkdownError(
            "Schema v1 knowledge pages are strict read-only compatibility and cannot "
            "be proposed for a controlled write"
        )
    proposed_frontmatter = proposed_page.frontmatter

    current_bytes: bytes | None
    current_frontmatter: KnowledgeFrontmatter | None
    current_body: str | None
    current_sha256: str | None
    if current is None:
        if expected_current_sha256 is not None:
            raise ControlledMarkdownRevisionError(
                "creation requires expected_current_sha256 to be null"
            )
        current_bytes = None
        current_frontmatter = None
        current_body = None
        current_sha256 = None
    else:
        current_bytes = _validate_payload(current, "current")
        if expected_current_sha256 is None:
            raise ControlledMarkdownRevisionError(
                "an existing update requires expected_current_sha256"
            )
        expected = _validate_sha256(
            expected_current_sha256,
            "expected_current_sha256",
        )
        current_sha256 = _sha256(current_bytes)
        if expected != current_sha256:
            raise ControlledMarkdownRevisionError(
                "expected_current_sha256 does not match the exact caller-supplied "
                "current bytes"
            )
        current_page = parse_knowledge_page(current_bytes, path=normalized_path)
        if current_page.frontmatter.schema_version != KNOWLEDGE_SCHEMA_VERSION:
            raise ControlledMarkdownError(
                "Schema v1 knowledge pages are read-only and require explicit migration"
            )
        current_frontmatter = current_page.frontmatter
        current_body = current_page.body
        _validate_existing_frontmatter(current_frontmatter, proposed_frontmatter)

    _validate_ownership_intent(
        proposed_frontmatter.ownership,
        normalized_intent,
        creating=current_frontmatter is None,
    )
    (
        output_body,
        region_ids,
        preserved_ids,
        changed_user_ids,
        discarded_user_ids,
        generated_changed,
        user_changed,
    ) = _merge_body(
        ownership=proposed_frontmatter.ownership,
        intent=normalized_intent,
        current_body=current_body,
        proposed_body=proposed_page.body,
    )
    output_bytes = (
        serialize_knowledge_frontmatter(proposed_frontmatter, path=normalized_path)
        + output_body
    ).encode("utf-8")
    if len(output_bytes) > MAX_KNOWLEDGE_PAGE_BYTES:
        raise ControlledMarkdownError(
            f"controlled output must contain at most {MAX_KNOWLEDGE_PAGE_BYTES} bytes"
        )

    proposed_sha256 = _sha256(proposed_bytes)
    output_sha256 = _sha256(output_bytes)
    changed_fields = _frontmatter_changes(current_frontmatter, proposed_frontmatter)
    material = _plan_id_material(
        path=normalized_path,
        project_id=proposed_frontmatter.project_id,
        artifact_type=proposed_frontmatter.artifact_type,
        ownership=proposed_frontmatter.ownership,
        intent=normalized_intent,
        current_sha256=current_sha256,
        proposed_sha256=proposed_sha256,
        output_sha256=output_sha256,
        updated_at=proposed_frontmatter.updated_at,
        user_region_ids=region_ids,
        preserved_user_region_ids=preserved_ids,
        changed_user_region_ids=changed_user_ids,
        discarded_proposed_user_region_ids=discarded_user_ids,
        changed_frontmatter_fields=changed_fields,
        generated_body_changed=generated_changed,
        user_body_changed=user_changed,
    )
    plan_id = "cmp-" + hashlib.sha256(_canonical_json(material)).hexdigest()
    return ControlledMarkdownUpdatePlan(
        schema_version=CONTROLLED_MARKDOWN_SCHEMA_VERSION,
        kind=CONTROLLED_MARKDOWN_PLAN_KIND,
        plan_version=CONTROLLED_MARKDOWN_VERSION,
        plan_id=plan_id,
        path=normalized_path,
        project_id=proposed_frontmatter.project_id,
        artifact_type=proposed_frontmatter.artifact_type,
        ownership=proposed_frontmatter.ownership,
        intent=normalized_intent,
        current_sha256=current_sha256,
        proposed_sha256=proposed_sha256,
        output_sha256=output_sha256,
        output_bytes=output_bytes,
        updated_at=proposed_frontmatter.updated_at,
        user_region_ids=region_ids,
        preserved_user_region_ids=preserved_ids,
        changed_user_region_ids=changed_user_ids,
        discarded_proposed_user_region_ids=discarded_user_ids,
        changed_frontmatter_fields=changed_fields,
        generated_body_changed=generated_changed,
        user_body_changed=user_changed,
    )


def plan_controlled_markdown_update(
    *,
    path: object,
    current: object | None,
    proposed: object,
    intent: object,
    expected_current_sha256: object | None,
) -> ControlledMarkdownUpdatePlan:
    """Return one body-safe plan with a body-free rejection contract.

    Structural path/frontmatter failures are normalized to
    ``ControlledMarkdownError`` so a later writer can persist a bounded reason code
    without copying caller Markdown, YAML values, or an underlying parser message.
    """

    try:
        return _plan_controlled_markdown_update(
            path=path,
            current=current,
            proposed=proposed,
            intent=intent,
            expected_current_sha256=expected_current_sha256,
        )
    except ControlledMarkdownError:
        raise
    except KnowledgeArtifactError as exc:
        raise ControlledMarkdownError(
            "controlled Markdown path or frontmatter is structurally invalid"
        ) from exc
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ControlledMarkdownError(
            "controlled Markdown input is structurally invalid"
        ) from exc
