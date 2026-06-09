#!/usr/bin/env python3
from __future__ import annotations

"""
Create a full-project raw Markdown evidence layer.

Usage:
    python tools/raw_md.py <project-root>

Default output:
    <project-root>/<project-name>-wiki/raw-md/primary/...
    <project-root>/<project-name>-wiki/state/raw-md-manifest.json
    <project-root>/<project-name>-wiki/reports/raw-md-report.md

This command is deterministic and does not call an LLM API.
"""

import argparse
import csv
import hashlib
import html.parser
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


DEFAULT_MAX_FILE_MB = 20

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".tox",
    ".idea",
    ".vscode",
    ".obsidian",
    "node_modules",
    "bower_components",
    "vendor",
    "dist",
    "build",
    "out",
    "target",
    ".next",
    ".nuxt",
    "coverage",
}

DIRECT_MARKDOWN_EXTS = {".md", ".markdown", ".mdown"}

PLAIN_TEXT_EXTS = {
    ".txt",
    ".text",
    ".log",
    ".rst",
    ".adoc",
    ".tex",
    ".bib",
}

STRUCTURED_TEXT_EXTS = {
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    ".xml",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".lock",
}

CODE_EXTS = {
    ".py",
    ".pyw",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".java",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".bat",
    ".cmd",
    ".sql",
    ".r",
    ".m",
    ".mm",
    ".lua",
    ".pl",
    ".pm",
    ".dart",
    ".vue",
    ".svelte",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".graphql",
    ".proto",
    ".gradle",
    ".cmake",
    ".make",
    ".mk",
}

CODE_FILENAMES = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "rakefile": "ruby",
    "gemfile": "ruby",
    "jenkinsfile": "groovy",
    "vagrantfile": "ruby",
}

TEXT_FILENAMES = {
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".npmignore",
    ".editorconfig",
    ".env",
    ".env.example",
    "requirements.txt",
}

MARKITDOWN_EXTS = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".html",
    ".htm",
    ".epub",
    ".ipynb",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
}

METADATA_ONLY_EXTS = {
    # images
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
    # audio
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
    # video
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".wmv",
    ".mpeg",
    ".mpg",
    ".m4v",
    # archives
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".tgz",
    # model/data/binary-heavy formats
    ".pt",
    ".pth",
    ".onnx",
    ".safetensors",
    ".ckpt",
    ".h5",
    ".pkl",
    ".pickle",
    ".joblib",
    ".bin",
    ".weights",
    ".pb",
    ".tflite",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".war",
    ".wasm",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
}

LANG_BY_EXT = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".fish": "fish",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".sql": "sql",
    ".r": "r",
    ".m": "matlab",
    ".mm": "objective-c",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".dart": "dart",
    ".vue": "vue",
    ".svelte": "svelte",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".graphql": "graphql",
    ".proto": "protobuf",
    ".json": "json",
    ".jsonl": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".csv": "csv",
    ".tsv": "tsv",
    ".xml": "xml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "text",
    ".properties": "properties",
    ".lock": "text",
}


@dataclass
class RawMdConfig:
    project_root: Path
    wiki_root: Path
    scope: str = "primary"
    max_file_bytes: int = DEFAULT_MAX_FILE_MB * 1024 * 1024
    clean: bool = False

    @property
    def raw_md_root(self) -> Path:
        return self.wiki_root / "raw-md"

    @property
    def scoped_raw_md_root(self) -> Path:
        return self.raw_md_root / self.scope

    @property
    def state_dir(self) -> Path:
        return self.wiki_root / "state"

    @property
    def reports_dir(self) -> Path:
        return self.wiki_root / "reports"


@dataclass
class ConversionResult:
    status: str
    converter: str
    content: str = ""
    notes: list[str] = field(default_factory=list)
    reason: str | None = None


class SimpleHTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "section", "article", "br", "li", "tr"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self.parts.append("\n" + ("#" * level) + " ")

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text + " ")

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


