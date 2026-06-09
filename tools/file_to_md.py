#!/usr/bin/env python3
from __future__ import annotations

"""
Compatibility wrapper for older directory-to-Markdown workflows.

Default behavior now delegates to tools/raw_md.py so source files are not
modified and generated Markdown is kept in a project wiki evidence layer.

Use --legacy-adjacent-output only when you intentionally want the old behavior
of writing converted .md files next to the originals.
"""

import argparse
import sys
from pathlib import Path

from markitdown import MarkItDown
from tqdm import tqdm

from raw_md import RawMdBuilder, RawMdConfig


def convert_directory_to_md_legacy(input_dir: Path, delete_source: bool = False) -> None:
    """Legacy adjacent-file conversion.

    Converts files to Markdown beside the source files. This is intentionally
    no longer the default because it mixes generated artifacts into raw inputs.
    """
    md = MarkItDown(enable_plugins=False)
    files_to_process = [f for f in input_dir.rglob("*") if f.is_file()]

    if not files_to_process:
        print(f"No files found in {input_dir}!")
        return

    for file_path in tqdm(files_to_process, desc="Converting Files"):
        if file_path.name.startswith(".") or file_path.suffix.lower() == ".md":
            tqdm.write(f"Skipping conversion of {file_path.name}")
            continue

        output_path = file_path.with_suffix(".md")
        try:
            result = md.convert(str(file_path))
            output_path.write_text(result.text_content, encoding="utf-8")
            if delete_source:
                file_path.unlink()
            tqdm.write(f"Converted: {file_path.name} -> {output_path.name}")
        except Exception as exc:
            tqdm.write(f"FAILED: Could not convert '{file_path.name}'. Reason: {exc}")


def build_raw_md(input_dir: Path, clean: bool = False) -> None:
    wiki_root = input_dir / f"{input_dir.name}-wiki"
    config = RawMdConfig(project_root=input_dir, wiki_root=wiki_root, clean=clean)
    manifest = RawMdBuilder(config).run()
    counts = manifest["counts"]
    print(
        "raw-md complete: "
        f"{counts['ok']} ok, {counts['metadata_only']} metadata-only, {counts['skipped']} skipped"
    )
    print(f"raw-md root: {config.raw_md_root}")
    print(f"manifest: {config.state_dir / 'raw-md-manifest.json'}")
    print(f"report: {config.reports_dir / 'raw-md-report.md'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a directory to Markdown evidence. Defaults to safe raw-md output."
    )
    parser.add_argument("--input_dir", type=str, required=True, help="Directory containing files to convert.")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="When using safe raw-md output, remove existing raw-md/<scope>/ before writing.",
    )
    parser.add_argument(
        "--legacy-adjacent-output",
        action="store_true",
        help="Use old behavior: write converted .md files beside source files.",
    )
    parser.add_argument(
        "--delete_source",
        action="store_true",
        help="Legacy only. Delete originals after conversion; requires --allow-delete-source.",
    )
    parser.add_argument(
        "--allow-delete-source",
        action="store_true",
        help="Required confirmation for --delete_source in legacy adjacent-output mode.",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input_dir).resolve()
    if not input_path.exists() or not input_path.is_dir():
        print(f"Error: input directory not found: {input_path}", file=sys.stderr)
        return 1

    if args.delete_source and not (args.legacy_adjacent_output and args.allow_delete_source):
        print(
            "Error: --delete_source is disabled by default. "
            "Use --legacy-adjacent-output --delete_source --allow-delete-source "
            "only if you intentionally want to delete original files.",
            file=sys.stderr,
        )
        return 2

    print("-" * 40)
    print(f"Input Directory: {input_path}")
    print("-" * 40)

    if args.legacy_adjacent_output:
        print("Warning: using legacy adjacent-output mode. Generated .md files will be written beside sources.")
        convert_directory_to_md_legacy(input_path, delete_source=args.delete_source)
    else:
        print("Using safe raw-md output. Source files will not be modified.")
        build_raw_md(input_path, clean=args.clean)

    print("\nConversion process complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
