#!/usr/bin/env python3
"""Bounded controlled-Markdown persistence with exact revision/audit binding (F-05B).

The embedding host supplies the complete proposed Knowledge Schema v2 page and a
structured, host-attested authorization bound to an F-05A plan.  Core reloads the
registered project, freshly reads the live knowledge page, independently recomputes
the plan, verifies the exact authorization binding, records a body-free prepared
audit record, and then performs one exact-revision atomic publication.

This module does not decide research semantics, authenticate an operating-system
principal, synthesize Markdown, register Evidence, read research-source content,
send data externally, or expose CLI/MCP/Web/Hook operations.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, cast

from tools.controlled_markdown import (
    CONTROLLED_MARKDOWN_VERSION,
    ControlledMarkdownError,
    ControlledMarkdownIntent,
    ControlledMarkdownUpdatePlan,
    plan_controlled_markdown_update,
)
from tools.knowledge_artifacts import (
    KnowledgeArtifactError,
    artifact_contract_for_path,
)
from tools.project_layout import (
    CURRENT_SCHEMA_VERSION,
    LayoutError,
    parse_json_bytes_strict,
    validate_project_id,
)
from tools.project_registry import (
    ProjectRegistrationResult,
    load_registered_project,
)
from tools.stable_file_access import (
    StableDirectoryLease,
    StableFileAccessError,
    StableFileCommitUnknownError,
    StableFileMissingError,
    StableFileRevisionConflictError,
    StableFileWriteResult,
    compare_and_swap_atomic_stable_file,
    exclusive_stable_file_lock,
    lease_stable_directory,
    read_stable_regular_file,
)


CONTROLLED_MARKDOWN_PERSISTENCE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
CONTROLLED_MARKDOWN_PERSISTENCE_VERSION = "controlled-markdown-persistence-v1"
CONTROLLED_MARKDOWN_PERSISTENCE_KIND = "llmwiki-controlled-markdown-write-result"
CONTROLLED_MARKDOWN_AUTHORIZATION_VERSION = "controlled-markdown-authorization-v1"
CONTROLLED_MARKDOWN_AUTHORIZATION_KIND = "llmwiki-controlled-markdown-write-authorization"
CONTROLLED_MARKDOWN_AUDIT_VERSION = "controlled-markdown-audit-v1"
CONTROLLED_MARKDOWN_AUDIT_KIND = "llmwiki-controlled-markdown-audit-record"
CONTROLLED_MARKDOWN_AUDIT_FILE = "controlled-markdown-audit.jsonl"
MAX_CONTROLLED_MARKDOWN_AUDIT_BYTES = 8 * 1024 * 1024
MAX_CONTROLLED_MARKDOWN_AUDIT_RECORD_BYTES = 16 * 1024
MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS = 50_000
LOCK_TIMEOUT_SECONDS = 30.0

ActorType = Literal["user", "host-agent"]
AuditPhase = Literal[
    "prepared",
    "committed",
    "conflict",
    "failed",
    "commit-unknown",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PLAN_ID_RE = re.compile(r"cmp-[0-9a-f]{64}")
_AUTHORIZATION_ID_RE = re.compile(r"cmwa-[0-9a-f]{64}")
_TRANSACTION_ID_RE = re.compile(r"cmtx-[0-9a-f]{64}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}")
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,95}")
_REGION_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_AUDIT_FRONTMATTER_FIELDS = (
    "schema_version",
    "kind",
    "project_id",
    "artifact_type",
    "title",
    "status",
    "ownership",
    "source_ids",
    "evidence_refs",
    "generated_at",
    "updated_at",
    "last_verified_at",
)


class ControlledMarkdownPersistenceError(Exception):
    """Base body-free F-05B failure."""

    reason_code = "controlled-markdown-persistence-error"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": CONTROLLED_MARKDOWN_PERSISTENCE_SCHEMA_VERSION,
            "kind": "llmwiki-controlled-markdown-write-rejection",
            "persistence_version": CONTROLLED_MARKDOWN_PERSISTENCE_VERSION,
            "outcome": "rejected",
            "reason_code": self.reason_code,
            "message": str(self),
        }


class ControlledMarkdownAuthorizationError(ControlledMarkdownPersistenceError):
    reason_code = "authorization-invalid"


class ControlledMarkdownAuthorizationMismatchError(
    ControlledMarkdownAuthorizationError
):
    reason_code = "authorization-plan-mismatch"


class ControlledMarkdownAuthorizationReuseError(ControlledMarkdownAuthorizationError):
    reason_code = "authorization-reused"


class ControlledMarkdownAuditStateError(ControlledMarkdownPersistenceError):
    reason_code = "audit-state-invalid"


class ControlledMarkdownAuditWriteError(ControlledMarkdownPersistenceError):
    reason_code = "audit-write-failed"


class ControlledMarkdownRegistrationChangedError(ControlledMarkdownPersistenceError):
    reason_code = "registration-changed"


class ControlledMarkdownWriteConflictError(ControlledMarkdownPersistenceError):
    reason_code = "revision-conflict"

    def __init__(
        self,
        message: str,
        *,
        expected_current_sha256: str | None,
        observed_current_sha256: str | None,
        transaction_id: str | None,
    ) -> None:
        super().__init__(message)
        self.expected_current_sha256 = expected_current_sha256
        self.observed_current_sha256 = observed_current_sha256
        self.transaction_id = transaction_id

    def as_dict(self) -> dict[str, object]:
        result = super().as_dict()
        result.update(
            {
                "transaction_id": self.transaction_id,
                "page_commit_state": "not-committed",
                "audit_commit_state": (
                    "committed" if self.transaction_id is not None else "not-started"
                ),
                "expected_current_sha256": self.expected_current_sha256,
                "observed_current_sha256": self.observed_current_sha256,
            }
        )
        return result


class ControlledMarkdownWriteRejectedError(ControlledMarkdownPersistenceError):
    reason_code = "write-rejected"


class ControlledMarkdownWriteFailedError(ControlledMarkdownPersistenceError):
    """A prepared transaction failed before the page publication was known committed."""

    reason_code = "write-failed"

    def __init__(
        self,
        message: str,
        *,
        transaction_id: str,
        observed_after_sha256: str | None,
    ) -> None:
        super().__init__(message)
        self.transaction_id = transaction_id
        self.observed_after_sha256 = observed_after_sha256

    def as_dict(self) -> dict[str, object]:
        result = super().as_dict()
        result.update(
            {
                "transaction_id": self.transaction_id,
                "page_commit_state": "not-committed",
                "audit_commit_state": "committed",
                "observed_after_sha256": self.observed_after_sha256,
            }
        )
        return result


class ControlledMarkdownCommitUnknownError(ControlledMarkdownPersistenceError):
    reason_code = "commit-state-unknown"

    def __init__(self, message: str, *, transaction_id: str) -> None:
        super().__init__(message)
        self.transaction_id = transaction_id

    def as_dict(self) -> dict[str, object]:
        result = super().as_dict()
        result.update(
            {
                "transaction_id": self.transaction_id,
                "page_commit_state": "unknown",
                "audit_commit_state": "committed",
            }
        )
        return result


class ControlledMarkdownCommitAuditUnknownError(ControlledMarkdownPersistenceError):
    reason_code = "commit-audit-state-unknown"

    def __init__(self, message: str, *, transaction_id: str) -> None:
        super().__init__(message)
        self.transaction_id = transaction_id

    def as_dict(self) -> dict[str, object]:
        result = super().as_dict()
        result.update(
            {
                "transaction_id": self.transaction_id,
                "page_commit_state": "committed-or-unknown",
                "audit_commit_state": "unknown",
            }
        )
        return result


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
        raise ControlledMarkdownPersistenceError(
            "controlled Markdown persistence metadata is not canonical JSON"
        ) from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: object, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ControlledMarkdownPersistenceError(
            f"{field_name} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _validate_token(value: object, field_name: str) -> str:
    if type(value) is not str or _TOKEN_RE.fullmatch(value) is None:
        raise ControlledMarkdownAuthorizationError(
            f"{field_name} must be a bounded opaque token"
        )
    return value


def _validate_utc_timestamp(value: object, field_name: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ControlledMarkdownAuthorizationError(
            f"{field_name} must be a canonical UTC RFC3339 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ControlledMarkdownAuthorizationError(
            f"{field_name} must be a canonical UTC RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ControlledMarkdownAuthorizationError(
            f"{field_name} must be a canonical UTC RFC3339 timestamp"
        )
    normalized = parsed.astimezone(timezone.utc)
    canonical = normalized.isoformat(
        timespec="microseconds" if normalized.microsecond else "seconds"
    ).replace("+00:00", "Z")
    if canonical != value:
        raise ControlledMarkdownAuthorizationError(
            f"{field_name} must be a canonical UTC RFC3339 timestamp"
        )
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _validate_lock_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ControlledMarkdownWriteRejectedError(
            "lock_timeout_seconds must be a positive finite number"
        )
    return float(value)


def _validate_bounded_string_list(
    value: object,
    field_name: str,
    *,
    pattern: re.Pattern[str],
    max_items: int = 128,
) -> tuple[str, ...]:
    if type(value) is not list or len(value) > max_items:
        raise ControlledMarkdownAuditStateError(
            f"audit {field_name} must be a bounded JSON array"
        )
    normalized: list[str] = []
    for item in value:
        if type(item) is not str or pattern.fullmatch(item) is None:
            raise ControlledMarkdownAuditStateError(
                f"audit {field_name} contains an invalid string"
            )
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise ControlledMarkdownAuditStateError(
            f"audit {field_name} must be duplicate-free"
        )
    return tuple(normalized)


@dataclass(frozen=True)
class TrustedHostSessionContext:
    """Closed host-attested attribution supplied by the embedding adapter.

    Core validates this structure but does not claim to authenticate an OS account or
    cryptographic principal.  The embedding host is responsible for constructing it
    from its trusted session boundary rather than caller-controlled Markdown or Hook
    payloads.
    """

    host_id: str
    actor_type: ActorType
    actor_id: str
    session_id: str

    def __post_init__(self) -> None:
        _validate_token(self.host_id, "host_id")
        if type(self.actor_type) is not str or self.actor_type not in {
            "user",
            "host-agent",
        }:
            raise ControlledMarkdownAuthorizationError(
                "actor_type must be either user or host-agent"
            )
        _validate_token(self.actor_id, "actor_id")
        _validate_token(self.session_id, "session_id")

    def as_dict(self) -> dict[str, str]:
        return {
            "host_id": self.host_id,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
        }


def _authorization_material(
    *,
    plan_id: str,
    project_id: str,
    path: str,
    intent: ControlledMarkdownIntent,
    current_sha256: str | None,
    proposed_sha256: str,
    output_sha256: str,
    host_context: TrustedHostSessionContext,
    decision_id: str,
    authorized_at: str,
) -> dict[str, object]:
    return {
        "authorization_version": CONTROLLED_MARKDOWN_AUTHORIZATION_VERSION,
        "decision": "authorized",
        "plan_id": plan_id,
        "project_id": project_id,
        "path": path,
        "intent": intent,
        "current_sha256": current_sha256,
        "proposed_sha256": proposed_sha256,
        "output_sha256": output_sha256,
        "host_context": host_context.as_dict(),
        "decision_id": decision_id,
        "authorized_at": authorized_at,
    }


@dataclass(frozen=True)
class ControlledMarkdownWriteAuthorization:
    """One host decision bound to an exact F-05A plan revision."""

    schema_version: int
    kind: str
    authorization_version: str
    authorization_id: str
    decision: str
    plan_id: str
    project_id: str
    path: str
    intent: ControlledMarkdownIntent
    current_sha256: str | None
    proposed_sha256: str
    output_sha256: str
    host_context: TrustedHostSessionContext
    decision_id: str
    authorized_at: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ControlledMarkdownAuthorizationError(
                "authorization schema_version must be exact integer 1"
            )
        for field_name in (
            "kind",
            "authorization_version",
            "authorization_id",
            "decision",
            "plan_id",
            "project_id",
            "path",
            "intent",
            "proposed_sha256",
            "output_sha256",
            "decision_id",
            "authorized_at",
        ):
            if type(getattr(self, field_name)) is not str:
                raise ControlledMarkdownAuthorizationError(
                    f"{field_name} must be an exact string"
                )
        if self.kind != CONTROLLED_MARKDOWN_AUTHORIZATION_KIND:
            raise ControlledMarkdownAuthorizationError("authorization kind is invalid")
        if self.authorization_version != CONTROLLED_MARKDOWN_AUTHORIZATION_VERSION:
            raise ControlledMarkdownAuthorizationError(
                "authorization version is unsupported"
            )
        if self.decision != "authorized":
            raise ControlledMarkdownAuthorizationError(
                "only an explicit authorized decision may be bound"
            )
        if _AUTHORIZATION_ID_RE.fullmatch(self.authorization_id) is None:
            raise ControlledMarkdownAuthorizationError("authorization_id is invalid")
        if _PLAN_ID_RE.fullmatch(self.plan_id) is None:
            raise ControlledMarkdownAuthorizationError("plan_id is invalid")
        validate_project_id(self.project_id)
        contract = artifact_contract_for_path(self.path)
        if contract.path != self.path:
            raise ControlledMarkdownAuthorizationError("authorization path is not canonical")
        if self.intent not in {"regenerate", "user-edit"}:
            raise ControlledMarkdownAuthorizationError("authorization intent is invalid")
        _validate_sha256(self.current_sha256, "current_sha256", optional=True)
        _validate_sha256(self.proposed_sha256, "proposed_sha256")
        _validate_sha256(self.output_sha256, "output_sha256")
        if type(self.host_context) is not TrustedHostSessionContext:
            raise ControlledMarkdownAuthorizationError(
                "host_context must be a TrustedHostSessionContext"
            )
        _validate_token(self.decision_id, "decision_id")
        _validate_utc_timestamp(self.authorized_at, "authorized_at")
        expected = "cmwa-" + _sha256(
            _canonical_json(
                _authorization_material(
                    plan_id=self.plan_id,
                    project_id=self.project_id,
                    path=self.path,
                    intent=self.intent,
                    current_sha256=self.current_sha256,
                    proposed_sha256=self.proposed_sha256,
                    output_sha256=self.output_sha256,
                    host_context=self.host_context,
                    decision_id=self.decision_id,
                    authorized_at=self.authorized_at,
                )
            )
        )
        if self.authorization_id != expected:
            raise ControlledMarkdownAuthorizationError(
                "authorization_id does not match the exact host decision binding"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "authorization_version": self.authorization_version,
            "authorization_id": self.authorization_id,
            "decision": self.decision,
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "path": self.path,
            "intent": self.intent,
            "current_sha256": self.current_sha256,
            "proposed_sha256": self.proposed_sha256,
            "output_sha256": self.output_sha256,
            "host_context": self.host_context.as_dict(),
            "decision_id": self.decision_id,
            "authorized_at": self.authorized_at,
        }


def bind_controlled_markdown_authorization(
    plan: ControlledMarkdownUpdatePlan,
    *,
    host_context: TrustedHostSessionContext,
    decision_id: object,
    authorized_at: object,
) -> ControlledMarkdownWriteAuthorization:
    """Bind an already-made host decision to one exact F-05A plan.

    This helper records and integrity-binds the host decision; it does not decide
    whether the proposed research or user-visible change is semantically justified.
    """

    if type(plan) is not ControlledMarkdownUpdatePlan:
        raise ControlledMarkdownAuthorizationError(
            "plan must be an exact ControlledMarkdownUpdatePlan"
        )
    if type(host_context) is not TrustedHostSessionContext:
        raise ControlledMarkdownAuthorizationError(
            "host_context must be a TrustedHostSessionContext"
        )
    normalized_decision_id = _validate_token(decision_id, "decision_id")
    normalized_authorized_at = _validate_utc_timestamp(
        authorized_at, "authorized_at"
    )
    material = _authorization_material(
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        path=plan.path,
        intent=plan.intent,
        current_sha256=plan.current_sha256,
        proposed_sha256=plan.proposed_sha256,
        output_sha256=plan.output_sha256,
        host_context=host_context,
        decision_id=normalized_decision_id,
        authorized_at=normalized_authorized_at,
    )
    return ControlledMarkdownWriteAuthorization(
        schema_version=CONTROLLED_MARKDOWN_PERSISTENCE_SCHEMA_VERSION,
        kind=CONTROLLED_MARKDOWN_AUTHORIZATION_KIND,
        authorization_version=CONTROLLED_MARKDOWN_AUTHORIZATION_VERSION,
        authorization_id="cmwa-" + _sha256(_canonical_json(material)),
        decision="authorized",
        plan_id=plan.plan_id,
        project_id=plan.project_id,
        path=plan.path,
        intent=plan.intent,
        current_sha256=plan.current_sha256,
        proposed_sha256=plan.proposed_sha256,
        output_sha256=plan.output_sha256,
        host_context=host_context,
        decision_id=normalized_decision_id,
        authorized_at=normalized_authorized_at,
    )


@dataclass(frozen=True)
class ControlledMarkdownWriteResult:
    """Body-free result for one committed knowledge-page revision."""

    schema_version: int
    kind: str
    persistence_version: str
    outcome: str
    project_id: str
    path: str
    plan_id: str
    authorization_id: str
    transaction_id: str
    created: bool
    previous_sha256: str | None
    output_sha256: str
    output_byte_count: int
    commit_state: str
    audit_sequence: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ControlledMarkdownPersistenceError(
                "result schema_version must be exact integer 1"
            )
        for field_name in (
            "kind",
            "persistence_version",
            "outcome",
            "project_id",
            "path",
            "plan_id",
            "authorization_id",
            "transaction_id",
            "output_sha256",
            "commit_state",
        ):
            if type(getattr(self, field_name)) is not str:
                raise ControlledMarkdownPersistenceError(
                    f"result {field_name} must be an exact string"
                )
        if self.kind != CONTROLLED_MARKDOWN_PERSISTENCE_KIND:
            raise ControlledMarkdownPersistenceError("result kind is invalid")
        if self.persistence_version != CONTROLLED_MARKDOWN_PERSISTENCE_VERSION:
            raise ControlledMarkdownPersistenceError("result version is invalid")
        if self.outcome != "committed":
            raise ControlledMarkdownPersistenceError("result outcome must be committed")
        validate_project_id(self.project_id)
        artifact_contract_for_path(self.path)
        if _PLAN_ID_RE.fullmatch(self.plan_id) is None:
            raise ControlledMarkdownPersistenceError("result plan_id is invalid")
        if _AUTHORIZATION_ID_RE.fullmatch(self.authorization_id) is None:
            raise ControlledMarkdownPersistenceError(
                "result authorization_id is invalid"
            )
        if _TRANSACTION_ID_RE.fullmatch(self.transaction_id) is None:
            raise ControlledMarkdownPersistenceError(
                "result transaction_id is invalid"
            )
        if type(self.created) is not bool:
            raise ControlledMarkdownPersistenceError("result created must be a boolean")
        _validate_sha256(self.previous_sha256, "previous_sha256", optional=True)
        _validate_sha256(self.output_sha256, "output_sha256")
        if type(self.output_byte_count) is not int or self.output_byte_count < 0:
            raise ControlledMarkdownPersistenceError(
                "result output_byte_count must be a non-negative exact integer"
            )
        if self.commit_state not in {
            "unchanged",
            "committed",
        }:
            raise ControlledMarkdownPersistenceError("result commit_state is invalid")
        if type(self.audit_sequence) is not int or self.audit_sequence < 2:
            raise ControlledMarkdownPersistenceError(
                "result audit_sequence must be an exact integer of at least 2"
            )
        if self.created != (self.previous_sha256 is None):
            raise ControlledMarkdownPersistenceError(
                "result created must agree with previous_sha256"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "persistence_version": self.persistence_version,
            "outcome": self.outcome,
            "project_id": self.project_id,
            "path": self.path,
            "plan_id": self.plan_id,
            "authorization_id": self.authorization_id,
            "transaction_id": self.transaction_id,
            "created": self.created,
            "previous_sha256": self.previous_sha256,
            "output_sha256": self.output_sha256,
            "output_byte_count": self.output_byte_count,
            "commit_state": self.commit_state,
            "audit_sequence": self.audit_sequence,
        }

_AUDIT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "audit_version",
        "sequence",
        "transaction_id",
        "phase",
        "project_id",
        "path",
        "artifact_type",
        "ownership",
        "intent",
        "plan_version",
        "plan_id",
        "authorization_id",
        "decision_id",
        "host_id",
        "actor_type",
        "actor_id",
        "session_id",
        "authorized_at",
        "recorded_at",
        "created",
        "expected_current_sha256",
        "observed_before_sha256",
        "observed_after_sha256",
        "proposed_sha256",
        "output_sha256",
        "output_byte_count",
        "updated_at",
        "user_region_ids",
        "preserved_user_region_ids",
        "changed_user_region_ids",
        "discarded_proposed_user_region_ids",
        "changed_frontmatter_fields",
        "generated_body_changed",
        "user_body_changed",
        "commit_state",
        "reason_code",
    }
)
_AUDIT_BINDING_FIELDS = (
    "transaction_id",
    "project_id",
    "path",
    "artifact_type",
    "ownership",
    "intent",
    "plan_version",
    "plan_id",
    "authorization_id",
    "decision_id",
    "host_id",
    "actor_type",
    "actor_id",
    "session_id",
    "authorized_at",
    "created",
    "expected_current_sha256",
    "observed_before_sha256",
    "proposed_sha256",
    "output_sha256",
    "output_byte_count",
    "updated_at",
    "user_region_ids",
    "preserved_user_region_ids",
    "changed_user_region_ids",
    "discarded_proposed_user_region_ids",
    "changed_frontmatter_fields",
    "generated_body_changed",
    "user_body_changed",
)


def _validate_audit_record(
    value: object,
    *,
    expected_sequence: int,
) -> dict[str, object]:
    try:
        if type(value) is not dict:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit records must be JSON objects"
            )
        record = cast(dict[str, object], value)
        if set(record) != _AUDIT_FIELDS:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit record fields are not exact Schema v1"
            )
        if type(record["schema_version"]) is not int or record["schema_version"] != 1:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit schema_version must be exact integer 1"
            )
        if record["kind"] != CONTROLLED_MARKDOWN_AUDIT_KIND:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit kind is invalid"
            )
        if record["audit_version"] != CONTROLLED_MARKDOWN_AUDIT_VERSION:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit version is unsupported"
            )
        if type(record["sequence"]) is not int or record["sequence"] != expected_sequence:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit sequence is not contiguous"
            )
        if type(record["transaction_id"]) is not str or _TRANSACTION_ID_RE.fullmatch(
            cast(str, record["transaction_id"])
        ) is None:
            raise ControlledMarkdownAuditStateError("audit transaction_id is invalid")
        phase = record["phase"]
        if type(phase) is not str or phase not in {
            "prepared",
            "committed",
            "conflict",
            "failed",
            "commit-unknown",
        }:
            raise ControlledMarkdownAuditStateError("audit phase is invalid")
        if type(record["project_id"]) is not str:
            raise ControlledMarkdownAuditStateError("audit project_id is invalid")
        validate_project_id(cast(str, record["project_id"]))
        if type(record["path"]) is not str:
            raise ControlledMarkdownAuditStateError("audit path is invalid")
        contract = artifact_contract_for_path(record["path"])
        if contract.path != record["path"]:
            raise ControlledMarkdownAuditStateError("audit path is not canonical")
        if record["artifact_type"] != contract.artifact_type:
            raise ControlledMarkdownAuditStateError(
                "audit artifact_type does not match path"
            )
        if record["ownership"] not in {"generated", "mixed", "user"}:
            raise ControlledMarkdownAuditStateError("audit ownership is invalid")
        if record["intent"] not in {"regenerate", "user-edit"}:
            raise ControlledMarkdownAuditStateError("audit intent is invalid")
        if type(record["plan_id"]) is not str or _PLAN_ID_RE.fullmatch(
            cast(str, record["plan_id"])
        ) is None:
            raise ControlledMarkdownAuditStateError("audit plan_id is invalid")
        if type(record["authorization_id"]) is not str or _AUTHORIZATION_ID_RE.fullmatch(
            cast(str, record["authorization_id"])
        ) is None:
            raise ControlledMarkdownAuditStateError(
                "audit authorization_id is invalid"
            )
        for field_name in (
            "decision_id",
            "host_id",
            "actor_id",
            "session_id",
        ):
            if type(record[field_name]) is not str or _TOKEN_RE.fullmatch(
                cast(str, record[field_name])
            ) is None:
                raise ControlledMarkdownAuditStateError(
                    f"audit {field_name} is invalid"
                )
        if record["actor_type"] not in {"user", "host-agent"}:
            raise ControlledMarkdownAuditStateError("audit actor_type is invalid")
        if (
            type(record["plan_version"]) is not str
            or record["plan_version"] != CONTROLLED_MARKDOWN_VERSION
        ):
            raise ControlledMarkdownAuditStateError("audit plan_version is invalid")
        _validate_utc_timestamp(record["authorized_at"], "authorized_at")
        _validate_utc_timestamp(record["recorded_at"], "recorded_at")
        _validate_utc_timestamp(record["updated_at"], "updated_at")
        user_region_ids = _validate_bounded_string_list(
            record["user_region_ids"],
            "user_region_ids",
            pattern=_REGION_ID_RE,
        )
        preserved_user_region_ids = _validate_bounded_string_list(
            record["preserved_user_region_ids"],
            "preserved_user_region_ids",
            pattern=_REGION_ID_RE,
        )
        changed_user_region_ids = _validate_bounded_string_list(
            record["changed_user_region_ids"],
            "changed_user_region_ids",
            pattern=_REGION_ID_RE,
        )
        discarded_proposed_user_region_ids = _validate_bounded_string_list(
            record["discarded_proposed_user_region_ids"],
            "discarded_proposed_user_region_ids",
            pattern=_REGION_ID_RE,
        )
        changed_frontmatter_fields = _validate_bounded_string_list(
            record["changed_frontmatter_fields"],
            "changed_frontmatter_fields",
            pattern=_FIELD_NAME_RE,
        )
        if not set(preserved_user_region_ids).issubset(user_region_ids):
            raise ControlledMarkdownAuditStateError(
                "audit preserved_user_region_ids must be a subset of user_region_ids"
            )
        for field_name, values in (
            ("changed_user_region_ids", changed_user_region_ids),
            (
                "discarded_proposed_user_region_ids",
                discarded_proposed_user_region_ids,
            ),
        ):
            if not set(values).issubset(user_region_ids):
                raise ControlledMarkdownAuditStateError(
                    f"audit {field_name} must be a subset of user_region_ids"
                )
        if any(
            field_name not in _AUDIT_FRONTMATTER_FIELDS
            for field_name in changed_frontmatter_fields
        ) or tuple(
            sorted(
                changed_frontmatter_fields,
                key=_AUDIT_FRONTMATTER_FIELDS.index,
            )
        ) != changed_frontmatter_fields:
            raise ControlledMarkdownAuditStateError(
                "audit changed_frontmatter_fields are not canonical Schema v2 fields"
            )
        for field_name in ("generated_body_changed", "user_body_changed"):
            if type(record[field_name]) is not bool:
                raise ControlledMarkdownAuditStateError(
                    f"audit {field_name} must be a boolean"
                )
        if type(record["created"]) is not bool:
            raise ControlledMarkdownAuditStateError("audit created must be a boolean")
        if record["ownership"] != "mixed" and (
            user_region_ids
            or preserved_user_region_ids
            or changed_user_region_ids
            or discarded_proposed_user_region_ids
        ):
            raise ControlledMarkdownAuditStateError(
                "non-mixed audit records cannot declare user regions"
            )
        if record["intent"] == "regenerate":
            if changed_user_region_ids:
                raise ControlledMarkdownAuditStateError(
                    "regeneration cannot report changed user-region content"
                )
            if record["ownership"] == "mixed":
                expected_preserved = (
                    () if record["created"] else user_region_ids
                )
                if preserved_user_region_ids != expected_preserved:
                    raise ControlledMarkdownAuditStateError(
                        "mixed regeneration preservation metadata is inconsistent"
                    )
        elif discarded_proposed_user_region_ids:
            raise ControlledMarkdownAuditStateError(
                "user-edit cannot report discarded generated user-region content"
            )
        if record["ownership"] == "mixed" and record["intent"] == "user-edit":
            if record["generated_body_changed"] or preserved_user_region_ids:
                raise ControlledMarkdownAuditStateError(
                    "mixed user-edit body metadata is inconsistent"
                )
        if record["ownership"] == "generated" and record["user_body_changed"]:
            raise ControlledMarkdownAuditStateError(
                "generated ownership cannot report a user-body change"
            )
        if record["ownership"] == "user" and record["generated_body_changed"]:
            raise ControlledMarkdownAuditStateError(
                "user ownership cannot report a generated-body change"
            )
        for field_name in (
            "expected_current_sha256",
            "observed_before_sha256",
            "observed_after_sha256",
        ):
            _validate_sha256(record[field_name], field_name, optional=True)
        for field_name in ("proposed_sha256", "output_sha256"):
            _validate_sha256(record[field_name], field_name)
        if (
            type(record["output_byte_count"]) is not int
            or cast(int, record["output_byte_count"]) < 0
        ):
            raise ControlledMarkdownAuditStateError(
                "audit output_byte_count must be a non-negative exact integer"
            )
        if record["created"] != (record["expected_current_sha256"] is None):
            raise ControlledMarkdownAuditStateError(
                "audit created must agree with expected_current_sha256"
            )
        if record["observed_before_sha256"] != record["expected_current_sha256"]:
            raise ControlledMarkdownAuditStateError(
                "audit observed_before_sha256 must match the prepared revision"
            )
        reason = record["reason_code"]
        if reason is not None and (
            type(reason) is not str or _REASON_RE.fullmatch(reason) is None
        ):
            raise ControlledMarkdownAuditStateError("audit reason_code is invalid")

        if phase == "prepared":
            if (
                record["commit_state"] != "not-attempted"
                or reason is not None
                or record["observed_after_sha256"] is not None
            ):
                raise ControlledMarkdownAuditStateError(
                    "prepared audit state is inconsistent"
                )
        elif phase == "committed":
            if record["commit_state"] not in {
                "unchanged",
                "committed",
            }:
                raise ControlledMarkdownAuditStateError(
                    "committed audit commit_state is invalid"
                )
            if reason is not None or record["observed_after_sha256"] != record["output_sha256"]:
                raise ControlledMarkdownAuditStateError(
                    "committed audit outcome is inconsistent"
                )
        elif phase == "conflict":
            if record["commit_state"] != "conflict" or reason is None:
                raise ControlledMarkdownAuditStateError(
                    "conflict audit outcome is inconsistent"
                )
            if (
                reason == "revision-conflict"
                and record["expected_current_sha256"] is not None
                and record["observed_after_sha256"]
                == record["expected_current_sha256"]
            ):
                raise ControlledMarkdownAuditStateError(
                    "revision conflict must report a changed live revision"
                )
        elif phase == "failed":
            if record["commit_state"] != "failed" or reason is None:
                raise ControlledMarkdownAuditStateError(
                    "failed audit outcome is inconsistent"
                )
        else:
            if record["commit_state"] != "unknown" or reason is None:
                raise ControlledMarkdownAuditStateError(
                    "commit-unknown audit outcome is inconsistent"
                )
        return record
    except ControlledMarkdownAuditStateError:
        raise
    except (ControlledMarkdownPersistenceError, KnowledgeArtifactError, LayoutError, ValueError) as exc:
        raise ControlledMarkdownAuditStateError(
            "controlled Markdown audit record is structurally invalid"
        ) from exc


def _audit_decision_key(record: dict[str, object]) -> tuple[object, ...]:
    return (
        record["host_id"],
        record["actor_type"],
        record["actor_id"],
        record["session_id"],
        record["decision_id"],
    )


def _authorization_decision_key(
    authorization: ControlledMarkdownWriteAuthorization,
) -> tuple[object, ...]:
    return (
        authorization.host_context.host_id,
        authorization.host_context.actor_type,
        authorization.host_context.actor_id,
        authorization.host_context.session_id,
        authorization.decision_id,
    )


def _validate_audit_history(records: list[dict[str, object]]) -> None:
    prepared_by_transaction: dict[str, dict[str, object]] = {}
    authorization_owner: dict[str, str] = {}
    decision_owner: dict[tuple[object, ...], str] = {}
    last_transaction: str | None = None
    last_phase: str | None = None
    for record in records:
        transaction_id = cast(str, record["transaction_id"])
        authorization_id = cast(str, record["authorization_id"])
        phase = cast(str, record["phase"])
        if phase == "prepared":
            if last_phase == "prepared":
                raise ControlledMarkdownAuditStateError(
                    "a prepared audit transaction must be resolved before another begins"
                )
            if transaction_id in prepared_by_transaction:
                raise ControlledMarkdownAuditStateError(
                    "an audit transaction may contain only one prepared record"
                )
            owner = authorization_owner.get(authorization_id)
            if owner is not None and owner != transaction_id:
                raise ControlledMarkdownAuditStateError(
                    "an authorization_id may bind only one audit transaction"
                )
            decision_key = _audit_decision_key(record)
            decision_transaction = decision_owner.get(decision_key)
            if decision_transaction is not None and decision_transaction != transaction_id:
                raise ControlledMarkdownAuditStateError(
                    "one trusted host/session decision may bind only one audit transaction"
                )
            prepared_by_transaction[transaction_id] = record
            authorization_owner[authorization_id] = transaction_id
            decision_owner[decision_key] = transaction_id
        else:
            prepared = prepared_by_transaction.get(transaction_id)
            if prepared is None:
                raise ControlledMarkdownAuditStateError(
                    "terminal audit record has no prepared transaction"
                )
            if last_transaction != transaction_id or last_phase != "prepared":
                raise ControlledMarkdownAuditStateError(
                    "terminal audit record must immediately follow its prepared record"
                )
            for field_name in _AUDIT_BINDING_FIELDS:
                if record[field_name] != prepared[field_name]:
                    raise ControlledMarkdownAuditStateError(
                        "terminal audit record does not match its prepared binding"
                    )
        last_transaction = transaction_id
        last_phase = phase


def _parse_audit_payload(payload: bytes) -> tuple[dict[str, object], ...]:
    if type(payload) is not bytes:
        raise TypeError("audit payload must be exact bytes")
    if len(payload) > MAX_CONTROLLED_MARKDOWN_AUDIT_BYTES:
        raise ControlledMarkdownAuditStateError(
            "controlled Markdown audit ledger exceeds its bounded size"
        )
    if not payload:
        return ()
    if not payload.endswith(b"\n"):
        raise ControlledMarkdownAuditStateError(
            "controlled Markdown audit ledger must end with LF"
        )
    records: list[dict[str, object]] = []
    lines = payload.splitlines(keepends=True)
    if len(lines) > MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS:
        raise ControlledMarkdownAuditStateError(
            "controlled Markdown audit ledger exceeds its record-count bound"
        )
    for sequence, line in enumerate(lines, start=1):
        if len(line) > MAX_CONTROLLED_MARKDOWN_AUDIT_RECORD_BYTES:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit record exceeds its bounded size"
            )
        if not line.endswith(b"\n") or line == b"\n":
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit ledger contains a blank or unterminated record"
            )
        raw = line[:-1]
        try:
            value = parse_json_bytes_strict(
                raw,
                label="controlled Markdown audit record",
            )
        except LayoutError as exc:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit ledger is not strict JSONL"
            ) from exc
        record = _validate_audit_record(value, expected_sequence=sequence)
        if _canonical_json(record) != raw:
            raise ControlledMarkdownAuditStateError(
                "controlled Markdown audit records must use canonical JSON"
            )
        records.append(record)
    _validate_audit_history(records)
    return tuple(records)

@dataclass(frozen=True)
class _AuditState:
    payload: bytes
    content_sha256: str | None
    records: tuple[dict[str, object], ...]


def _read_audit_state(
    runs_lease: StableDirectoryLease,
    ledger_path: Path,
) -> _AuditState:
    try:
        observation = read_stable_regular_file(
            runs_lease.root,
            ledger_path,
            reject_redirection=True,
            capture_bytes=True,
            root_lease=runs_lease,
        )
    except StableFileMissingError:
        return _AuditState(payload=b"", content_sha256=None, records=())
    except StableFileAccessError as exc:
        raise ControlledMarkdownAuditStateError(
            "controlled Markdown audit ledger cannot be read safely"
        ) from exc
    if observation.data is None:
        raise ControlledMarkdownAuditStateError(
            "controlled Markdown audit ledger bytes were not captured"
        )
    return _AuditState(
        payload=observation.data,
        content_sha256=observation.content_sha256,
        records=_parse_audit_payload(observation.data),
    )


def _append_audit_record(
    runs_lease: StableDirectoryLease,
    ledger_path: Path,
    builder: Callable[[int, tuple[dict[str, object], ...]], dict[str, object]],
    *,
    markdown_may_be_committed: bool,
    transaction_id: str | None = None,
    reserved_record_count: int = 0,
    reserved_byte_count: int = 0,
) -> tuple[dict[str, object], StableFileWriteResult]:
    """Append one canonical audit record with phase-aware failure mapping.

    A terminal append runs after page publication may already be visible.  Every
    state read, record construction/validation, capacity check, CAS, and post-CAS
    verification therefore belongs to the same uncertainty boundary: a failure
    there must be reported as ``commit-audit-unknown`` rather than as a normal
    pre-publication rejection.  Prepared-phase failures retain their precise
    pre-publication audit errors.

    ``reserved_*`` is used by the prepared phase to leave room for its mandatory
    terminal record before any page bytes are published.
    """

    if (
        type(reserved_record_count) is not int
        or reserved_record_count < 0
        or type(reserved_byte_count) is not int
        or reserved_byte_count < 0
    ):
        raise ValueError("audit capacity reservations must be non-negative integers")
    if markdown_may_be_committed and (
        type(transaction_id) is not str
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
    ):
        raise ValueError("terminal audit append requires a valid transaction_id")

    record: dict[str, object] | None = None

    def unknown_error() -> ControlledMarkdownCommitAuditUnknownError:
        # The guard above proves this for every terminal caller.
        resolved_transaction_id = cast(str, transaction_id)
        return ControlledMarkdownCommitAuditUnknownError(
            "Markdown may be committed but terminal audit state is unknown",
            transaction_id=resolved_transaction_id,
        )

    try:
        state = _read_audit_state(runs_lease, ledger_path)
        sequence = len(state.records) + 1
        if sequence + reserved_record_count > MAX_CONTROLLED_MARKDOWN_AUDIT_RECORDS:
            raise ControlledMarkdownAuditWriteError(
                "controlled Markdown audit ledger lacks the reserved record capacity"
            )
        record = builder(sequence, state.records)
        record = _validate_audit_record(record, expected_sequence=sequence)
        combined_records = list(state.records) + [record]
        _validate_audit_history(combined_records)
        line = _canonical_json(record) + b"\n"
        if len(line) > MAX_CONTROLLED_MARKDOWN_AUDIT_RECORD_BYTES:
            raise ControlledMarkdownAuditWriteError(
                "controlled Markdown audit record exceeds its bounded size"
            )
        payload = state.payload + line
        if (
            len(payload) + reserved_byte_count
            > MAX_CONTROLLED_MARKDOWN_AUDIT_BYTES
        ):
            raise ControlledMarkdownAuditWriteError(
                "controlled Markdown audit ledger lacks the reserved byte capacity"
            )
        write_result = compare_and_swap_atomic_stable_file(
            runs_lease.root,
            ledger_path,
            payload,
            expected_current_sha256=state.content_sha256,
            root_lease=runs_lease,
        )
        verified = _read_audit_state(runs_lease, ledger_path)
        if not verified.records or verified.payload != payload or verified.records[-1] != record:
            raise ControlledMarkdownAuditWriteError(
                "controlled Markdown audit append could not be verified"
            )
        if write_result.commit_state != "committed":
            if markdown_may_be_committed:
                raise unknown_error()
            raise ControlledMarkdownAuditWriteError(
                "prepared audit durability is unknown; Markdown was not published"
            )
        return record, write_result
    except ControlledMarkdownAuthorizationReuseError:
        # Reuse is a caller/transaction decision error, not publication
        # uncertainty, and must remain directly observable.
        raise
    except ControlledMarkdownCommitAuditUnknownError:
        raise
    except Exception as exc:
        if markdown_may_be_committed:
            raise unknown_error() from exc
        if isinstance(exc, ControlledMarkdownAuditStateError):
            raise
        if isinstance(exc, ControlledMarkdownAuditWriteError):
            raise
        if isinstance(exc, StableFileAccessError):
            raise ControlledMarkdownAuditWriteError(
                "controlled Markdown audit append failed before Markdown publication"
            ) from exc
        raise


def _registration_identity(result: ProjectRegistrationResult) -> tuple[object, ...]:
    return (
        result.project_id,
        result.project_root,
        result.project_file,
        result.layout.workspace_root,
        result.layout.machine_root,
        result.layout.knowledge_projects_root,
        result.layout.knowledge_root,
        _sha256(_canonical_json(result.record)),
    )


def _assert_registration_unchanged(
    expected: ProjectRegistrationResult,
    observed: ProjectRegistrationResult,
) -> None:
    if _registration_identity(expected) != _registration_identity(observed):
        raise ControlledMarkdownRegistrationChangedError(
            "registered project storage changed during controlled Markdown persistence"
        )


def _assert_authorization_matches_plan(
    authorization: ControlledMarkdownWriteAuthorization,
    plan: ControlledMarkdownUpdatePlan,
) -> None:
    if (
        authorization.plan_id != plan.plan_id
        or authorization.project_id != plan.project_id
        or authorization.path != plan.path
        or authorization.intent != plan.intent
        or authorization.current_sha256 != plan.current_sha256
        or authorization.proposed_sha256 != plan.proposed_sha256
        or authorization.output_sha256 != plan.output_sha256
    ):
        raise ControlledMarkdownAuthorizationMismatchError(
            "host authorization is not bound to the freshly recomputed plan"
        )


def _transaction_id(
    *,
    sequence: int,
    authorization: ControlledMarkdownWriteAuthorization,
) -> str:
    return "cmtx-" + _sha256(
        _canonical_json(
            {
                "transaction_version": CONTROLLED_MARKDOWN_PERSISTENCE_VERSION,
                "sequence": sequence,
                "authorization_id": authorization.authorization_id,
                "plan_id": authorization.plan_id,
                "project_id": authorization.project_id,
                "path": authorization.path,
            }
        )
    )


def _audit_record_base(
    *,
    sequence: int,
    transaction_id: str,
    phase: AuditPhase,
    plan: ControlledMarkdownUpdatePlan,
    authorization: ControlledMarkdownWriteAuthorization,
    observed_after_sha256: str | None,
    commit_state: str,
    reason_code: str | None,
) -> dict[str, object]:
    return {
        "schema_version": CONTROLLED_MARKDOWN_PERSISTENCE_SCHEMA_VERSION,
        "kind": CONTROLLED_MARKDOWN_AUDIT_KIND,
        "audit_version": CONTROLLED_MARKDOWN_AUDIT_VERSION,
        "sequence": sequence,
        "transaction_id": transaction_id,
        "phase": phase,
        "project_id": plan.project_id,
        "path": plan.path,
        "artifact_type": plan.artifact_type,
        "ownership": plan.ownership,
        "intent": plan.intent,
        "plan_version": plan.plan_version,
        "plan_id": plan.plan_id,
        "authorization_id": authorization.authorization_id,
        "decision_id": authorization.decision_id,
        "host_id": authorization.host_context.host_id,
        "actor_type": authorization.host_context.actor_type,
        "actor_id": authorization.host_context.actor_id,
        "session_id": authorization.host_context.session_id,
        "authorized_at": authorization.authorized_at,
        "recorded_at": _utc_now(),
        "created": plan.created,
        "expected_current_sha256": plan.current_sha256,
        "observed_before_sha256": plan.current_sha256,
        "observed_after_sha256": observed_after_sha256,
        "proposed_sha256": plan.proposed_sha256,
        "output_sha256": plan.output_sha256,
        "output_byte_count": plan.output_byte_count,
        "updated_at": plan.updated_at,
        "user_region_ids": list(plan.user_region_ids),
        "preserved_user_region_ids": list(plan.preserved_user_region_ids),
        "changed_user_region_ids": list(plan.changed_user_region_ids),
        "discarded_proposed_user_region_ids": list(
            plan.discarded_proposed_user_region_ids
        ),
        "changed_frontmatter_fields": list(plan.changed_frontmatter_fields),
        "generated_body_changed": plan.generated_body_changed,
        "user_body_changed": plan.user_body_changed,
        "commit_state": commit_state,
        "reason_code": reason_code,
    }


def _assert_authorization_unused(
    runs_lease: StableDirectoryLease,
    ledger_path: Path,
    authorization: ControlledMarkdownWriteAuthorization,
) -> None:
    state = _read_audit_state(runs_lease, ledger_path)
    decision_key = _authorization_decision_key(authorization)
    if any(
        record["authorization_id"] == authorization.authorization_id
        or _audit_decision_key(record) == decision_key
        for record in state.records
    ):
        raise ControlledMarkdownAuthorizationReuseError(
            "host authorization or trusted host/session decision was already consumed"
        )


def _append_prepared(
    runs_lease: StableDirectoryLease,
    ledger_path: Path,
    *,
    plan: ControlledMarkdownUpdatePlan,
    authorization: ControlledMarkdownWriteAuthorization,
) -> tuple[str, int]:
    def build(
        sequence: int,
        records: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        decision_key = _authorization_decision_key(authorization)
        if any(
            record["authorization_id"] == authorization.authorization_id
            or _audit_decision_key(record) == decision_key
            for record in records
        ):
            raise ControlledMarkdownAuthorizationReuseError(
                "host authorization or trusted host/session decision was already consumed"
            )
        transaction_id = _transaction_id(
            sequence=sequence,
            authorization=authorization,
        )
        return _audit_record_base(
            sequence=sequence,
            transaction_id=transaction_id,
            phase="prepared",
            plan=plan,
            authorization=authorization,
            observed_after_sha256=None,
            commit_state="not-attempted",
            reason_code=None,
        )

    record, _ = _append_audit_record(
        runs_lease,
        ledger_path,
        build,
        markdown_may_be_committed=False,
        reserved_record_count=1,
        reserved_byte_count=MAX_CONTROLLED_MARKDOWN_AUDIT_RECORD_BYTES,
    )
    return cast(str, record["transaction_id"]), cast(int, record["sequence"])


def _append_terminal(
    runs_lease: StableDirectoryLease,
    ledger_path: Path,
    *,
    transaction_id: str,
    plan: ControlledMarkdownUpdatePlan,
    authorization: ControlledMarkdownWriteAuthorization,
    phase: Literal["committed", "conflict", "failed", "commit-unknown"],
    observed_after_sha256: str | None,
    commit_state: str,
    reason_code: str | None,
    markdown_may_be_committed: bool,
) -> int:
    def build(
        sequence: int,
        records: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        if not records:
            raise ControlledMarkdownAuditStateError(
                "terminal audit record has no prepared record"
            )
        prepared = records[-1]
        if (
            prepared["transaction_id"] != transaction_id
            or prepared["phase"] != "prepared"
        ):
            raise ControlledMarkdownAuditStateError(
                "terminal audit record does not immediately follow its prepared record"
            )
        return _audit_record_base(
            sequence=sequence,
            transaction_id=transaction_id,
            phase=phase,
            plan=plan,
            authorization=authorization,
            observed_after_sha256=observed_after_sha256,
            commit_state=commit_state,
            reason_code=reason_code,
        )

    record, _ = _append_audit_record(
        runs_lease,
        ledger_path,
        build,
        markdown_may_be_committed=markdown_may_be_committed,
        transaction_id=transaction_id,
    )
    return cast(int, record["sequence"])


@dataclass(frozen=True)
class _KnowledgeSnapshot:
    data: bytes | None
    content_sha256: str | None


def _knowledge_target(
    registration: ProjectRegistrationResult,
    path: object,
) -> tuple[str, Path]:
    try:
        contract = artifact_contract_for_path(path)
    except (KnowledgeArtifactError, TypeError, ValueError) as exc:
        raise ControlledMarkdownWriteRejectedError(
            "controlled Markdown path is not a canonical project knowledge path"
        ) from exc
    normalized_path = contract.path
    parts = PurePosixPath(normalized_path).parts
    target = registration.layout.knowledge_root.joinpath(*parts)
    try:
        target.relative_to(registration.layout.knowledge_root)
    except ValueError as exc:
        raise ControlledMarkdownWriteRejectedError(
            "controlled Markdown target escaped the registered knowledge root"
        ) from exc
    return normalized_path, target


def _read_knowledge_snapshot(
    knowledge_lease: StableDirectoryLease,
    target: Path,
) -> _KnowledgeSnapshot:
    try:
        observation = read_stable_regular_file(
            knowledge_lease.root,
            target,
            reject_redirection=True,
            capture_bytes=True,
            root_lease=knowledge_lease,
        )
    except StableFileMissingError:
        return _KnowledgeSnapshot(data=None, content_sha256=None)
    if observation.data is None:
        raise StableFileAccessError(
            f"stable knowledge read did not capture bytes: {target}"
        )
    return _KnowledgeSnapshot(
        data=observation.data,
        content_sha256=observation.content_sha256,
    )


def _observe_knowledge_sha256(
    knowledge_lease: StableDirectoryLease,
    target: Path,
) -> str | None:
    try:
        return _read_knowledge_snapshot(knowledge_lease, target).content_sha256
    except StableFileAccessError:
        return None


def _stable_failure_reason(exc: BaseException, default: str) -> str:
    candidate = getattr(exc, "reason_code", default)
    if type(candidate) is not str or _REASON_RE.fullmatch(candidate) is None:
        return default
    return candidate


def _raise_plan_rejection(
    exc: ControlledMarkdownError,
    *,
    expected_current_sha256: str | None,
    observed_current_sha256: str | None,
) -> None:
    if getattr(exc, "reason_code", None) == "revision-conflict":
        raise ControlledMarkdownWriteConflictError(
            "the live knowledge page no longer satisfies the requested revision",
            expected_current_sha256=expected_current_sha256,
            observed_current_sha256=observed_current_sha256,
            transaction_id=None,
        ) from exc
    reason = getattr(exc, "reason_code", "invalid-controlled-markdown")
    raise ControlledMarkdownWriteRejectedError(
        f"controlled Markdown plan was rejected ({reason})"
    ) from exc


def _assert_initial_authorization_binding(
    authorization: ControlledMarkdownWriteAuthorization,
    *,
    project_id: str,
    path: str,
    intent: object,
    expected_current_sha256: str | None,
    proposed: bytes,
) -> None:
    if (
        authorization.project_id != project_id
        or authorization.path != path
        or authorization.intent != intent
        or authorization.current_sha256 != expected_current_sha256
        or authorization.proposed_sha256 != _sha256(proposed)
    ):
        raise ControlledMarkdownAuthorizationMismatchError(
            "host authorization does not match the requested project, path, intent, or revision"
        )


def _append_failed_terminal(
    machine_lease: StableDirectoryLease,
    ledger_path: Path,
    *,
    transaction_id: str,
    plan: ControlledMarkdownUpdatePlan,
    authorization: ControlledMarkdownWriteAuthorization,
    observed_after_sha256: str | None,
    reason_code: str,
) -> int:
    return _append_terminal(
        machine_lease,
        ledger_path,
        transaction_id=transaction_id,
        plan=plan,
        authorization=authorization,
        phase="failed",
        observed_after_sha256=observed_after_sha256,
        commit_state="failed",
        reason_code=reason_code,
        markdown_may_be_committed=False,
    )


def _append_commit_unknown_terminal(
    machine_lease: StableDirectoryLease,
    ledger_path: Path,
    *,
    transaction_id: str,
    plan: ControlledMarkdownUpdatePlan,
    authorization: ControlledMarkdownWriteAuthorization,
    observed_after_sha256: str | None,
    reason_code: str,
) -> int:
    return _append_terminal(
        machine_lease,
        ledger_path,
        transaction_id=transaction_id,
        plan=plan,
        authorization=authorization,
        phase="commit-unknown",
        observed_after_sha256=observed_after_sha256,
        commit_state="unknown",
        reason_code=reason_code,
        markdown_may_be_committed=True,
    )


def _revalidate_authorization_integrity(
    authorization: ControlledMarkdownWriteAuthorization,
) -> None:
    """Re-run constructor invariants at the persistence boundary.

    Frozen dataclasses prevent ordinary assignment but are not an integrity or
    trust boundary: a caller can still mutate them through reflection or pass a
    nested mutable object.  Revalidating the nested host context and the complete
    authorization before any ledger/page access prevents such mutations from
    reaching the persistence transaction.
    """

    if type(authorization.host_context) is not TrustedHostSessionContext:
        raise ControlledMarkdownAuthorizationError(
            "authorization host_context integrity is invalid"
        )
    authorization.host_context.__post_init__()
    authorization.__post_init__()


def persist_controlled_markdown_update(
    workspace_root: str | Path,
    project_id: object,
    *,
    path: object,
    proposed: object,
    intent: object,
    expected_current_sha256: object | None,
    authorization: object,
    lock_timeout_seconds: object = LOCK_TIMEOUT_SECONDS,
) -> ControlledMarkdownWriteResult:
    """Persist one already-authorized F-05A plan with exact live-file CAS.

    The caller supplies the complete proposed Schema v2 page and the exact base
    revision used by its host decision.  Core never trusts a caller-provided plan
    object or plan metadata: under the per-project machine-state lock it reloads
    the registration, stably reads the live knowledge page, recomputes the F-05A
    plan, and compares that result with the structured authorization.

    A successful call appends ``prepared`` followed by ``committed`` to the body-free
    project audit ledger.  Revision races append ``conflict`` and consume the
    authorization.  Safe pre-publication failures append ``failed``; any outcome in
    which page publication or terminal audit durability cannot be proved appends
    ``commit-unknown`` and raises an explicit uncertainty error.  No rollback is
    attempted after publication.
    """

    if type(authorization) is not ControlledMarkdownWriteAuthorization:
        raise ControlledMarkdownAuthorizationError(
            "authorization must be an exact ControlledMarkdownWriteAuthorization"
        )
    _revalidate_authorization_integrity(authorization)
    if type(proposed) is not bytes:
        raise ControlledMarkdownWriteRejectedError(
            "proposed must contain exact UTF-8 page bytes"
        )
    try:
        normalized_project_id = validate_project_id(project_id)
        normalized_expected_sha256 = _validate_sha256(
            expected_current_sha256,
            "expected_current_sha256",
            optional=True,
        )
    except (LayoutError, ControlledMarkdownPersistenceError) as exc:
        if isinstance(exc, ControlledMarkdownPersistenceError):
            raise
        raise ControlledMarkdownWriteRejectedError(
            "project_id or expected revision is invalid"
        ) from exc
    timeout = _validate_lock_timeout(lock_timeout_seconds)

    try:
        registration = load_registered_project(
            workspace_root,
            normalized_project_id,
        )
    except (LayoutError, OSError, TypeError, ValueError) as exc:
        raise ControlledMarkdownWriteRejectedError(
            "registered project could not be loaded safely"
        ) from exc
    normalized_path, target = _knowledge_target(registration, path)
    _assert_initial_authorization_binding(
        authorization,
        project_id=normalized_project_id,
        path=normalized_path,
        intent=intent,
        expected_current_sha256=normalized_expected_sha256,
        proposed=proposed,
    )

    layout = registration.layout
    ledger_path = layout.indexes_dir / CONTROLLED_MARKDOWN_AUDIT_FILE
    try:
        with exclusive_stable_file_lock(
            layout.indexes_dir,
            layout.machine_state_lock_file,
            timeout_seconds=timeout,
        ) as machine_lease:
            try:
                locked_registration = load_registered_project(
                    workspace_root,
                    normalized_project_id,
                )
            except (LayoutError, OSError, TypeError, ValueError) as exc:
                raise ControlledMarkdownRegistrationChangedError(
                    "registered project could not be reloaded under the mutation lock"
                ) from exc
            _assert_registration_unchanged(registration, locked_registration)
            locked_path, locked_target = _knowledge_target(
                locked_registration,
                normalized_path,
            )
            if locked_path != normalized_path or locked_target != target:
                raise ControlledMarkdownRegistrationChangedError(
                    "knowledge target changed while acquiring the mutation lock"
                )

            try:
                _assert_authorization_unused(
                    machine_lease,
                    ledger_path,
                    authorization,
                )
                with lease_stable_directory(locked_target.parent) as knowledge_lease:
                    snapshot = _read_knowledge_snapshot(knowledge_lease, locked_target)
                    try:
                        plan = plan_controlled_markdown_update(
                            path=normalized_path,
                            current=snapshot.data,
                            proposed=proposed,
                            intent=intent,
                            expected_current_sha256=normalized_expected_sha256,
                        )
                    except ControlledMarkdownError as exc:
                        _raise_plan_rejection(
                            exc,
                            expected_current_sha256=normalized_expected_sha256,
                            observed_current_sha256=snapshot.content_sha256,
                        )
                    if plan.project_id != normalized_project_id:
                        raise ControlledMarkdownWriteRejectedError(
                            "proposed page project_id does not match the registered project"
                        )
                    _assert_authorization_matches_plan(authorization, plan)
                    transaction_id, _ = _append_prepared(
                        machine_lease,
                        ledger_path,
                        plan=plan,
                        authorization=authorization,
                    )
                    try:
                        write_result = compare_and_swap_atomic_stable_file(
                            knowledge_lease.root,
                            locked_target,
                            plan.output_bytes,
                            expected_current_sha256=plan.current_sha256,
                            root_lease=knowledge_lease,
                        )
                    except StableFileRevisionConflictError as exc:
                        _append_terminal(
                            machine_lease,
                            ledger_path,
                            transaction_id=transaction_id,
                            plan=plan,
                            authorization=authorization,
                            phase="conflict",
                            observed_after_sha256=exc.observed_current_sha256,
                            commit_state="conflict",
                            reason_code="revision-conflict",
                            markdown_may_be_committed=False,
                        )
                        raise ControlledMarkdownWriteConflictError(
                            "live knowledge revision changed before atomic publication",
                            expected_current_sha256=plan.current_sha256,
                            observed_current_sha256=exc.observed_current_sha256,
                            transaction_id=transaction_id,
                        ) from exc
                    except StableFileCommitUnknownError as exc:
                        observed_after = _observe_knowledge_sha256(
                            knowledge_lease,
                            locked_target,
                        )
                        _append_commit_unknown_terminal(
                            machine_lease,
                            ledger_path,
                            transaction_id=transaction_id,
                            plan=plan,
                            authorization=authorization,
                            observed_after_sha256=observed_after,
                            reason_code="commit-state-unknown",
                        )
                        raise ControlledMarkdownCommitUnknownError(
                            "page publication may have occurred but its commit state is unknown",
                            transaction_id=transaction_id,
                        ) from exc
                    except StableFileAccessError as exc:
                        observed_after = _observe_knowledge_sha256(
                            knowledge_lease,
                            locked_target,
                        )
                        if observed_after == plan.output_sha256:
                            _append_commit_unknown_terminal(
                                machine_lease,
                                ledger_path,
                                transaction_id=transaction_id,
                                plan=plan,
                                authorization=authorization,
                                observed_after_sha256=observed_after,
                                reason_code="write-state-unknown",
                            )
                            raise ControlledMarkdownCommitUnknownError(
                                "page publication state is unknown after a stable-file failure",
                                transaction_id=transaction_id,
                            ) from exc
                        _append_failed_terminal(
                            machine_lease,
                            ledger_path,
                            transaction_id=transaction_id,
                            plan=plan,
                            authorization=authorization,
                            observed_after_sha256=observed_after,
                            reason_code=_stable_failure_reason(
                                exc,
                                "stable-file-write-failed",
                            ),
                        )
                        raise ControlledMarkdownWriteFailedError(
                            "page publication failed before a committed output was observed",
                            transaction_id=transaction_id,
                            observed_after_sha256=observed_after,
                        ) from exc
                    except Exception as exc:
                        observed_after = _observe_knowledge_sha256(
                            knowledge_lease,
                            locked_target,
                        )
                        _append_commit_unknown_terminal(
                            machine_lease,
                            ledger_path,
                            transaction_id=transaction_id,
                            plan=plan,
                            authorization=authorization,
                            observed_after_sha256=observed_after,
                            reason_code="unexpected-write-state",
                        )
                        raise ControlledMarkdownCommitUnknownError(
                            "page publication state is unknown after an unexpected failure",
                            transaction_id=transaction_id,
                        ) from exc

                    if write_result.commit_state == "committed-durability-unknown":
                        observed_after = _observe_knowledge_sha256(
                            knowledge_lease,
                            locked_target,
                        )
                        _append_commit_unknown_terminal(
                            machine_lease,
                            ledger_path,
                            transaction_id=transaction_id,
                            plan=plan,
                            authorization=authorization,
                            observed_after_sha256=observed_after,
                            reason_code="durability-unknown",
                        )
                        raise ControlledMarkdownCommitUnknownError(
                            "page bytes may be committed but durability is unknown",
                            transaction_id=transaction_id,
                        )

                    observed_after = _observe_knowledge_sha256(
                        knowledge_lease,
                        locked_target,
                    )
                    if observed_after != plan.output_sha256:
                        _append_commit_unknown_terminal(
                            machine_lease,
                            ledger_path,
                            transaction_id=transaction_id,
                            plan=plan,
                            authorization=authorization,
                            observed_after_sha256=observed_after,
                            reason_code="post-write-hash-mismatch",
                        )
                        raise ControlledMarkdownCommitUnknownError(
                            "published page did not expose the expected output hash",
                            transaction_id=transaction_id,
                        )

                    audit_sequence = _append_terminal(
                        machine_lease,
                        ledger_path,
                        transaction_id=transaction_id,
                        plan=plan,
                        authorization=authorization,
                        phase="committed",
                        observed_after_sha256=observed_after,
                        commit_state=write_result.commit_state,
                        reason_code=None,
                        markdown_may_be_committed=True,
                    )
                    return ControlledMarkdownWriteResult(
                        schema_version=CONTROLLED_MARKDOWN_PERSISTENCE_SCHEMA_VERSION,
                        kind=CONTROLLED_MARKDOWN_PERSISTENCE_KIND,
                        persistence_version=CONTROLLED_MARKDOWN_PERSISTENCE_VERSION,
                        outcome="committed",
                        project_id=plan.project_id,
                        path=plan.path,
                        plan_id=plan.plan_id,
                        authorization_id=authorization.authorization_id,
                        transaction_id=transaction_id,
                        created=plan.created,
                        previous_sha256=plan.current_sha256,
                        output_sha256=plan.output_sha256,
                        output_byte_count=plan.output_byte_count,
                        commit_state=write_result.commit_state,
                        audit_sequence=audit_sequence,
                    )
            except ControlledMarkdownCommitAuditUnknownError:
                raise
    except ControlledMarkdownPersistenceError:
        raise
    except (StableFileAccessError, LayoutError, OSError, TypeError, ValueError) as exc:
        raise ControlledMarkdownWriteRejectedError(
            "controlled Markdown persistence could not complete safely"
        ) from exc
