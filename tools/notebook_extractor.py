#!/usr/bin/env python3
"""Deterministic C-03 extraction for Jupyter Notebook files."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from .extraction_schema import (
        Block,
        ExtractedDocument,
        ExtractionResult,
        NotebookCellLocator,
    )
else:
    from extraction_schema import (  # type: ignore[no-redef]
        Block,
        ExtractedDocument,
        ExtractionResult,
        NotebookCellLocator,
    )


NOTEBOOK_EXTRACTOR_NAME = "deterministic-notebook"
NOTEBOOK_EXTRACTOR_VERSION = "1"
SUPPORTED_NOTEBOOK_FORMATS = frozenset({"notebook"})

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SUPPORTED_CELL_TYPES = frozenset({"code", "markdown", "raw"})
_PREVIEWABLE_MIME_TYPES = frozenset(
    {
        "application/json",
        "text/markdown",
        "text/plain",
    }
)
_RICH_MIME_TYPES = frozenset(
    {
        "application/javascript",
        "image/svg+xml",
        "text/html",
    }
)


class _NotebookValidationError(ValueError):
    """Raised when a Notebook JSON value violates the supported v4 shape."""


class _UnsupportedNotebookVersion(ValueError):
    """Raised when a syntactically valid Notebook uses an unsupported version."""


@dataclass(frozen=True)
class NotebookExtractionLimits:
    """Deterministic safety bounds for one Notebook source."""

    max_source_bytes: int = 64 * 1024 * 1024
    max_cell_characters: int = 512 * 1024
    max_output_text_characters: int = 16 * 1024
    max_output_summary_characters: int = 128 * 1024
    max_outputs_per_cell: int = 100
    max_mime_entries_per_output: int = 32
    max_attachments_per_cell: int = 32
    max_mime_entries_per_attachment: int = 32
    read_chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for field_name in (
            "max_source_bytes",
            "max_cell_characters",
            "max_output_text_characters",
            "max_output_summary_characters",
            "max_outputs_per_cell",
            "max_mime_entries_per_output",
            "max_attachments_per_cell",
            "max_mime_entries_per_attachment",
            "read_chunk_bytes",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_source_bytes < 4:
            raise ValueError("max_source_bytes must be at least 4")


@dataclass(frozen=True)
class _ReadResult:
    data: bytes
    content_sha256: str
    source_size_bytes: int


@dataclass(frozen=True)
class _DecodedNotebook:
    value: dict[str, Any]
    encoding: str


@dataclass(frozen=True)
class _CellBlocks:
    blocks: tuple[Block, ...]
    cell_type: str
    has_cell_id: bool
    truncation_reasons: tuple[str, ...]


def _unsupported_result(format_value: object) -> ExtractionResult:
    return ExtractionResult(
        status="unsupported",
        reason_code="unsupported-notebook-format",
        reason=f"format {format_value!r} is not the C-03 Notebook format",
        document=None,
    )


def _failed_result(
    reason_code: str,
    reason: str,
    *diagnostics: str,
) -> ExtractionResult:
    return ExtractionResult(
        status="failed",
        reason_code=reason_code,
        reason=reason,
        document=None,
        diagnostics=tuple(item for item in diagnostics if item),
    )


def _read_and_hash(path: Path, limits: NotebookExtractionLimits) -> _ReadResult:
    digest = hashlib.sha256()
    retained = bytearray()
    source_size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(limits.read_chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
            source_size += len(chunk)
            remaining = limits.max_source_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
    return _ReadResult(
        data=bytes(retained),
        content_sha256=digest.hexdigest(),
        source_size_bytes=source_size,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _NotebookValidationError(f"duplicate Notebook JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise _NotebookValidationError(
        f"non-finite Notebook JSON constant {value!r} is not supported"
    )


def _decode_notebook(data: bytes) -> _DecodedNotebook:
    encoding = "utf-8-sig" if data.startswith(codecs.BOM_UTF8) else "utf-8"
    text = data.decode(encoding, errors="strict")
    value = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise _NotebookValidationError("Notebook root must be a JSON object")
    return _DecodedNotebook(value=value, encoding=encoding)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _required_integer(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
) -> int:
    if not _is_integer(value) or value < minimum:
        raise _NotebookValidationError(
            f"{field_name} must be an integer >= {minimum}"
        )
    return value


def _optional_execution_count(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _required_integer(value, field_name)


def _string_or_lines(value: object, field_name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "".join(value)
    raise _NotebookValidationError(
        f"{field_name} must be a string or an array of strings"
    )


def _metadata_keys(value: object, field_name: str) -> list[str]:
    if not isinstance(value, dict):
        raise _NotebookValidationError(f"{field_name} must be an object")
    return sorted(value)


def _stable_json_sha256(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _reason_detail(reason_code: str) -> str:
    details = {
        "attachment-count-limit": (
            "cell attachments exceeded max_attachments_per_cell"
        ),
        "attachment-mime-entry-limit": (
            "an attachment MIME bundle exceeded max_mime_entries_per_attachment"
        ),
        "binary-output-omitted": (
            "a binary output payload was omitted from the deterministic summary"
        ),
        "cell-attachment-omitted": (
            "cell attachment payloads were omitted from the source block"
        ),
        "cell-character-limit": "cell source exceeded max_cell_characters",
        "output-count-limit": "cell outputs exceeded max_outputs_per_cell",
        "output-mime-entry-limit": (
            "an output MIME bundle exceeded max_mime_entries_per_output"
        ),
        "output-summary-character-limit": (
            "rendered output summary exceeded max_output_summary_characters"
        ),
        "output-text-character-limit": (
            "textual output exceeded max_output_text_characters"
        ),
        "rich-output-omitted": (
            "an active or rich output payload was omitted from the deterministic summary"
        ),
    }
    try:
        return details[reason_code]
    except KeyError as exc:
        raise AssertionError(f"unknown Notebook truncation reason {reason_code}") from exc


def _truncation_fields(
    reason_codes: set[str],
    *,
    multiple_code: str,
    multiple_reason: str,
) -> tuple[bool, str | None, str | None, list[dict[str, str]]]:
    ordered = sorted(reason_codes)
    records = [
        {"code": reason_code, "reason": _reason_detail(reason_code)}
        for reason_code in ordered
    ]
    if len(ordered) == 1:
        return True, ordered[0], _reason_detail(ordered[0]), records
    if len(ordered) > 1:
        return True, multiple_code, multiple_reason, records
    return False, None, None, records


def _cell_block_type(cell_type: str) -> str:
    if cell_type == "code":
        return "code"
    if cell_type == "markdown":
        return "markdown"
    return "text"


def _attachment_metadata(
    cell: dict[str, Any],
    cell_index: int,
    limits: NotebookExtractionLimits,
    reason_codes: set[str],
) -> dict[str, Any]:
    attachments = cell.get("attachments", {})
    if attachments is None:
        attachments = {}
    if not isinstance(attachments, dict):
        raise _NotebookValidationError(
            f"cells[{cell_index}].attachments must be an object"
        )
    ordered_names = sorted(attachments)
    for attachment_name in ordered_names:
        bundle = attachments[attachment_name]
        if not isinstance(attachment_name, str) or not attachment_name:
            raise _NotebookValidationError(
                f"cells[{cell_index}].attachments names must be non-empty strings"
            )
        if not isinstance(bundle, dict):
            raise _NotebookValidationError(
                f"cells[{cell_index}].attachments must map names to MIME bundles"
            )
    included_names = ordered_names[: limits.max_attachments_per_cell]
    if attachments:
        reason_codes.add("cell-attachment-omitted")
    if len(ordered_names) > len(included_names):
        reason_codes.add("attachment-count-limit")

    mime_types: set[str] = set()
    summaries: dict[str, Any] = {}
    for attachment_name in included_names:
        bundle = attachments[attachment_name]
        assert isinstance(bundle, dict)
        ordered_mime_types = sorted(bundle)
        included_mime_types = ordered_mime_types[
            : limits.max_mime_entries_per_attachment
        ]
        if len(ordered_mime_types) > len(included_mime_types):
            reason_codes.add("attachment-mime-entry-limit")
        items: dict[str, Any] = {}
        for mime_type in included_mime_types:
            if not isinstance(mime_type, str) or not mime_type:
                raise _NotebookValidationError(
                    f"cells[{cell_index}].attachments[{attachment_name!r}] "
                    "MIME keys must be non-empty strings"
                )
            text = _mime_value_text(
                bundle[mime_type],
                f"cells[{cell_index}].attachments[{attachment_name!r}].{mime_type}",
            )
            mime_types.add(mime_type)
            items[mime_type] = {
                "character_count": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "payload_omitted": True,
            }
        summaries[attachment_name] = {
            "mime_count": len(ordered_mime_types),
            "included_mime_count": len(included_mime_types),
            "items": items,
        }
    return {
        "attachment_count": len(ordered_names),
        "included_attachment_count": len(included_names),
        "attachment_names": included_names,
        "attachment_mime_types": sorted(mime_types),
        "attachment_summaries": summaries,
    }


def _summarize_text(
    value: object,
    field_name: str,
    limits: NotebookExtractionLimits,
    reason_codes: set[str],
) -> dict[str, Any]:
    text = _string_or_lines(value, field_name)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    truncated = len(text) > limits.max_output_text_characters
    if truncated:
        reason_codes.add("output-text-character-limit")
    return {
        "character_count": len(text),
        "sha256": digest,
        "preview": text[: limits.max_output_text_characters],
        "truncated": truncated,
    }


def _mime_payload_policy(mime_type: str) -> str:
    if mime_type in _PREVIEWABLE_MIME_TYPES or mime_type.endswith("+json"):
        return "preview"
    if mime_type.startswith("text/") or mime_type in _RICH_MIME_TYPES:
        return "rich"
    return "binary"


def _mime_value_text(value: object, field_name: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "".join(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _NotebookValidationError(
            f"{field_name} must contain JSON-compatible MIME data"
        ) from exc


def _summarize_mime_bundle(
    value: object,
    field_name: str,
    limits: NotebookExtractionLimits,
    reason_codes: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _NotebookValidationError(f"{field_name} must be an object")
    mime_types = sorted(value)
    included = mime_types[: limits.max_mime_entries_per_output]
    if len(mime_types) > len(included):
        reason_codes.add("output-mime-entry-limit")
    records: dict[str, Any] = {}
    for mime_type in included:
        if not isinstance(mime_type, str) or not mime_type:
            raise _NotebookValidationError(
                f"{field_name} MIME keys must be non-empty strings"
            )
        text = _mime_value_text(value[mime_type], f"{field_name}.{mime_type}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        policy = _mime_payload_policy(mime_type)
        record: dict[str, Any] = {
            "character_count": len(text),
            "sha256": digest,
            "payload_omitted": policy != "preview",
        }
        if policy == "preview":
            truncated = len(text) > limits.max_output_text_characters
            if truncated:
                reason_codes.add("output-text-character-limit")
            record.update(
                {
                    "preview": text[: limits.max_output_text_characters],
                    "truncated": truncated,
                }
            )
        elif policy == "rich":
            reason_codes.add("rich-output-omitted")
        else:
            reason_codes.add("binary-output-omitted")
        records[mime_type] = record
    return {
        "mime_count": len(mime_types),
        "included_mime_count": len(included),
        "items": records,
    }


def _summarize_output(
    value: object,
    *,
    cell_index: int,
    output_index: int,
    limits: NotebookExtractionLimits,
    reason_codes: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _NotebookValidationError(
            f"cells[{cell_index}].outputs[{output_index}] must be an object"
        )
    output_type = value.get("output_type")
    field_prefix = f"cells[{cell_index}].outputs[{output_index}]"
    record: dict[str, Any] = {
        "output_index": output_index,
        "output_type": output_type,
    }
    if output_type == "stream":
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise _NotebookValidationError(f"{field_prefix}.name must be text")
        record["name"] = name
        record["text"] = _summarize_text(
            value.get("text", ""),
            f"{field_prefix}.text",
            limits,
            reason_codes,
        )
        return record
    if output_type in {"display_data", "execute_result"}:
        record["data"] = _summarize_mime_bundle(
            value.get("data", {}),
            f"{field_prefix}.data",
            limits,
            reason_codes,
        )
        record["metadata_keys"] = _metadata_keys(
            value.get("metadata", {}),
            f"{field_prefix}.metadata",
        )
        if output_type == "execute_result":
            record["execution_count"] = _optional_execution_count(
                value.get("execution_count"),
                f"{field_prefix}.execution_count",
            )
        return record
    if output_type == "error":
        ename = value.get("ename")
        evalue = value.get("evalue")
        if not isinstance(ename, str) or not isinstance(evalue, str):
            raise _NotebookValidationError(
                f"{field_prefix} error name and value must be strings"
            )
        record["ename"] = ename
        record["evalue"] = _summarize_text(
            evalue,
            f"{field_prefix}.evalue",
            limits,
            reason_codes,
        )
        record["traceback"] = _summarize_text(
            value.get("traceback", []),
            f"{field_prefix}.traceback",
            limits,
            reason_codes,
        )
        return record
    raise _NotebookValidationError(
        f"{field_prefix}.output_type {output_type!r} is unsupported"
    )


def _output_block(
    outputs: object,
    *,
    cell_index: int,
    cell_id: str | None,
    execution_count: int | None,
    limits: NotebookExtractionLimits,
) -> Block | None:
    if not isinstance(outputs, list):
        raise _NotebookValidationError(
            f"cells[{cell_index}].outputs must be an array"
        )
    if not outputs:
        return None
    reason_codes: set[str] = set()
    included_outputs = outputs[: limits.max_outputs_per_cell]
    if len(outputs) > len(included_outputs):
        reason_codes.add("output-count-limit")
    summaries = [
        _summarize_output(
            output,
            cell_index=cell_index,
            output_index=output_index,
            limits=limits,
            reason_codes=reason_codes,
        )
        for output_index, output in enumerate(included_outputs)
    ]
    rendered = (
        json.dumps(
            summaries,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    full_summary_characters = len(rendered)
    full_summary_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if len(rendered) > limits.max_output_summary_characters:
        reason_codes.add("output-summary-character-limit")
        rendered = rendered[: limits.max_output_summary_characters]
    truncated, primary_code, primary_reason, reason_records = _truncation_fields(
        reason_codes,
        multiple_code="multiple-output-limits",
        multiple_reason="multiple deterministic output limits were applied",
    )
    return Block(
        block_id=f"cell-{cell_index}-outputs",
        block_type="output_summary",
        text=rendered,
        locator=NotebookCellLocator(cell_index, cell_id),
        metadata={
            "cell_index": cell_index,
            "cell_id": cell_id,
            "execution_count": execution_count,
            "output_count": len(outputs),
            "included_output_count": len(included_outputs),
            "full_summary_characters": full_summary_characters,
            "full_summary_sha256": full_summary_sha256,
            "truncation_reasons": reason_records,
        },
        truncated=truncated,
        truncation_reason_code=primary_code,
        truncation_reason=primary_reason,
    )


def _cell_blocks(
    value: object,
    *,
    cell_index: int,
    seen_cell_ids: set[str],
    limits: NotebookExtractionLimits,
) -> _CellBlocks:
    if not isinstance(value, dict):
        raise _NotebookValidationError(f"cells[{cell_index}] must be an object")
    cell_type = value.get("cell_type")
    if cell_type not in _SUPPORTED_CELL_TYPES:
        raise _NotebookValidationError(
            f"cells[{cell_index}].cell_type {cell_type!r} is unsupported"
        )
    cell_id = value.get("id")
    if cell_id is not None:
        if not isinstance(cell_id, str) or not cell_id.strip():
            raise _NotebookValidationError(
                f"cells[{cell_index}].id must be non-empty text"
            )
        if cell_id in seen_cell_ids:
            raise _NotebookValidationError(f"duplicate Notebook cell id {cell_id!r}")
        seen_cell_ids.add(cell_id)
    source = _string_or_lines(value.get("source", ""), f"cells[{cell_index}].source")
    metadata_keys = _metadata_keys(
        value.get("metadata", {}),
        f"cells[{cell_index}].metadata",
    )
    execution_count: int | None = None
    outputs: object = []
    output_count = 0
    if cell_type == "code":
        execution_count = _optional_execution_count(
            value.get("execution_count"),
            f"cells[{cell_index}].execution_count",
        )
        outputs = value.get("outputs", [])
        if not isinstance(outputs, list):
            raise _NotebookValidationError(
                f"cells[{cell_index}].outputs must be an array"
            )
        output_count = len(outputs)
    source_reason_codes: set[str] = set()
    attachment_metadata = _attachment_metadata(
        value,
        cell_index,
        limits,
        source_reason_codes,
    )
    if len(source) > limits.max_cell_characters:
        source_reason_codes.add("cell-character-limit")
    source_text = source[: limits.max_cell_characters]
    truncated, primary_code, primary_reason, reason_records = _truncation_fields(
        source_reason_codes,
        multiple_code="multiple-cell-limits",
        multiple_reason="multiple deterministic cell limits were applied",
    )
    source_metadata = {
        "cell_index": cell_index,
        "cell_id": cell_id,
        "cell_type": cell_type,
        "execution_count": execution_count,
        "output_count": output_count,
        "metadata_keys": metadata_keys,
        "source_character_count": len(source),
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "source_line_count": len(source.splitlines()),
        "truncation_reasons": reason_records,
        **attachment_metadata,
    }
    blocks: list[Block] = [
        Block(
            block_id=f"cell-{cell_index}-source",
            block_type=_cell_block_type(cell_type),
            text=source_text,
            locator=NotebookCellLocator(cell_index, cell_id),
            metadata=source_metadata,
            truncated=truncated,
            truncation_reason_code=primary_code,
            truncation_reason=primary_reason,
        )
    ]
    if cell_type == "code":
        output = _output_block(
            outputs,
            cell_index=cell_index,
            cell_id=cell_id,
            execution_count=execution_count,
            limits=limits,
        )
        if output is not None:
            blocks.append(output)
    truncation_reasons = {
        reason["code"]
        for block in blocks
        for reason in block.metadata["truncation_reasons"]
    }
    return _CellBlocks(
        blocks=tuple(blocks),
        cell_type=cell_type,
        has_cell_id=cell_id is not None,
        truncation_reasons=tuple(sorted(truncation_reasons)),
    )


def _build_document(
    decoded: _DecodedNotebook,
    *,
    relative_path: str,
    content_sha256: str,
    source_size_bytes: int,
    limits: NotebookExtractionLimits,
) -> ExtractionResult:
    notebook = decoded.value
    nbformat = _required_integer(notebook.get("nbformat"), "nbformat", minimum=1)
    nbformat_minor = _required_integer(
        notebook.get("nbformat_minor"),
        "nbformat_minor",
    )
    if nbformat != 4:
        raise _UnsupportedNotebookVersion(
            f"Notebook nbformat {nbformat} is unsupported; only nbformat 4 is supported"
        )
    notebook_metadata = notebook.get("metadata", {})
    notebook_metadata_keys = _metadata_keys(notebook_metadata, "metadata")
    notebook_metadata_sha256 = _stable_json_sha256(notebook_metadata)
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise _NotebookValidationError("cells must be an array")
    seen_cell_ids: set[str] = set()
    all_blocks: list[Block] = []
    cell_type_counts = {cell_type: 0 for cell_type in sorted(_SUPPORTED_CELL_TYPES)}
    truncation_reasons: set[str] = set()
    cells_with_ids = 0
    for cell_index, cell in enumerate(cells):
        extracted = _cell_blocks(
            cell,
            cell_index=cell_index,
            seen_cell_ids=seen_cell_ids,
            limits=limits,
        )
        all_blocks.extend(extracted.blocks)
        cell_type_counts[extracted.cell_type] += 1
        cells_with_ids += int(extracted.has_cell_id)
        truncation_reasons.update(extracted.truncation_reasons)
    partial = bool(truncation_reasons)
    document = ExtractedDocument(
        path=relative_path,
        content_sha256=content_sha256,
        format="notebook",
        extractor=NOTEBOOK_EXTRACTOR_NAME,
        extractor_version=NOTEBOOK_EXTRACTOR_VERSION,
        encoding=decoded.encoding,
        blocks=tuple(all_blocks),
        metadata={
            "source_size_bytes": source_size_bytes,
            "nbformat": nbformat,
            "nbformat_minor": nbformat_minor,
            "notebook_metadata_keys": notebook_metadata_keys,
            "notebook_metadata_sha256": notebook_metadata_sha256,
            "cell_count": len(cells),
            "cell_type_counts": cell_type_counts,
            "cells_with_ids": cells_with_ids,
            "block_count": len(all_blocks),
            "output_summary_block_count": sum(
                block.block_type == "output_summary" for block in all_blocks
            ),
            "truncation_reasons": sorted(truncation_reasons),
        },
    )
    return ExtractionResult(
        status="partial" if partial else "processed",
        reason_code=(
            "deterministic-notebook-partial"
            if partial
            else "deterministic-notebook-extracted"
        ),
        reason=(
            "Notebook cells were extracted with deterministic content limits"
            if partial
            else "all Notebook cells were deterministically extracted"
        ),
        document=document,
        diagnostics=tuple(
            _reason_detail(reason_code) for reason_code in sorted(truncation_reasons)
        ),
    )


def _extract_notebook_data(
    data: bytes,
    *,
    relative_path: str,
    content_sha256: str,
    source_size_bytes: int,
    limits: NotebookExtractionLimits,
) -> ExtractionResult:
    if source_size_bytes > limits.max_source_bytes:
        return _failed_result(
            "notebook-source-byte-limit",
            "the Notebook exceeds the deterministic source byte limit",
            f"source has {source_size_bytes} bytes; limit is {limits.max_source_bytes}",
            f"content SHA-256 is {content_sha256}",
        )
    try:
        decoded = _decode_notebook(data)
    except UnicodeDecodeError as exc:
        return _failed_result(
            "notebook-decoding-failed",
            "the Notebook is not strict UTF-8 JSON",
            str(exc),
        )
    except (json.JSONDecodeError, _NotebookValidationError, RecursionError) as exc:
        return _failed_result(
            "notebook-json-invalid",
            "the Notebook JSON could not be parsed safely",
            str(exc),
        )
    try:
        return _build_document(
            decoded,
            relative_path=relative_path,
            content_sha256=content_sha256,
            source_size_bytes=source_size_bytes,
            limits=limits,
        )
    except _UnsupportedNotebookVersion as exc:
        return ExtractionResult(
            status="unsupported",
            reason_code="unsupported-notebook-version",
            reason=str(exc),
            document=None,
        )
    except _NotebookValidationError as exc:
        return _failed_result(
            "notebook-structure-invalid",
            "the Notebook structure violates the supported deterministic contract",
            str(exc),
        )


def extract_notebook_bytes(
    data: bytes,
    *,
    relative_path: str,
    format_value: str = "notebook",
    limits: NotebookExtractionLimits | None = None,
) -> ExtractionResult:
    """Extract one in-memory Notebook without execution or external services."""

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if format_value not in SUPPORTED_NOTEBOOK_FORMATS:
        return _unsupported_result(format_value)
    chosen_limits = limits or NotebookExtractionLimits()
    return _extract_notebook_data(
        data[: chosen_limits.max_source_bytes],
        relative_path=relative_path,
        content_sha256=hashlib.sha256(data).hexdigest(),
        source_size_bytes=len(data),
        limits=chosen_limits,
    )


def extract_notebook_file(
    source_path: str | Path,
    *,
    relative_path: str,
    format_value: str = "notebook",
    expected_sha256: str | None = None,
    limits: NotebookExtractionLimits | None = None,
) -> ExtractionResult:
    """Read one Notebook without writing it and return the C-01 result Schema."""

    if format_value not in SUPPORTED_NOTEBOOK_FORMATS:
        return _unsupported_result(format_value)
    chosen_limits = limits or NotebookExtractionLimits()
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise ValueError(
            "expected_sha256 must be 64 lowercase hexadecimal characters"
        )
    try:
        source = _read_and_hash(Path(source_path), chosen_limits)
    except OSError as exc:
        return _failed_result(
            "source-read-failed",
            "the Notebook source file could not be read",
            str(exc),
        )
    if expected_sha256 is not None and source.content_sha256 != expected_sha256:
        return _failed_result(
            "content-hash-mismatch",
            "the Notebook content no longer matches the requested file version",
            f"expected {expected_sha256}; observed {source.content_sha256}",
        )
    return _extract_notebook_data(
        source.data,
        relative_path=relative_path,
        content_sha256=source.content_sha256,
        source_size_bytes=source.source_size_bytes,
        limits=chosen_limits,
    )
