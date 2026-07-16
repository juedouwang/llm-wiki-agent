#!/usr/bin/env python3
"""Versioned B-06 two-axis state for inventoried research-project files.

``processing_status`` records whether deterministic processing succeeded, while
``read_depth`` records how much source content was read.  Keeping the axes
separate avoids treating a policy skip, an unsupported format, and a failed
read as the same condition.  Every state carries a stable reason code and a
human-readable reason, and unsupported/future records fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


FILE_STATE_SCHEMA_VERSION = 1
FILE_STATE_KIND = "llmwiki-file-state"

PROCESSING_STATUSES = (
    "discovered",
    "processed",
    "partial",
    "failed",
    "missing",
)
READ_DEPTHS = (
    "deep_read",
    "normal_read",
    "sampled",
    "metadata_only",
    "ignored",
    "unsupported",
)

_VALID_READ_DEPTHS = {
    "discovered": frozenset(
        {"sampled", "metadata_only", "ignored", "unsupported"}
    ),
    "processed": frozenset(READ_DEPTHS),
    "partial": frozenset(
        {"deep_read", "normal_read", "sampled", "metadata_only"}
    ),
    "failed": frozenset(
        {"deep_read", "normal_read", "sampled", "metadata_only"}
    ),
    "missing": frozenset({"metadata_only"}),
}
_REASON_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_POLICY_LIMIT_REASON_CODES = frozenset(
    {"sensitive-path", "content-size-limit", "outside-scan-boundary"}
)


@dataclass(frozen=True)
class FileState:
    """Auditable processing outcome and actual source-read depth."""

    processing_status: str
    read_depth: str
    reason_code: str
    reason: str

    def __post_init__(self) -> None:
        _validate_fields(
            self.processing_status,
            self.read_depth,
            self.reason_code,
            self.reason,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FILE_STATE_SCHEMA_VERSION,
            "kind": FILE_STATE_KIND,
            "processing_status": self.processing_status,
            "read_depth": self.read_depth,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


def _validate_fields(
    processing_status: object,
    read_depth: object,
    reason_code: object,
    reason: object,
) -> None:
    if processing_status not in PROCESSING_STATUSES:
        raise ValueError(f"unsupported processing_status {processing_status!r}")
    if read_depth not in READ_DEPTHS:
        raise ValueError(f"unsupported read_depth {read_depth!r}")
    assert isinstance(processing_status, str)
    assert isinstance(read_depth, str)
    if read_depth not in _VALID_READ_DEPTHS[processing_status]:
        raise ValueError(
            "invalid file-state combination: "
            f"processing_status={processing_status!r}, read_depth={read_depth!r}"
        )
    if not isinstance(reason_code, str) or not _REASON_CODE.fullmatch(reason_code):
        raise ValueError("file-state reason_code must be a stable kebab-case code")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("file-state reason must be a non-empty string")


def file_state_from_dict(value: object) -> FileState:
    """Validate and deserialize one persisted B-06 file-state object."""

    if not isinstance(value, dict):
        raise ValueError("file_state must be an object")
    expected_fields = {
        "schema_version",
        "kind",
        "processing_status",
        "read_depth",
        "reason_code",
        "reason",
    }
    if set(value) != expected_fields:
        raise ValueError("file_state must contain exactly the Schema v1 fields")
    if value.get("schema_version") != FILE_STATE_SCHEMA_VERSION:
        raise ValueError("file_state schema_version is missing, legacy, or unsupported")
    if value.get("kind") != FILE_STATE_KIND:
        raise ValueError(f"unexpected file_state kind {value.get('kind')!r}")
    return FileState(
        processing_status=value.get("processing_status"),
        read_depth=value.get("read_depth"),
        reason_code=value.get("reason_code"),
        reason=value.get("reason"),
    )


def inventory_file_state(
    format_value: str,
    *,
    content_access_reason_code: str | None = None,
    content_access_reason: str | None = None,
) -> FileState:
    """Return the truthful state produced by inventory/fingerprint/classification.

    A permitted B-05 classification prefix counts as ``sampled`` but not as a
    completed extraction.  B-02 policy restrictions remain explicit.  Unknown
    formats are recorded as unsupported rather than silently omitted.
    """

    if not isinstance(format_value, str) or not format_value:
        raise ValueError("format_value must be a non-empty string")
    restricted = content_access_reason_code is not None
    if restricted != (content_access_reason is not None):
        raise ValueError("content access reason code and detail must be provided together")
    if restricted:
        assert content_access_reason_code is not None
        assert content_access_reason is not None
        if not _REASON_CODE.fullmatch(content_access_reason_code):
            raise ValueError("content access reason code must be stable kebab-case")
        read_depth = (
            "ignored"
            if content_access_reason_code == "sensitive-path"
            else "metadata_only"
        )
        return FileState(
            processing_status="discovered",
            read_depth=read_depth,
            reason_code=content_access_reason_code,
            reason=content_access_reason,
        )
    if format_value == "unknown":
        return FileState(
            processing_status="discovered",
            read_depth="unsupported",
            reason_code="unsupported-format",
            reason=(
                "deterministic classification found no supported format; "
                "content extraction has not been attempted"
            ),
        )
    return FileState(
        processing_status="discovered",
        read_depth="sampled",
        reason_code="classification-sample",
        reason=(
            "a bounded local prefix was read for deterministic classification; "
            "content extraction has not been attempted"
        ),
    )


def state_is_compatible_with_inventory(
    previous: FileState,
    current_inventory_state: FileState,
) -> bool:
    """Whether an unchanged file may retain a previous state on re-inventory."""

    if current_inventory_state.reason_code in _POLICY_LIMIT_REASON_CODES:
        return previous == current_inventory_state
    if previous.reason_code in _POLICY_LIMIT_REASON_CODES:
        return False
    return previous.processing_status != "missing"
