#!/usr/bin/env python3
"""Deterministic B-05 file classification for research-project inventories.

Classification is intentionally local, bounded, and policy-aware: callers provide
the normalized project-relative path plus either a permitted small file prefix or
an explicit B-02 content-access restriction.  No model or external service is
required.  Strong file signatures outrank extensions when content access is
allowed; path-only classification remains explicit when raw bytes are protected.
Every result includes stable reason codes suitable for audit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


FILE_CLASSIFICATION_SCHEMA_VERSION = 1
FILE_CLASSIFICATION_KIND = "llmwiki-file-classification"
CLASSIFICATION_SAMPLE_BYTES = 64 * 1024
UNKNOWN_CLASSIFICATION = "unknown"

_SLUG = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class _FormatSpec:
    value: str
    media_type: str
    language: str = UNKNOWN_CLASSIFICATION
    binary: bool = False


@dataclass(frozen=True)
class _Candidate:
    spec: _FormatSpec
    reason_code: str
    reason: str


@dataclass(frozen=True)
class FileClassification:
    """Stable format, language, and research-role classification."""

    format: str
    media_type: str
    language: str
    research_role: str
    reasons: dict[str, dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FILE_CLASSIFICATION_SCHEMA_VERSION,
            "kind": FILE_CLASSIFICATION_KIND,
            "format": self.format,
            "media_type": self.media_type,
            "language": self.language,
            "research_role": self.research_role,
            "reasons": {
                key: dict(value) for key, value in sorted(self.reasons.items())
            },
        }


_FORMATS: dict[str, _FormatSpec] = {
    "plain_text": _FormatSpec("plain_text", "text/plain", "text"),
    "markdown": _FormatSpec("markdown", "text/markdown", "markdown"),
    "restructured_text": _FormatSpec(
        "restructured_text", "text/x-rst", "restructured_text"
    ),
    "python": _FormatSpec("python", "text/x-python", "python"),
    "r": _FormatSpec("r", "text/x-r", "r"),
    "julia": _FormatSpec("julia", "text/x-julia", "julia"),
    "matlab": _FormatSpec("matlab", "text/x-matlab", "matlab"),
    "c": _FormatSpec("c", "text/x-c", "c"),
    "cpp": _FormatSpec("cpp", "text/x-c++", "cpp"),
    "java": _FormatSpec("java", "text/x-java-source", "java"),
    "javascript": _FormatSpec(
        "javascript", "text/javascript", "javascript"
    ),
    "typescript": _FormatSpec(
        "typescript", "text/x-typescript", "typescript"
    ),
    "shell": _FormatSpec("shell", "text/x-shellscript", "shell"),
    "powershell": _FormatSpec(
        "powershell", "text/x-powershell", "powershell"
    ),
    "batch": _FormatSpec("batch", "text/x-msdos-batch", "batch"),
    "rust": _FormatSpec("rust", "text/x-rust", "rust"),
    "go": _FormatSpec("go", "text/x-go", "go"),
    "ruby": _FormatSpec("ruby", "text/x-ruby", "ruby"),
    "perl": _FormatSpec("perl", "text/x-perl", "perl"),
    "sql": _FormatSpec("sql", "application/sql", "sql"),
    "html": _FormatSpec("html", "text/html", "html"),
    "css": _FormatSpec("css", "text/css", "css"),
    "xml": _FormatSpec("xml", "application/xml", "xml"),
    "json": _FormatSpec("json", "application/json", "json"),
    "jsonl": _FormatSpec("jsonl", "application/x-ndjson", "json"),
    "yaml": _FormatSpec("yaml", "application/yaml", "yaml"),
    "toml": _FormatSpec("toml", "application/toml", "toml"),
    "ini": _FormatSpec("ini", "text/plain", "ini"),
    "csv": _FormatSpec("csv", "text/csv", "csv"),
    "tsv": _FormatSpec("tsv", "text/tab-separated-values", "tsv"),
    "latex": _FormatSpec("latex", "application/x-tex", "latex"),
    "bibtex": _FormatSpec("bibtex", "application/x-bibtex", "bibtex"),
    "notebook": _FormatSpec(
        "notebook", "application/x-ipynb+json", "jupyter_notebook"
    ),
    "log": _FormatSpec("log", "text/plain", "log"),
    "pdf": _FormatSpec("pdf", "application/pdf", binary=True),
    "docx": _FormatSpec(
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        binary=True,
    ),
    "pptx": _FormatSpec(
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        binary=True,
    ),
    "xlsx": _FormatSpec(
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        binary=True,
    ),
    "png": _FormatSpec("png", "image/png", binary=True),
    "jpeg": _FormatSpec("jpeg", "image/jpeg", binary=True),
    "gif": _FormatSpec("gif", "image/gif", binary=True),
    "tiff": _FormatSpec("tiff", "image/tiff", binary=True),
    "bmp": _FormatSpec("bmp", "image/bmp", binary=True),
    "svg": _FormatSpec("svg", "image/svg+xml", "svg"),
    "wav": _FormatSpec("wav", "audio/wav", binary=True),
    "mp3": _FormatSpec("mp3", "audio/mpeg", binary=True),
    "zip": _FormatSpec("zip", "application/zip", binary=True),
    "gzip": _FormatSpec("gzip", "application/gzip", binary=True),
    "tar": _FormatSpec("tar", "application/x-tar", binary=True),
    "parquet": _FormatSpec(
        "parquet", "application/vnd.apache.parquet", binary=True
    ),
    "hdf5": _FormatSpec("hdf5", "application/x-hdf5", binary=True),
    "npy": _FormatSpec("npy", "application/x-npy", binary=True),
    "npz": _FormatSpec("npz", "application/x-npz", binary=True),
    "mat_data": _FormatSpec("mat_data", "application/x-matlab-data", binary=True),
    "sqlite": _FormatSpec("sqlite", "application/vnd.sqlite3", binary=True),
    "pytorch_checkpoint": _FormatSpec(
        "pytorch_checkpoint", "application/x-pytorch", binary=True
    ),
    "model_checkpoint": _FormatSpec(
        "model_checkpoint", "application/octet-stream", binary=True
    ),
    "pickle": _FormatSpec("pickle", "application/x-python-pickle", binary=True),
    "binary": _FormatSpec("binary", "application/octet-stream", binary=True),
    UNKNOWN_CLASSIFICATION: _FormatSpec(
        UNKNOWN_CLASSIFICATION, "application/octet-stream", binary=True
    ),
}

_EXTENSION_FORMATS: dict[str, str] = {
    ".txt": "plain_text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "restructured_text",
    ".py": "python",
    ".pyi": "python",
    ".r": "r",
    ".rmd": "markdown",
    ".jl": "julia",
    ".m": "matlab",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".pl": "perl",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".xml": "xml",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".csv": "csv",
    ".tsv": "tsv",
    ".tex": "latex",
    ".sty": "latex",
    ".cls": "latex",
    ".bib": "bibtex",
    ".ipynb": "notebook",
    ".log": "log",
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".bmp": "bmp",
    ".svg": "svg",
    ".wav": "wav",
    ".mp3": "mp3",
    ".zip": "zip",
    ".gz": "gzip",
    ".tgz": "gzip",
    ".tar": "tar",
    ".parquet": "parquet",
    ".h5": "hdf5",
    ".hdf5": "hdf5",
    ".npy": "npy",
    ".npz": "npz",
    ".mat": "mat_data",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".db": "sqlite",
    ".pt": "pytorch_checkpoint",
    ".pth": "pytorch_checkpoint",
    ".ckpt": "model_checkpoint",
    ".safetensors": "model_checkpoint",
    ".pkl": "pickle",
    ".pickle": "pickle",
}

_KNOWN_FILENAMES: dict[str, str] = {
    "readme": "plain_text",
    "license": "plain_text",
    "copying": "plain_text",
    "authors": "plain_text",
    "changelog": "plain_text",
    "makefile": "plain_text",
    "dockerfile": "plain_text",
    "snakefile": "python",
    "requirements.txt": "plain_text",
    "environment.yml": "yaml",
    "environment.yaml": "yaml",
    "pyproject.toml": "toml",
    "cargo.toml": "toml",
    "package.json": "json",
    "tsconfig.json": "json",
}

_SHEBANG_FORMATS: tuple[tuple[str, str], ...] = (
    ("python", "python"),
    ("rscript", "r"),
    ("julia", "julia"),
    ("node", "javascript"),
    ("deno", "typescript"),
    ("pwsh", "powershell"),
    ("powershell", "powershell"),
    ("bash", "shell"),
    ("zsh", "shell"),
    ("fish", "shell"),
    ("sh", "shell"),
    ("ruby", "ruby"),
    ("perl", "perl"),
)

_RESEARCH_ROLES = {
    "project_documentation",
    "documentation",
    "source_code",
    "test_code",
    "automation",
    "configuration",
    "dependency_manifest",
    "notebook",
    "paper",
    "bibliography",
    "dataset",
    "experiment",
    "result",
    "run_log",
    "figure",
    "model_artifact",
    "project_metadata",
    UNKNOWN_CLASSIFICATION,
}

_DEPENDENCY_FILENAMES = {
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "pyproject.toml",
    "poetry.lock",
    "pipfile",
    "pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "renv.lock",
    "manifest.toml",
    "project.toml",
}

_SOURCE_FORMATS = {
    "python",
    "r",
    "julia",
    "matlab",
    "c",
    "cpp",
    "java",
    "javascript",
    "typescript",
    "shell",
    "powershell",
    "batch",
    "rust",
    "go",
    "ruby",
    "perl",
    "sql",
}

_CONFIG_FORMATS = {"json", "jsonl", "yaml", "toml", "ini", "xml"}
_DATA_FORMATS = {
    "csv",
    "tsv",
    "parquet",
    "hdf5",
    "npy",
    "npz",
    "mat_data",
    "sqlite",
}
_IMAGE_FORMATS = {"png", "jpeg", "gif", "tiff", "bmp", "svg"}
_MODEL_FORMATS = {"pytorch_checkpoint", "model_checkpoint", "pickle"}


def _reason(source: str, code: str, detail: str) -> dict[str, str]:
    return {"source": source, "code": code, "detail": detail}


def _decode_text(sample: bytes) -> str | None:
    if not sample:
        return ""
    encodings: list[str] = []
    if sample.startswith(b"\xef\xbb\xbf"):
        encodings.append("utf-8-sig")
    elif sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(("utf-8", "cp1252"))
    for encoding in encodings:
        try:
            text = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        controls = sum(
            1
            for char in text
            if ord(char) < 32 and char not in "\n\r\t\f\b"
        )
        if controls <= max(1, len(text) // 100):
            return text
    return None


def _magic_candidate(sample: bytes, suffix: str) -> _Candidate | None:
    signatures: tuple[tuple[bool, str, str], ...] = (
        (sample.startswith(b"%PDF-"), "pdf", "PDF header %PDF-"),
        (sample.startswith(b"\x89PNG\r\n\x1a\n"), "png", "PNG signature"),
        (sample.startswith(b"\xff\xd8\xff"), "jpeg", "JPEG signature"),
        (
            sample.startswith((b"GIF87a", b"GIF89a")),
            "gif",
            "GIF signature",
        ),
        (
            sample.startswith((b"II*\x00", b"MM\x00*")),
            "tiff",
            "TIFF signature",
        ),
        (sample.startswith(b"BM"), "bmp", "BMP signature"),
        (
            sample.startswith(b"\x89HDF\r\n\x1a\n"),
            "hdf5",
            "HDF5 signature",
        ),
        (sample.startswith(b"\x93NUMPY"), "npy", "NumPy NPY signature"),
        (sample.startswith(b"PAR1"), "parquet", "Parquet signature"),
        (
            sample.startswith(b"SQLite format 3\x00"),
            "sqlite",
            "SQLite signature",
        ),
        (
            sample.startswith(b"MATLAB 5.0 MAT-file"),
            "mat_data",
            "MATLAB MAT-file header",
        ),
        (sample.startswith(b"\x1f\x8b"), "gzip", "gzip signature"),
        (
            sample.startswith(b"RIFF") and sample[8:12] == b"WAVE",
            "wav",
            "RIFF/WAVE signature",
        ),
        (
            sample.startswith(b"ID3")
            or (len(sample) >= 2 and sample[0] == 0xFF and sample[1] & 0xE0 == 0xE0),
            "mp3",
            "MP3 signature",
        ),
    )
    for matched, value, detail in signatures:
        if matched:
            return _Candidate(_FORMATS[value], "magic-signature", detail)

    if sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        zipped_by_suffix = {
            ".docx": "docx",
            ".pptx": "pptx",
            ".xlsx": "xlsx",
            ".npz": "npz",
        }
        value = zipped_by_suffix.get(suffix, "zip")
        return _Candidate(
            _FORMATS[value],
            "magic-zip-container",
            f"ZIP container signature with {suffix or 'no'} extension",
        )
    return None


def _shebang_candidate(text: str) -> _Candidate | None:
    first_line = text.splitlines()[0].strip().lower() if text.splitlines() else ""
    if not first_line.startswith("#!"):
        return None
    for token, value in _SHEBANG_FORMATS:
        if re.search(rf"(?:^|[/\s]){re.escape(token)}(?:\s|$)", first_line):
            return _Candidate(
                _FORMATS[value],
                "shebang-interpreter",
                f"shebang selects {token}",
            )
    return None


def _content_candidate(text: str) -> _Candidate | None:
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return None
    if stripped.startswith(("{", "[")):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            if (
                isinstance(value, dict)
                and isinstance(value.get("cells"), list)
                and isinstance(value.get("nbformat"), int)
            ):
                return _Candidate(
                    _FORMATS["notebook"],
                    "content-notebook-json",
                    "JSON object contains Jupyter cells and nbformat",
                )
            return _Candidate(
                _FORMATS["json"],
                "content-json",
                "sample parses as JSON",
            )
    lowered = stripped[:512].lower()
    if lowered.startswith("<?xml"):
        return _Candidate(_FORMATS["xml"], "content-xml", "XML declaration")
    if lowered.startswith("<!doctype html") or re.search(
        r"<html(?:\s|>)", lowered
    ):
        return _Candidate(_FORMATS["html"], "content-html", "HTML root marker")
    if lowered.startswith("<svg") or "<svg " in lowered:
        return _Candidate(_FORMATS["svg"], "content-svg", "SVG root marker")
    if re.search(r"^\s*\\documentclass(?:\[.*?\])?\{", text, re.MULTILINE):
        return _Candidate(
            _FORMATS["latex"],
            "content-latex-documentclass",
            "LaTeX documentclass command",
        )
    return None


def _format_candidate(relative_path: str, sample: bytes) -> tuple[_Candidate, str | None]:
    path = PurePosixPath(relative_path)
    name = path.name
    lowered_name = name.lower()
    suffix = path.suffix.lower()
    extension_value = _EXTENSION_FORMATS.get(suffix)
    extension_spec = _FORMATS[extension_value] if extension_value else None
    text = _decode_text(sample)

    magic = _magic_candidate(sample, suffix)
    if magic is not None:
        return magic, text

    shebang = _shebang_candidate(text) if text is not None else None
    if shebang is not None:
        detail = shebang.reason
        if extension_spec is not None and extension_spec.value != shebang.spec.value:
            detail += f"; overrides extension {suffix}"
        return _Candidate(shebang.spec, shebang.reason_code, detail), text

    content = _content_candidate(text) if text is not None else None
    if extension_spec is not None:
        if extension_spec.binary and text is not None:
            if content is not None:
                return _Candidate(
                    content.spec,
                    "content-overrides-binary-extension",
                    f"text content does not match binary extension {suffix}; "
                    f"{content.reason}",
                ), text
            return _Candidate(
                _FORMATS["plain_text"],
                "binary-extension-signature-mismatch",
                f"text content lacks the required signature for {suffix}",
            ), text
        return _Candidate(
            extension_spec,
            "extension-match",
            f"recognized extension {suffix}",
        ), text

    known_value = _KNOWN_FILENAMES.get(lowered_name)
    if known_value is not None:
        return _Candidate(
            _FORMATS[known_value],
            "known-filename",
            f"recognized filename {name}",
        ), text

    if content is not None:
        return content, text
    if text is not None:
        return _Candidate(
            _FORMATS["plain_text"],
            "text-fallback",
            "sample is decodable text with no stronger format signal",
        ), text
    return _Candidate(
        _FORMATS[UNKNOWN_CLASSIFICATION],
        "unknown-binary-fallback",
        "no supported signature, extension, filename, or text signal",
    ), None


def _path_only_format_candidate(
    relative_path: str,
    *,
    content_access_reason_code: str,
    content_access_reason: str,
) -> _Candidate:
    path = PurePosixPath(relative_path)
    name = path.name
    suffix = path.suffix.lower()
    extension_value = _EXTENSION_FORMATS.get(suffix)
    restriction = (
        f"B-02 raw-content access is restricted by {content_access_reason_code}: "
        f"{content_access_reason}"
    )

    if extension_value is not None:
        return _Candidate(
            _FORMATS[extension_value],
            f"{content_access_reason_code}-extension-match",
            f"{restriction}; recognized extension {suffix}",
        )

    known_value = _KNOWN_FILENAMES.get(name.lower())
    if known_value is not None:
        return _Candidate(
            _FORMATS[known_value],
            f"{content_access_reason_code}-known-filename",
            f"{restriction}; recognized filename {name}",
        )

    return _Candidate(
        _FORMATS[UNKNOWN_CLASSIFICATION],
        f"{content_access_reason_code}-path-only-fallback",
        f"{restriction}; no supported path-only format signal matched",
    )


def _role_for(relative_path: str, format_value: str) -> tuple[str, dict[str, str]]:
    path = PurePosixPath(relative_path)
    parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    stem = path.stem.lower()
    parent_parts = set(parts[:-1])

    if stem.startswith("readme") or name in {"license", "copying", "authors"}:
        return "project_documentation", _reason(
            "filename", "project-documentation-filename", f"recognized {path.name}"
        )
    if name in _DEPENDENCY_FILENAMES:
        return "dependency_manifest", _reason(
            "filename", "dependency-manifest-filename", f"recognized {path.name}"
        )
    if name in {".gitignore", ".gitattributes", ".llmwikiignore"}:
        return "project_metadata", _reason(
            "filename", "project-metadata-filename", f"recognized {path.name}"
        )
    if format_value == "notebook" or parent_parts & {"notebook", "notebooks"}:
        return "notebook", _reason(
            "path", "notebook-path-or-format", "notebook format or directory"
        )
    if parent_parts & {"test", "tests", "testing", "spec", "specs"}:
        return "test_code", _reason(
            "path", "test-directory", "path is under a test directory"
        )
    if parent_parts & {"paper", "papers", "manuscript", "manuscripts"}:
        if format_value == "bibtex":
            return "bibliography", _reason(
                "path", "paper-bibliography", "bibliography within paper material"
            )
        return "paper", _reason(
            "path", "paper-directory", "path is under a paper directory"
        )
    if format_value == "bibtex":
        return "bibliography", _reason(
            "format", "bibliography-format", "BibTeX source"
        )
    if parent_parts & {"config", "configs", "configuration", "settings"}:
        return "configuration", _reason(
            "path", "configuration-directory", "path is under a configuration directory"
        )
    if parent_parts & {"script", "scripts", "bin", "tools"}:
        return "automation", _reason(
            "path", "automation-directory", "path is under an automation directory"
        )
    if parent_parts & {"figure", "figures", "images", "plots"}:
        return "figure", _reason(
            "path", "figure-directory", "path is under a figure directory"
        )
    if parent_parts & {"result", "results", "output", "outputs", "reports"}:
        return "result", _reason(
            "path", "result-directory", "path is under a result directory"
        )
    if parent_parts & {"experiment", "experiments", "runs"}:
        if format_value == "log":
            return "run_log", _reason(
                "path", "experiment-log", "log inside experiment/run directory"
            )
        return "experiment", _reason(
            "path", "experiment-directory", "path is under an experiment directory"
        )
    if parent_parts & {"data", "dataset", "datasets"}:
        return "dataset", _reason(
            "path", "dataset-directory", "path is under a data directory"
        )
    if parent_parts & {"model", "models", "weight", "weights", "checkpoint", "checkpoints"}:
        return "model_artifact", _reason(
            "path", "model-directory", "path is under a model/checkpoint directory"
        )
    if format_value in _MODEL_FORMATS:
        return "model_artifact", _reason(
            "format", "model-artifact-format", f"format is {format_value}"
        )
    if format_value == "log":
        return "run_log", _reason("format", "log-format", "log file format")
    if format_value in _IMAGE_FORMATS:
        return "figure", _reason(
            "format", "visual-format", f"visual format is {format_value}"
        )
    if format_value in _DATA_FORMATS:
        return "dataset", _reason(
            "format", "structured-data-format", f"data format is {format_value}"
        )
    if format_value in _CONFIG_FORMATS:
        return "configuration", _reason(
            "format", "configuration-format", f"configuration-like format is {format_value}"
        )
    if format_value in _SOURCE_FORMATS:
        return "source_code", _reason(
            "format", "source-code-format", f"source format is {format_value}"
        )
    if format_value in {"markdown", "restructured_text", "plain_text", "html"}:
        return "documentation", _reason(
            "format", "documentation-format", f"documentation-like format is {format_value}"
        )
    return UNKNOWN_CLASSIFICATION, _reason(
        "fallback", "unknown-research-role", "no deterministic research-role rule matched"
    )


def classify_file(
    relative_path: str,
    sample: bytes | None,
    *,
    content_access_reason_code: str | None = None,
    content_access_reason: str | None = None,
) -> FileClassification:
    """Classify one normalized project-relative regular file deterministically.

    ``sample=None`` means B-02 prohibited reading raw content.  Callers must then
    provide the policy reason so the path-only result remains auditable.  An empty
    byte string is distinct: it represents a permitted sample from an empty file.
    """

    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("relative_path must be a non-empty string")
    if sample is not None and not isinstance(sample, bytes):
        raise TypeError("sample must be bytes or None")

    if sample is None:
        if (
            not isinstance(content_access_reason_code, str)
            or not content_access_reason_code
            or not _SLUG.fullmatch(content_access_reason_code.replace("-", "_"))
        ):
            raise ValueError(
                "content_access_reason_code must be a non-empty stable code "
                "when sample is None"
            )
        if not isinstance(content_access_reason, str) or not content_access_reason:
            raise ValueError(
                "content_access_reason must be non-empty when sample is None"
            )
        candidate = _path_only_format_candidate(
            relative_path,
            content_access_reason_code=content_access_reason_code,
            content_access_reason=content_access_reason,
        )
        format_reason_source = "policy-path"
    else:
        if content_access_reason_code is not None or content_access_reason is not None:
            raise ValueError(
                "content-access restriction reasons are only valid when sample is None"
            )
        candidate, _text = _format_candidate(relative_path, sample)
        format_reason_source = "content-or-path"

    role, role_reason = _role_for(relative_path, candidate.spec.value)
    language_reason = (
        _reason(
            "format",
            "language-from-format",
            f"language is implied by format {candidate.spec.value}",
        )
        if candidate.spec.language != UNKNOWN_CLASSIFICATION
        else _reason(
            "fallback",
            "unknown-language",
            "format does not deterministically identify a language",
        )
    )
    return FileClassification(
        format=candidate.spec.value,
        media_type=candidate.spec.media_type,
        language=candidate.spec.language,
        research_role=role,
        reasons={
            "format": _reason(
                format_reason_source,
                candidate.reason_code,
                candidate.reason,
            ),
            "language": language_reason,
            "research_role": role_reason,
        },
    )


def classification_from_dict(value: object) -> FileClassification:
    """Validate and load a persisted B-05 classification object."""

    if not isinstance(value, dict):
        raise ValueError("classification must be an object")
    if value.get("schema_version") != FILE_CLASSIFICATION_SCHEMA_VERSION:
        raise ValueError(
            "classification schema_version is missing, legacy, or unsupported"
        )
    if value.get("kind") != FILE_CLASSIFICATION_KIND:
        raise ValueError(f"unexpected classification kind {value.get('kind')!r}")
    format_value = value.get("format")
    if format_value not in _FORMATS:
        raise ValueError(f"unsupported classification format {format_value!r}")
    media_type = value.get("media_type")
    if media_type != _FORMATS[format_value].media_type:
        raise ValueError("classification media_type does not match format")
    language = value.get("language")
    if language != _FORMATS[format_value].language:
        raise ValueError("classification language does not match format")
    research_role = value.get("research_role")
    if research_role not in _RESEARCH_ROLES:
        raise ValueError(f"unsupported research_role {research_role!r}")
    reasons = value.get("reasons")
    if not isinstance(reasons, dict) or set(reasons) != {
        "format",
        "language",
        "research_role",
    }:
        raise ValueError(
            "classification reasons must contain format, language, and research_role"
        )
    normalized_reasons: dict[str, dict[str, str]] = {}
    for key in ("format", "language", "research_role"):
        reason = reasons[key]
        if not isinstance(reason, dict) or set(reason) != {
            "source",
            "code",
            "detail",
        }:
            raise ValueError(f"classification reason {key} is malformed")
        if not all(isinstance(reason[field], str) and reason[field] for field in reason):
            raise ValueError(f"classification reason {key} fields must be non-empty")
        if not _SLUG.fullmatch(reason["source"].replace("-", "_")):
            raise ValueError(f"classification reason {key} source is invalid")
        if not _SLUG.fullmatch(reason["code"].replace("-", "_")):
            raise ValueError(f"classification reason {key} code is invalid")
        normalized_reasons[key] = dict(reason)
    return FileClassification(
        format=format_value,
        media_type=media_type,
        language=language,
        research_role=research_role,
        reasons=normalized_reasons,
    )
