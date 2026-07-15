#!/usr/bin/env python3
"""Deterministic, explainable project scan policy (roadmap task B-02).

This module defines *what may be traversed or read*. It deliberately does not
walk a project, create a Manifest, hash source files, extract content, or call
an LLM. Later roadmap tasks consume the policy objects defined here.
"""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal


SCAN_POLICY_SCHEMA_VERSION = 1
SCAN_POLICY_KIND = "llmwiki-scan-policy"
SCAN_POLICY_VERSION = "scan-policy-v1"
IGNORE_FILE_NAME = ".llmwikiignore"
MAX_IGNORE_FILE_BYTES = 1024 * 1024

DEFAULT_MAX_CONTENT_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_RAW_EXTERNAL_SEND_BYTES = 5 * 1024 * 1024

ExternalSendMode = Literal["local-only", "safe", "allowlist"]
RuleAction = Literal["include", "exclude"]
LocalContentAccess = Literal["allowed", "metadata_only", "blocked"]
RawExternalSend = Literal["allowed", "blocked"]

_PROTECTED_EXCLUDE_PATTERNS = (".git", ".hg", ".svn", ".llmwiki")

# Do not broadly exclude research-output names such as results/, data/, build/
# or dist/. These defaults target generated noise and dependency copies only.
_DEFAULT_EXCLUDE_PATTERNS = (
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".ipynb_checkpoints/",
    ".tox/",
    ".nox/",
    ".cache/",
    "node_modules/",
    ".venv/",
    "venv/",
    "env/",
    "__MACOSX/",
    ".DS_Store",
    "Thumbs.db",
    "*.pyc",
    "*.pyo",
    "*.swp",
    "~$*",
    "*-wiki/",
)

# Obvious secret containers are metadata-only and never approved for external
# send. Content-based secret detection remains separate later security work.
_DEFAULT_SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.*",
    "secret",
    "secret.*",
    "secrets",
    "secrets.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "**/.aws/credentials",
    "**/.kube/config",
    "**/.config/gcloud/application_default_credentials.json",
)

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_GLOB_MAGIC_RE = re.compile(r"[*?[]")


class ScanPolicyError(ValueError):
    """Base error for invalid scan-policy data or unsafe path usage."""


class ScanPolicyConfigError(ScanPolicyError):
    """Raised when policy configuration is invalid or contradictory."""


class ScanPolicyPathError(ScanPolicyError):
    """Raised when a path cannot safely be interpreted relative to a project."""


@dataclass(frozen=True)
class _NormalizedPattern:
    display: str
    core: str
    anchored: bool
    directory_only: bool


def _normalize_pattern(pattern: str, field: str) -> _NormalizedPattern:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ScanPolicyConfigError(f"{field} patterns must be non-empty strings")
    value = pattern.strip()
    if len(value) > 1024:
        raise ScanPolicyConfigError(f"{field} pattern is longer than 1024 characters")
    if any(ord(character) < 32 for character in value):
        raise ScanPolicyConfigError(f"{field} pattern contains a control character")

    value = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if value.startswith("//") or _WINDOWS_DRIVE_RE.match(value):
        raise ScanPolicyConfigError(
            f"{field} patterns must be project-relative, not absolute: {pattern!r}"
        )
    anchored = value.startswith("/")
    if anchored:
        value = value[1:]
    directory_only = value.endswith("/")
    if directory_only:
        value = value[:-1]
    if not value or value == "." or "//" in value:
        raise ScanPolicyConfigError(f"invalid {field} pattern: {pattern!r}")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ScanPolicyConfigError(
            f"{field} pattern cannot contain empty, '.' or '..' segments: {pattern!r}"
        )
    display = f"{'/' if anchored else ''}{value}{'/' if directory_only else ''}"
    return _NormalizedPattern(display, value, anchored, directory_only)