class RawMdBuilder:
    def __init__(self, config: RawMdConfig) -> None:
        self.config = config
        self.entries: list[dict[str, Any]] = []
        self._markitdown: Any | None = None
        self._markitdown_import_error: str | None = None
        self.generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def run(self) -> dict[str, Any]:
        self._prepare_output_dirs()

        for skipped_dir in self._iter_files_and_record_skipped_dirs():
            if skipped_dir.is_file():
                self._process_file(skipped_dir)

        manifest = self._build_manifest()
        self._write_manifest(manifest)
        self._write_report(manifest)
        return manifest

    def _prepare_output_dirs(self) -> None:
        if self.config.clean and self.config.scoped_raw_md_root.exists():
            resolved_scope = self.config.scoped_raw_md_root.resolve()
            resolved_wiki = self.config.wiki_root.resolve()
            if not (resolved_scope == resolved_wiki or resolved_wiki in resolved_scope.parents):
                raise RuntimeError(f"Refusing to clean outside wiki root: {resolved_scope}")
            shutil.rmtree(resolved_scope)

        self.config.scoped_raw_md_root.mkdir(parents=True, exist_ok=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)

    def _iter_files_and_record_skipped_dirs(self) -> list[Path]:
        files: list[Path] = []
        root = self.config.project_root

        for dirpath_str, dirnames, filenames in os.walk(root):
            dirpath = Path(dirpath_str)
            kept_dirs = []

            for dirname in sorted(dirnames):
                child = dirpath / dirname
                reason = self._skip_dir_reason(child)
                if reason:
                    self._record_skipped_dir(child, reason)
                else:
                    kept_dirs.append(dirname)

            dirnames[:] = kept_dirs

            for filename in sorted(filenames):
                files.append(dirpath / filename)

        return files

    def _skip_dir_reason(self, path: Path) -> str | None:
        name_lower = path.name.lower()
        if name_lower in SKIP_DIR_NAMES:
            return f"excluded directory: {path.name}"
        if name_lower.endswith("-wiki"):
            return "generated wiki directory"
        try:
            resolved_path = path.resolve()
            resolved_wiki = self.config.wiki_root.resolve()
            if resolved_path == resolved_wiki or resolved_wiki in resolved_path.parents:
                return "raw-md output directory"
        except OSError:
            return "unresolvable directory"
        return None

    def _record_skipped_dir(self, path: Path, reason: str) -> None:
        rel = self._rel_posix(path)
        self.entries.append(
            {
                "source_path": rel,
                "kind": "dir",
                "status": "skipped",
                "reason": reason,
                "scope": self.config.scope,
                "output_path": None,
                "converter": None,
                "bytes": None,
                "mime": None,
                "source_hash": None,
            }
        )

    def _process_file(self, path: Path) -> None:
        rel = self._rel_posix(path)
        stat = path.stat()
        size = stat.st_size
        mime = guess_mime(path)
        source_hash = sha256_file(path) if size <= self.config.max_file_bytes else None
        output_path = self._output_path_for(path)

        if size > self.config.max_file_bytes:
            result = ConversionResult(
                status="metadata-only",
                converter="metadata-only",
                reason="file_too_large",
                notes=[f"File exceeds max-file-mb limit ({self.config.max_file_bytes} bytes)."],
            )
        else:
            result = self._convert_file(path)

        page = build_raw_md_page(
            rel_path=rel,
            source_hash=source_hash,
            scope=self.config.scope,
            converter=result.converter,
            status=result.status,
            size=size,
            mime=mime,
            converted_at=self.generated_at,
            content=result.content,
            notes=result.notes,
            reason=result.reason,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(page, encoding="utf-8")

        record = {
            "source_path": rel,
            "kind": "file",
            "status": result.status,
            "reason": result.reason,
            "scope": self.config.scope,
            "output_path": self._wiki_rel_posix(output_path),
            "converter": result.converter,
            "bytes": size,
            "mime": mime,
            "source_hash": source_hash,
        }
        if result.notes:
            record["notes"] = result.notes
        self.entries.append(record)

    def _convert_file(self, path: Path) -> ConversionResult:
        suffix = path.suffix.lower()
        name_lower = path.name.lower()

        if suffix in METADATA_ONLY_EXTS:
            return ConversionResult(
                status="metadata-only",
                converter="metadata-only",
                reason=f"unsupported_binary_type:{suffix or path.name}",
                notes=["First version records metadata only for this media/binary/archive type."],
            )

        if suffix in DIRECT_MARKDOWN_EXTS:
            return self._convert_direct_text(path, converter="markdown-wrap", fenced=False)

        if (
            suffix in CODE_EXTS
            or name_lower in CODE_FILENAMES
            or suffix in STRUCTURED_TEXT_EXTS
        ):
            lang = CODE_FILENAMES.get(name_lower) or LANG_BY_EXT.get(suffix, "")
            return self._convert_direct_text(path, converter="code-wrap", fenced=True, lang=lang)

        if suffix in PLAIN_TEXT_EXTS or name_lower in TEXT_FILENAMES:
            return self._convert_direct_text(path, converter="text-wrap", fenced=True, lang="text")

        if suffix in MARKITDOWN_EXTS:
            converted = self._convert_with_markitdown(path)
            if converted.status == "ok":
                return converted
            fallback = self._convert_with_fallback(path, converted)
            if fallback.status == "ok":
                return fallback
            return self._metadata_from_failed_conversion(path, converted, fallback)

        if is_probably_text(path):
            return self._convert_direct_text(path, converter="text-wrap", fenced=True, lang="text")

        return ConversionResult(
            status="metadata-only",
            converter="metadata-only",
            reason=f"unsupported_or_binary_type:{suffix or path.name}",
            notes=["No safe text converter matched this file in the first version."],
        )

    def _convert_direct_text(
        self,
        path: Path,
        converter: str,
        fenced: bool,
        lang: str = "",
    ) -> ConversionResult:
        try:
            text, encoding = read_text_best_effort(path)
        except UnicodeError as exc:
            return ConversionResult(
                status="metadata-only",
                converter="metadata-only",
                reason="text_decode_failed",
                notes=[str(exc)],
            )

        if fenced:
            text = fence_code(text, lang=lang)

        notes = [f"Read directly as {encoding}; no LLM or MarkItDown transformation."]
        return ConversionResult(status="ok", converter=converter, content=text, notes=notes)

    def _convert_with_markitdown(self, path: Path) -> ConversionResult:
        try:
            md = self._get_markitdown()
            result = md.convert(str(path))
            content = (getattr(result, "text_content", "") or "").strip()
            if not content:
                return ConversionResult(
                    status="metadata-only",
                    converter="markitdown",
                    reason="markitdown_empty_output",
                    notes=["MarkItDown returned empty text_content."],
                )
            return ConversionResult(
                status="ok",
                converter="markitdown",
                content=content,
                notes=["Converted with MarkItDown; no LLM API call."],
            )
        except Exception as exc:
            return ConversionResult(
                status="metadata-only",
                converter="markitdown",
                reason="markitdown_failed",
                notes=[f"MarkItDown failed: {type(exc).__name__}: {exc}"],
            )

    def _get_markitdown(self) -> Any:
        if self._markitdown is not None:
            return self._markitdown
        if self._markitdown_import_error:
            raise RuntimeError(self._markitdown_import_error)
        try:
            from markitdown import MarkItDown
        except Exception as exc:
            self._markitdown_import_error = f"markitdown import failed: {exc}"
            raise RuntimeError(self._markitdown_import_error) from exc
        self._markitdown = MarkItDown(enable_plugins=False)
        return self._markitdown

    def _convert_with_fallback(
        self,
        path: Path,
        previous: ConversionResult,
    ) -> ConversionResult:
        suffix = path.suffix.lower()
        notes = list(previous.notes)

        try:
            if suffix in {".html", ".htm"}:
                text, encoding = read_text_best_effort(path)
                parser = SimpleHTMLTextExtractor()
                parser.feed(text)
                content = parser.get_text()
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-html",
                        content=content,
                        notes=notes + [f"Fallback parsed HTML text as {encoding}."],
                    )
            if suffix == ".ipynb":
                content = fallback_ipynb(path)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-ipynb",
                        content=content,
                        notes=notes + ["Fallback extracted notebook markdown/code cells."],
                    )
            if suffix == ".docx":
                content = fallback_docx(path)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-docx",
                        content=content,
                        notes=notes + ["Fallback extracted text from DOCX XML."],
                    )
            if suffix == ".pptx":
                content = fallback_pptx(path)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-pptx",
                        content=content,
                        notes=notes + ["Fallback extracted slide text from PPTX XML."],
                    )
            if suffix == ".xlsx":
                content = fallback_xlsx(path)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-xlsx",
                        content=content,
                        notes=notes + ["Fallback extracted workbook text from XLSX XML."],
                    )
            if suffix == ".epub":
                content = fallback_epub(path)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-epub",
                        content=content,
                        notes=notes + ["Fallback extracted XHTML text from EPUB archive."],
                    )
            if suffix == ".rtf":
                text, encoding = read_text_best_effort(path)
                content = fallback_rtf_text(text)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-rtf",
                        content=content,
                        notes=notes + [f"Fallback stripped basic RTF controls as {encoding}."],
                    )
            if suffix == ".pdf":
                content = fallback_pdf(path)
                if content:
                    return ConversionResult(
                        status="ok",
                        converter="fallback-pdf",
                        content=content,
                        notes=notes + ["Fallback extracted PDF text with an installed PDF library."],
                    )
        except Exception as exc:
            return ConversionResult(
                status="metadata-only",
                converter="fallback",
                reason="fallback_failed",
                notes=notes + [f"Fallback failed: {type(exc).__name__}: {exc}"],
            )

        return ConversionResult(
            status="metadata-only",
            converter="fallback",
            reason="fallback_unavailable",
            notes=notes + ["No lightweight fallback produced text for this format."],
        )

    def _metadata_from_failed_conversion(
        self,
        path: Path,
        markitdown_result: ConversionResult,
        fallback_result: ConversionResult,
    ) -> ConversionResult:
        reason = fallback_result.reason or markitdown_result.reason or "conversion_failed"
        notes = markitdown_result.notes + fallback_result.notes
        return ConversionResult(
            status="metadata-only",
            converter="metadata-only",
            reason=reason,
            notes=dedupe_preserve_order(notes),
        )

    def _output_path_for(self, path: Path) -> Path:
        rel = path.relative_to(self.config.project_root)
        return self.config.scoped_raw_md_root / Path(rel.as_posix() + ".md")

    def _rel_posix(self, path: Path) -> str:
        return path.relative_to(self.config.project_root).as_posix()

    def _wiki_rel_posix(self, path: Path) -> str:
        return path.relative_to(self.config.wiki_root).as_posix()

    def _build_manifest(self) -> dict[str, Any]:
        counts = Counter(entry["status"] for entry in self.entries)
        file_count = sum(1 for entry in self.entries if entry["kind"] == "file")
        dir_count = sum(1 for entry in self.entries if entry["kind"] == "dir")
        return {
            "version": 1,
            "generated_at": self.generated_at,
            "project_root": str(self.config.project_root),
            "wiki_root": str(self.config.wiki_root),
            "raw_md_root": str(self.config.raw_md_root),
            "scope": self.config.scope,
            "max_file_bytes": self.config.max_file_bytes,
            "counts": {
                "entries": len(self.entries),
                "files": file_count,
                "directories": dir_count,
                "ok": counts.get("ok", 0),
                "metadata_only": counts.get("metadata-only", 0),
                "skipped": counts.get("skipped", 0),
            },
            "entries": self.entries,
        }

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        path = self.config.state_dir / "raw-md-manifest.json"
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    def _write_report(self, manifest: dict[str, Any]) -> None:
        report = format_report(manifest)
        path = self.config.reports_dir / "raw-md-report.md"
        path.write_text(report, encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    if path.suffix.lower() in CODE_EXTS or path.name.lower() in CODE_FILENAMES:
        return "text/plain"
    return "application/octet-stream"


def read_text_best_effort(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    encodings = ["utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1"]
    errors: list[str] = []
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise UnicodeError("; ".join(errors))


def is_probably_text(path: Path, sample_size: int = 8192) -> bool:
    try:
        data = path.read_bytes()[:sample_size]
    except OSError:
        return False
    if not data:
        return True
    if b"\x00" in data:
        return False
    textish = sum(byte in b"\n\r\t\f\b" or 32 <= byte <= 126 or byte >= 128 for byte in data)
    return textish / len(data) > 0.85


def fence_code(text: str, lang: str = "") -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    lang_part = lang.strip()
    return f"{fence}{lang_part}\n{text.rstrip()}\n{fence}"


def build_raw_md_page(
    rel_path: str,
    source_hash: str | None,
    scope: str,
    converter: str,
    status: str,
    size: int,
    mime: str,
    converted_at: str,
    content: str,
    notes: list[str],
    reason: str | None,
) -> str:
    frontmatter = "\n".join(
        [
            "---",
            f"title: {json.dumps(rel_path, ensure_ascii=False)}",
            "type: raw-md",
            f"source_path: {json.dumps(rel_path, ensure_ascii=False)}",
            f"source_hash: {json.dumps(source_hash) if source_hash else 'null'}",
            f"scope: {json.dumps(scope, ensure_ascii=False)}",
            f"converter: {json.dumps(converter, ensure_ascii=False)}",
            f"status: {json.dumps(status, ensure_ascii=False)}",
            f"bytes: {size}",
            f"mime: {json.dumps(mime, ensure_ascii=False)}",
            f"converted_at: {json.dumps(converted_at, ensure_ascii=False)}",
            "---",
        ]
    )

    lines = [
        frontmatter,
        "",
        f"# {rel_path}",
        "",
        "## Metadata",
        "",
        f"- Source path: `{rel_path}`",
        f"- Source hash: `{source_hash or 'not-computed'}`",
        f"- Scope: `{scope}`",
        f"- Converter: `{converter}`",
        f"- Status: `{status}`",
        f"- Bytes: `{size}`",
        f"- MIME: `{mime}`",
    ]

    if reason:
        lines.append(f"- Reason: `{reason}`")

    lines.extend(["", "## Content", ""])
    if content:
        lines.append(content.rstrip())
    else:
        lines.append("_No text content was extracted. This page preserves file metadata only._")

    if notes:
        lines.extend(["", "## Conversion Notes", ""])
        for note in notes:
            lines.append(f"- {note}")

    lines.append("")
    return "\n".join(lines)


def fallback_ipynb(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    cells = data.get("cells", [])
    parts: list[str] = []
    for idx, cell in enumerate(cells, 1):
        cell_type = cell.get("cell_type", "unknown")
        source = "".join(cell.get("source", []))
        if cell_type == "markdown":
            parts.append(source.strip())
        elif cell_type == "code":
            parts.append(f"### Code Cell {idx}\n\n{fence_code(source, 'python')}")
            outputs = []
            for output in cell.get("outputs", []):
                text = output.get("text")
                if isinstance(text, list):
                    outputs.append("".join(text))
                elif isinstance(text, str):
                    outputs.append(text)
            if outputs:
                parts.append(f"### Output {idx}\n\n{fence_code(''.join(outputs), 'text')}")
        elif source:
            parts.append(f"### {cell_type.title()} Cell {idx}\n\n{source.strip()}")
    return "\n\n".join(part for part in parts if part.strip())


def fallback_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    paragraphs: list[str] = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        texts = [
            node.text or ""
            for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        ]
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def fallback_pptx(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        slide_names = sorted(
            (name for name in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)),
            key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1)),  # type: ignore[union-attr]
        )
        for slide_idx, slide_name in enumerate(slide_names, 1):
            root = ET.fromstring(zf.read(slide_name))
            texts = [
                node.text or ""
                for node in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
            ]
            slide_text = "\n".join(text.strip() for text in texts if text.strip())
            if slide_text:
                parts.append(f"## Slide {slide_idx}\n\n{slide_text}")
    return "\n\n".join(parts)


def fallback_xlsx(path: Path) -> str:
    shared_strings: list[str] = []
    parts: list[str] = []
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    with zipfile.ZipFile(path) as zf:
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("main:si", ns):
                texts = [node.text or "" for node in si.findall(".//main:t", ns)]
                shared_strings.append("".join(texts))

        sheet_names = sorted(name for name in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", name))
        for sheet_idx, sheet_name in enumerate(sheet_names, 1):
            root = ET.fromstring(zf.read(sheet_name))
            rows: list[list[str]] = []
            for row in root.findall(".//main:row", ns):
                values: list[str] = []
                for cell in row.findall("main:c", ns):
                    value = cell.find("main:v", ns)
                    inline_text = cell.find(".//main:t", ns)
                    cell_type = cell.attrib.get("t")
                    if inline_text is not None and inline_text.text:
                        values.append(inline_text.text)
                    elif value is None or value.text is None:
                        values.append("")
                    elif cell_type == "s":
                        idx = int(value.text)
                        values.append(shared_strings[idx] if idx < len(shared_strings) else value.text)
                    else:
                        values.append(value.text)
                if any(values):
                    rows.append(values)
            if rows:
                rendered = render_markdown_table(rows[:200])
                parts.append(f"## Sheet {sheet_idx}\n\n{rendered}")

    return "\n\n".join(parts)


def fallback_epub(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = [
            name
            for name in zf.namelist()
            if name.lower().endswith((".html", ".xhtml", ".htm"))
        ]
        for name in sorted(names):
            parser = SimpleHTMLTextExtractor()
            parser.feed(zf.read(name).decode("utf-8", errors="ignore"))
            text = parser.get_text()
            if text:
                parts.append(f"## {name}\n\n{text}")
    return "\n\n".join(parts)


def fallback_rtf_text(text: str) -> str:
    text = text.replace(r"\par", "\n")
    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
    text = re.sub(r"\\[a-zA-Z]+\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fallback_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for idx, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"## Page {idx}\n\n{text.strip()}")
        return "\n\n".join(pages)
    except Exception:
        pass

    try:
        import fitz  # type: ignore

        doc = fitz.open(str(path))
        pages = []
        for idx, page in enumerate(doc, 1):
            text = page.get_text("text") or ""
            if text.strip():
                pages.append(f"## Page {idx}\n\n{text.strip()}")
        return "\n\n".join(pages)
    except Exception as exc:
        raise RuntimeError("no PDF fallback library available or extraction failed") from exc


def render_markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    output = io.StringIO()
    writer = csv.writer(output)
    for row in normalized:
        writer.writerow(row)
    csv_text = output.getvalue().strip()
    return fence_code(csv_text, "csv")


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def format_report(manifest: dict[str, Any]) -> str:
    entries = manifest["entries"]
    counts = manifest["counts"]
    metadata_only = [entry for entry in entries if entry["status"] == "metadata-only"]
    skipped = [entry for entry in entries if entry["status"] == "skipped"]
    ok = [entry for entry in entries if entry["status"] == "ok"]

    lines = [
        f"# Raw Markdown Conversion Report - {manifest['generated_at']}",
        "",
        f"- Project root: `{manifest['project_root']}`",
        f"- Wiki root: `{manifest['wiki_root']}`",
        f"- Raw-md root: `{manifest['raw_md_root']}`",
        f"- Scope: `{manifest['scope']}`",
        f"- Max file bytes: `{manifest['max_file_bytes']}`",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---:|",
        f"| ok | {counts['ok']} |",
        f"| metadata-only | {counts['metadata_only']} |",
        f"| skipped | {counts['skipped']} |",
        f"| files seen | {counts['files']} |",
        f"| directories recorded | {counts['directories']} |",
        "",
        "## Converted Files",
        "",
    ]

    if ok:
        for entry in ok[:200]:
            lines.append(f"- `{entry['source_path']}` -> `{entry['output_path']}` ({entry['converter']})")
        if len(ok) > 200:
            lines.append(f"- ... {len(ok) - 200} more")
    else:
        lines.append("- None")

    lines.extend(["", "## Metadata-Only Files", ""])
    if metadata_only:
        for entry in metadata_only[:200]:
            reason = entry.get("reason") or "metadata-only"
            lines.append(f"- `{entry['source_path']}` -> `{entry['output_path']}` ({reason})")
        if len(metadata_only) > 200:
            lines.append(f"- ... {len(metadata_only) - 200} more")
    else:
        lines.append("- None")

    lines.extend(["", "## Skipped Directories", ""])
    if skipped:
        for entry in skipped:
            lines.append(f"- `{entry['source_path']}` ({entry.get('reason')})")
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This command does not call an LLM API.",
            "- Code, Markdown, and text-like files are preserved by direct wrapping.",
            "- MarkItDown is used for document-style formats when available.",
            "- Media, archives, model weights, oversized files, and failed conversions are represented by metadata-only pages.",
        ]
    )

    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a raw Markdown evidence layer for a project.")
    parser.add_argument("project_root", help="Project directory to convert.")
    parser.add_argument(
        "--wiki-root",
        help="Output wiki root. Defaults to <project-root>/<project-name>-wiki.",
    )
    parser.add_argument(
        "--scope",
        default="primary",
        help="Raw-md scope directory name under raw-md/. Default: primary.",
    )
    parser.add_argument(
        "--max-file-mb",
        type=int,
        default=DEFAULT_MAX_FILE_MB,
        help=f"Files larger than this become metadata-only. Default: {DEFAULT_MAX_FILE_MB}.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the existing raw-md/<scope>/ output before writing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.exists() or not project_root.is_dir():
        print(f"Error: project root does not exist or is not a directory: {project_root}", file=sys.stderr)
        return 1

    wiki_root = (
        Path(args.wiki_root).expanduser().resolve()
        if args.wiki_root
        else project_root / f"{project_root.name}-wiki"
    )

    config = RawMdConfig(
        project_root=project_root,
        wiki_root=wiki_root,
        scope=args.scope,
        max_file_bytes=args.max_file_mb * 1024 * 1024,
        clean=args.clean,
    )

    builder = RawMdBuilder(config)
    manifest = builder.run()
    counts = manifest["counts"]
    print(f"raw-md complete: {counts['ok']} ok, {counts['metadata_only']} metadata-only, {counts['skipped']} skipped")
    print(f"raw-md root: {config.raw_md_root}")
    print(f"manifest: {config.state_dir / 'raw-md-manifest.json'}")
    print(f"report: {config.reports_dir / 'raw-md-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