def _normalize_pattern_list(value: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ScanPolicyConfigError(f"{field} must be a sequence of patterns")
    try:
        raw_patterns = tuple(value)
    except TypeError as exc:
        raise ScanPolicyConfigError(f"{field} must be a sequence of patterns") from exc

    normalized: list[str] = []
    seen: set[str] = set()
    for pattern in raw_patterns:
        if isinstance(pattern, str) and pattern.startswith("!"):
            raise ScanPolicyConfigError(
                f"{field} already defines the action; remove leading '!': {pattern!r}"
            )
        canonical = _normalize_pattern(pattern, field).display
        if canonical not in seen:
            normalized.append(canonical)
            seen.add(canonical)
    return tuple(normalized)


def _validate_byte_limit(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScanPolicyConfigError(
            f"{field} must be a positive integer number of bytes"
        )
    return value


@dataclass(frozen=True)
class ScanPolicyConfig:
    """User-controlled inputs layered over safe, research-oriented defaults."""

    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    sensitive_patterns: tuple[str, ...] = ()
    external_include_patterns: tuple[str, ...] = ()
    external_exclude_patterns: tuple[str, ...] = ()
    max_content_file_bytes: int = DEFAULT_MAX_CONTENT_FILE_BYTES
    max_raw_external_send_bytes: int = DEFAULT_MAX_RAW_EXTERNAL_SEND_BYTES
    follow_symlinks: bool = False
    external_send_mode: ExternalSendMode = "local-only"
    case_sensitive: bool | None = None

    def __post_init__(self) -> None:
        pattern_fields = (
            "include_patterns",
            "exclude_patterns",
            "sensitive_patterns",
            "external_include_patterns",
            "external_exclude_patterns",
        )
        for field in pattern_fields:
            object.__setattr__(
                self,
                field,
                _normalize_pattern_list(getattr(self, field), field),
            )

        max_content = _validate_byte_limit(
            self.max_content_file_bytes, "max_content_file_bytes"
        )
        max_external = _validate_byte_limit(
            self.max_raw_external_send_bytes,
            "max_raw_external_send_bytes",
        )
        if max_external > max_content:
            raise ScanPolicyConfigError(
                "max_raw_external_send_bytes cannot exceed max_content_file_bytes"
            )
        if not isinstance(self.follow_symlinks, bool):
            raise ScanPolicyConfigError("follow_symlinks must be a boolean")
        if self.case_sensitive is not None and not isinstance(
            self.case_sensitive, bool
        ):
            raise ScanPolicyConfigError("case_sensitive must be true, false, or null")
        if self.external_send_mode not in {"local-only", "safe", "allowlist"}:
            raise ScanPolicyConfigError(
                "external_send_mode must be 'local-only', 'safe', or 'allowlist'"
            )
        if self.external_send_mode == "allowlist":
            if not self.external_include_patterns:
                raise ScanPolicyConfigError(
                    "external_send_mode 'allowlist' requires external_include_patterns"
                )
        elif self.external_include_patterns:
            raise ScanPolicyConfigError(
                "external_include_patterns require external_send_mode 'allowlist'"
            )

        boundary_conflicts = set(self.include_patterns) & set(self.exclude_patterns)
        if boundary_conflicts:
            conflict = sorted(boundary_conflicts)[0]
            raise ScanPolicyConfigError(
                f"the same pattern cannot be both included and excluded: {conflict!r}"
            )
        external_conflicts = set(self.external_include_patterns) & set(
            self.external_exclude_patterns
        )
        if external_conflicts:
            conflict = sorted(external_conflicts)[0]
            raise ScanPolicyConfigError(
                "the same pattern cannot be both externally allowed and denied: "
                f"{conflict!r}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "include_patterns": list(self.include_patterns),
            "exclude_patterns": list(self.exclude_patterns),
            "sensitive_patterns": list(self.sensitive_patterns),
            "external_include_patterns": list(self.external_include_patterns),
            "external_exclude_patterns": list(self.external_exclude_patterns),
            "max_content_file_bytes": self.max_content_file_bytes,
            "max_raw_external_send_bytes": self.max_raw_external_send_bytes,
            "follow_symlinks": self.follow_symlinks,
            "external_send_mode": self.external_send_mode,
            "case_sensitive": self.case_sensitive,
        }


def _segment_matches(value: str, pattern: str, *, case_sensitive: bool) -> bool:
    if not case_sensitive:
        value = value.casefold()
        pattern = pattern.casefold()
    return fnmatch.fnmatchcase(value, pattern)


def _glob_matches(
    path_segments: tuple[str, ...],
    pattern_segments: tuple[str, ...],
    *,
    case_sensitive: bool,
) -> bool:
    """Match slash-separated globs with ``**`` as a whole-segment wildcard."""

    if not case_sensitive:
        path_segments = tuple(segment.casefold() for segment in path_segments)
        pattern_segments = tuple(segment.casefold() for segment in pattern_segments)
    memo: dict[tuple[int, int], bool] = {}

    def matches(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_segments):
            result = path_index == len(path_segments)
        elif pattern_segments[pattern_index] == "**":
            result = matches(pattern_index + 1, path_index) or (
                path_index < len(path_segments)
                and matches(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_segments)
                and fnmatch.fnmatchcase(
                    path_segments[path_index], pattern_segments[pattern_index]
                )
                and matches(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return matches(0, 0)


@dataclass(frozen=True)
class PathRule:
    """One normalized path rule with stable provenance for explanations."""

    pattern: str
    action: RuleAction
    source: str
    line_number: int | None = None

    def __post_init__(self) -> None:
        normalized = _normalize_pattern(self.pattern, f"{self.source} rule")
        object.__setattr__(self, "pattern", normalized.display)
        if self.action not in {"include", "exclude"}:
            raise ScanPolicyConfigError(f"invalid rule action: {self.action!r}")
        if self.line_number is not None and self.line_number <= 0:
            raise ScanPolicyConfigError("rule line_number must be positive")

    @property
    def normalized(self) -> _NormalizedPattern:
        return _normalize_pattern(self.pattern, f"{self.source} rule")

    def matches(
        self,
        relative_path: str,
        *,
        is_directory: bool,
        case_sensitive: bool,
    ) -> bool:
        normalized_path = _normalize_relative_path(relative_path)
        path_segments = tuple(normalized_path.split("/"))
        rule = self.normalized
        pattern_segments = tuple(rule.core.split("/"))
        basename_rule = not rule.anchored and len(pattern_segments) == 1

        for end in range(1, len(path_segments) + 1):
            candidate_is_directory = end < len(path_segments) or is_directory
            if rule.directory_only and not candidate_is_directory:
                continue
            candidate_segments = path_segments[:end]
            if basename_rule:
                if _segment_matches(
                    candidate_segments[-1],
                    pattern_segments[0],
                    case_sensitive=case_sensitive,
                ):
                    return True
            elif _glob_matches(
                candidate_segments,
                pattern_segments,
                case_sensitive=case_sensitive,
            ):
                return True
        return False

    def could_match_descendant(self, directory: str, *, case_sensitive: bool) -> bool:
        """Conservatively report whether this include could match below a directory."""

        normalized_directory = _normalize_relative_path(directory)
        rule = self.normalized
        pattern_segments = tuple(rule.core.split("/"))
        if not rule.anchored and len(pattern_segments) == 1:
            return True

        prefix: list[str] = []
        for segment in pattern_segments:
            if segment == "**" or _GLOB_MAGIC_RE.search(segment):
                break
            prefix.append(segment)
        if not prefix:
            return True

        directory_segments = normalized_directory.split("/")
        if not case_sensitive:
            prefix = [segment.casefold() for segment in prefix]
            directory_segments = [segment.casefold() for segment in directory_segments]
        common_length = min(len(prefix), len(directory_segments))
        return prefix[:common_length] == directory_segments[:common_length]

    @property
    def reason_code(self) -> str:
        if self.source == "ignore-file":
            return f"ignore-{self.action}"
        return self.source

    def describe(self) -> str:
        location = f" line {self.line_number}" if self.line_number is not None else ""
        return f"{self.source}{location} {self.action} rule {self.pattern!r} matched"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "pattern": self.pattern,
            "action": self.action,
            "source": self.source,
        }
        if self.line_number is not None:
            result["line_number"] = self.line_number
        return result


def _normalize_relative_path(path: str | Path) -> str:
    value = unicodedata.normalize("NFC", str(path).replace("\\", "/"))
    if not value or value in {".", "/"}:
        raise ScanPolicyPathError("path must identify an entry below the project root")
    if (
        value.startswith("/")
        or value.startswith("//")
        or _WINDOWS_DRIVE_RE.match(value)
    ):
        raise ScanPolicyPathError(f"path must be project-relative: {path!r}")
    if any(ord(character) < 32 for character in value):
        raise ScanPolicyPathError("path contains a control character")
    while "//" in value:
        value = value.replace("//", "/")
    value = value.rstrip("/")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ScanPolicyPathError(
            f"path cannot contain empty, '.' or '..' segments: {path!r}"
        )
    return "/".join(segments)


def _parse_ignore_file(ignore_file: Path) -> tuple[PathRule, ...]:
    if ignore_file.is_symlink():
        raise ScanPolicyConfigError(f"{ignore_file} must not be a symbolic link")
    if not ignore_file.exists():
        return ()
    if not ignore_file.is_file():
        raise ScanPolicyConfigError(f"{ignore_file} must be a regular UTF-8 file")
    try:
        size = ignore_file.stat().st_size
    except OSError as exc:
        raise ScanPolicyConfigError(f"cannot stat {ignore_file}: {exc}") from exc
    if size > MAX_IGNORE_FILE_BYTES:
        raise ScanPolicyConfigError(
            f"{ignore_file} exceeds the {MAX_IGNORE_FILE_BYTES}-byte policy limit"
        )
    try:
        text = ignore_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ScanPolicyConfigError(
            f"cannot read {ignore_file} as UTF-8: {exc}"
        ) from exc

    rules: list[PathRule] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        action: RuleAction = "exclude"
        if line.startswith(r"\#") or line.startswith(r"\!"):
            line = line[1:]
        elif line.startswith("!"):
            action = "include"
            line = line[1:]
            if not line:
                raise ScanPolicyConfigError(
                    f"{ignore_file}:{line_number}: '!' must be followed by a pattern"
                )
        try:
            rules.append(
                PathRule(
                    pattern=line,
                    action=action,
                    source="ignore-file",
                    line_number=line_number,
                )
            )
        except ScanPolicyConfigError as exc:
            raise ScanPolicyConfigError(f"{ignore_file}:{line_number}: {exc}") from exc
    return tuple(rules)


def _rules(
    patterns: Iterable[str],
    *,
    action: RuleAction,
    source: str,
) -> tuple[PathRule, ...]:
    return tuple(
        PathRule(pattern=pattern, action=action, source=source) for pattern in patterns
    )


def _first_match(
    rules: Iterable[PathRule],
    relative_path: str,
    *,
    is_directory: bool,
    case_sensitive: bool,
) -> PathRule | None:
    for rule in rules:
        if rule.matches(
            relative_path,
            is_directory=is_directory,
            case_sensitive=case_sensitive,
        ):
            return rule
    return None


def _last_match(
    rules: Iterable[PathRule],
    relative_path: str,
    *,
    is_directory: bool,
    case_sensitive: bool,
) -> PathRule | None:
    matched: PathRule | None = None
    for rule in rules:
        if rule.matches(
            relative_path,
            is_directory=is_directory,
            case_sensitive=case_sensitive,
        ):
            matched = rule
    return matched


@dataclass(frozen=True)
class PathDecision:
    path: str
    included: bool
    traverse: bool
    reason_code: str
    reason: str
    matched_rule: PathRule | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "included": self.included,
            "traverse": self.traverse,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "matched_rule": (
                self.matched_rule.as_dict() if self.matched_rule is not None else None
            ),
        }


@dataclass(frozen=True)
class FilePolicyDecision:
    path: str
    size_bytes: int
    boundary: PathDecision
    sensitive: bool
    sensitive_rule: PathRule | None
    local_content_access: LocalContentAccess
    local_reason_code: str
    local_reason: str
    raw_external_send: RawExternalSend
    external_reason_code: str
    external_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "boundary": self.boundary.as_dict(),
            "sensitive": self.sensitive,
            "sensitive_rule": (
                self.sensitive_rule.as_dict()
                if self.sensitive_rule is not None
                else None
            ),
            "local_content_access": self.local_content_access,
            "local_reason_code": self.local_reason_code,
            "local_reason": self.local_reason,
            "raw_external_send": self.raw_external_send,
            "external_reason_code": self.external_reason_code,
            "external_reason": self.external_reason,
        }


@dataclass(frozen=True)
class SymlinkDecision:
    link_path: str
    follow: bool
    target_path: str | None
    reason_code: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "link_path": self.link_path,
            "follow": self.follow,
            "target_path": self.target_path,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


def _canonical_path_key(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(resolved)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ScanPolicy:
    """Resolved policy used by future inventory and extraction stages."""

    project_root: Path
    config: ScanPolicyConfig
    case_sensitive: bool
    ignore_file: Path | None
    protected_excludes: tuple[PathRule, ...]
    default_excludes: tuple[PathRule, ...]
    ignore_rules: tuple[PathRule, ...]
    explicit_includes: tuple[PathRule, ...]
    explicit_excludes: tuple[PathRule, ...]
    sensitive_rules: tuple[PathRule, ...]
    external_includes: tuple[PathRule, ...]
    external_excludes: tuple[PathRule, ...]

    def _possible_reinclude(self, directory: str) -> bool:
        include_rules = self.explicit_includes + tuple(
            rule for rule in self.ignore_rules if rule.action == "include"
        )
        return any(
            rule.could_match_descendant(
                directory,
                case_sensitive=self.case_sensitive,
            )
            for rule in include_rules
        )

    def decide_path(
        self, relative_path: str | Path, *, is_directory: bool
    ) -> PathDecision:
        """Resolve one path using the documented precedence and explain why."""

        if not isinstance(is_directory, bool):
            raise ScanPolicyPathError("is_directory must be a boolean")
        path = _normalize_relative_path(relative_path)

        protected = _first_match(
            self.protected_excludes,
            path,
            is_directory=is_directory,
            case_sensitive=self.case_sensitive,
        )
        if protected is not None:
            return PathDecision(
                path,
                False,
                False,
                protected.reason_code,
                protected.describe(),
                protected,
            )

        explicit_exclude = _first_match(
            self.explicit_excludes,
            path,
            is_directory=is_directory,
            case_sensitive=self.case_sensitive,
        )
        if explicit_exclude is not None:
            return PathDecision(
                path,
                False,
                False,
                explicit_exclude.reason_code,
                explicit_exclude.describe(),
                explicit_exclude,
            )

        explicit_include = _first_match(
            self.explicit_includes,
            path,
            is_directory=is_directory,
            case_sensitive=self.case_sensitive,
        )
        if explicit_include is not None:
            return PathDecision(
                path,
                True,
                is_directory,
                explicit_include.reason_code,
                explicit_include.describe(),
                explicit_include,
            )

        ignore_match = _last_match(
            self.ignore_rules,
            path,
            is_directory=is_directory,
            case_sensitive=self.case_sensitive,
        )
        if ignore_match is not None:
            included = ignore_match.action == "include"
            traverse = is_directory and (included or self._possible_reinclude(path))
            reason = ignore_match.describe()
            if traverse and not included:
                reason += "; traversal retained for a possible re-included descendant"
            return PathDecision(
                path,
                included,
                traverse,
                ignore_match.reason_code,
                reason,
                ignore_match,
            )

        default_exclude = _first_match(
            self.default_excludes,
            path,
            is_directory=is_directory,
            case_sensitive=self.case_sensitive,
        )
        if default_exclude is not None:
            traverse = is_directory and self._possible_reinclude(path)
            reason = default_exclude.describe()
            if traverse:
                reason += "; traversal retained for a possible re-included descendant"
            return PathDecision(
                path,
                False,
                traverse,
                default_exclude.reason_code,
                reason,
                default_exclude,
            )

        return PathDecision(
            path,
            True,
            is_directory,
            "default-include",
            "no protected, explicit, ignore-file, or default exclusion matched",
            None,
        )

    def decide_file(
        self,
        relative_path: str | Path,
        *,
        size_bytes: int,
    ) -> FilePolicyDecision:
        """Decide local raw-content access and raw external-send eligibility."""

        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise ScanPolicyPathError("size_bytes must be a non-negative integer")
        path = _normalize_relative_path(relative_path)
        boundary = self.decide_path(path, is_directory=False)
        sensitive_rule = _first_match(
            self.sensitive_rules,
            path,
            is_directory=False,
            case_sensitive=self.case_sensitive,
        )
        sensitive = sensitive_rule is not None

        if not boundary.included:
            local_access: LocalContentAccess = "blocked"
            local_code = "outside-scan-boundary"
            local_reason = boundary.reason
        elif sensitive:
            local_access = "metadata_only"
            local_code = "sensitive-path"
            local_reason = (
                f"sensitive path rule {sensitive_rule.pattern!r} matched; "
                "raw content is not read"
            )
        elif size_bytes > self.config.max_content_file_bytes:
            local_access = "metadata_only"
            local_code = "content-size-limit"
            local_reason = (
                f"file size {size_bytes} exceeds max_content_file_bytes "
                f"{self.config.max_content_file_bytes}"
            )
        else:
            local_access = "allowed"
            local_code = "local-content-allowed"
            local_reason = (
                "file is in scope, non-sensitive by path, and within size limit"
            )

        external_exclude = _first_match(
            self.external_excludes,
            path,
            is_directory=False,
            case_sensitive=self.case_sensitive,
        )
        external_include = _first_match(
            self.external_includes,
            path,
            is_directory=False,
            case_sensitive=self.case_sensitive,
        )

        if not boundary.included:
            raw_external: RawExternalSend = "blocked"
            external_code = "outside-scan-boundary"
            external_reason = boundary.reason
        elif sensitive:
            raw_external = "blocked"
            external_code = "sensitive-path"
            external_reason = (
                "sensitive-path raw content is never approved for external send"
            )
        elif self.config.external_send_mode == "local-only":
            raw_external = "blocked"
            external_code = "external-local-only"
            external_reason = "external_send_mode is local-only"
        elif external_exclude is not None:
            raw_external = "blocked"
            external_code = "external-explicit-exclude"
            external_reason = external_exclude.describe()
        elif size_bytes > self.config.max_raw_external_send_bytes:
            raw_external = "blocked"
            external_code = "external-size-limit"
            external_reason = (
                f"file size {size_bytes} exceeds max_raw_external_send_bytes "
                f"{self.config.max_raw_external_send_bytes}"
            )
        elif self.config.external_send_mode == "allowlist" and external_include is None:
            raw_external = "blocked"
            external_code = "external-not-allowlisted"
            external_reason = "path did not match an external allowlist rule"
        else:
            raw_external = "allowed"
            external_code = "raw-external-send-allowed"
            external_reason = (
                "non-sensitive in-scope raw file is within the external size limit"
            )

        return FilePolicyDecision(
            path=path,
            size_bytes=size_bytes,
            boundary=boundary,
            sensitive=sensitive,
            sensitive_rule=sensitive_rule,
            local_content_access=local_access,
            local_reason_code=local_code,
            local_reason=local_reason,
            raw_external_send=raw_external,
            external_reason_code=external_code,
            external_reason=external_reason,
        )

    def assess_symlink_target(
        self,
        link_path: str | Path,
        resolved_target: str | Path,
        *,
        ancestor_realpaths: Iterable[str | Path] = (),
        visited_realpaths: Iterable[str | Path] = (),
    ) -> SymlinkDecision:
        """Assess an already-resolved target without performing directory traversal."""

        link = _normalize_relative_path(link_path)
        target = Path(resolved_target).expanduser().resolve(strict=False)
        if not self.config.follow_symlinks:
            return SymlinkDecision(
                link,
                False,
                str(target),
                "symlink-follow-disabled",
                "follow_symlinks is false; the link is recorded but not followed",
            )
        if not _is_within(target, self.project_root):
            return SymlinkDecision(
                link,
                False,
                str(target),
                "symlink-target-outside-project",
                "symbolic-link target is outside the registered project root",
            )

        target_key = _canonical_path_key(target)
        ancestor_keys = {_canonical_path_key(path) for path in ancestor_realpaths}
        if target_key in ancestor_keys:
            return SymlinkDecision(
                link,
                False,
                str(target),
                "symlink-cycle",
                "symbolic-link target is already in the active ancestor chain",
            )
        visited_keys = {_canonical_path_key(path) for path in visited_realpaths}
        if target_key in visited_keys:
            return SymlinkDecision(
                link,
                False,
                str(target),
                "symlink-target-already-visited",
                "symbolic-link target was already visited through another path",
            )
        return SymlinkDecision(
            link,
            True,
            str(target),
            "symlink-follow-safe",
            "target is inside the project and is not an ancestor or prior visit",
        )

    def decide_symlink(
        self,
        link_path: str | Path,
        *,
        ancestor_realpaths: Iterable[str | Path] = (),
        visited_realpaths: Iterable[str | Path] = (),
    ) -> SymlinkDecision:
        """Resolve and assess a real symlink, failing closed on broken links/cycles."""

        relative = _normalize_relative_path(link_path)
        link = self.project_root.joinpath(*relative.split("/"))
        if not link.is_symlink():
            raise ScanPolicyPathError(f"path is not a symbolic link: {relative}")
        if not self.config.follow_symlinks:
            return SymlinkDecision(
                relative,
                False,
                None,
                "symlink-follow-disabled",
                "follow_symlinks is false; the link is recorded but not followed",
            )
        try:
            target = link.resolve(strict=True)
        except RuntimeError:
            return SymlinkDecision(
                relative,
                False,
                None,
                "symlink-cycle",
                "symbolic-link resolution detected a cycle",
            )
        except OSError as exc:
            return SymlinkDecision(
                relative,
                False,
                None,
                "symlink-broken-or-unreadable",
                f"symbolic-link target could not be resolved: {exc}",
            )
        return self.assess_symlink_target(
            relative,
            target,
            ancestor_realpaths=ancestor_realpaths,
            visited_realpaths=visited_realpaths,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCAN_POLICY_SCHEMA_VERSION,
            "kind": SCAN_POLICY_KIND,
            "policy_version": SCAN_POLICY_VERSION,
            "project_root": str(self.project_root),
            "ignore_file": (
                str(self.ignore_file.relative_to(self.project_root))
                if self.ignore_file is not None
                else None
            ),
            "effective_case_sensitive": self.case_sensitive,
            "config": self.config.as_dict(),
            "rules": {
                "protected_excludes": [
                    rule.as_dict() for rule in self.protected_excludes
                ],
                "default_excludes": [rule.as_dict() for rule in self.default_excludes],
                "ignore_file": [rule.as_dict() for rule in self.ignore_rules],
                "explicit_includes": [
                    rule.as_dict() for rule in self.explicit_includes
                ],
                "explicit_excludes": [
                    rule.as_dict() for rule in self.explicit_excludes
                ],
                "sensitive": [rule.as_dict() for rule in self.sensitive_rules],
                "external_includes": [
                    rule.as_dict() for rule in self.external_includes
                ],
                "external_excludes": [
                    rule.as_dict() for rule in self.external_excludes
                ],
            },
        }


def load_scan_policy(
    project_root: str | Path,
    *,
    config: ScanPolicyConfig | None = None,
) -> ScanPolicy:
    """Load a project policy without writing to the source or assistant workspace."""

    root = Path(project_root).expanduser().resolve()
    if not root.exists():
        raise ScanPolicyPathError(f"project root does not exist: {root}")
    if not root.is_dir():
        raise ScanPolicyPathError(f"project root is not a directory: {root}")
    selected_config = ScanPolicyConfig() if config is None else config
    if not isinstance(selected_config, ScanPolicyConfig):
        raise ScanPolicyConfigError("config must be a ScanPolicyConfig instance")
    case_sensitive = (
        selected_config.case_sensitive
        if selected_config.case_sensitive is not None
        else os.name != "nt"
    )

    ignore_path = root / IGNORE_FILE_NAME
    ignore_rules = _parse_ignore_file(ignore_path)
    explicit_sensitive = tuple(
        pattern
        for pattern in selected_config.sensitive_patterns
        if pattern not in _DEFAULT_SENSITIVE_PATTERNS
    )
    return ScanPolicy(
        project_root=root,
        config=selected_config,
        case_sensitive=case_sensitive,
        ignore_file=ignore_path if ignore_path.is_file() else None,
        protected_excludes=_rules(
            _PROTECTED_EXCLUDE_PATTERNS,
            action="exclude",
            source="protected-exclude",
        ),
        default_excludes=_rules(
            _DEFAULT_EXCLUDE_PATTERNS,
            action="exclude",
            source="default-exclude",
        ),
        ignore_rules=ignore_rules,
        explicit_includes=_rules(
            selected_config.include_patterns,
            action="include",
            source="explicit-include",
        ),
        explicit_excludes=_rules(
            selected_config.exclude_patterns,
            action="exclude",
            source="explicit-exclude",
        ),
        sensitive_rules=(
            _rules(
                _DEFAULT_SENSITIVE_PATTERNS,
                action="exclude",
                source="sensitive-default",
            )
            + _rules(
                explicit_sensitive,
                action="exclude",
                source="sensitive-explicit",
            )
        ),
        external_includes=_rules(
            selected_config.external_include_patterns,
            action="include",
            source="external-include",
        ),
        external_excludes=_rules(
            selected_config.external_exclude_patterns,
            action="exclude",
            source="external-exclude",
        ),
    )
