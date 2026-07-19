"""Build the self-contained J-05B Codex Plugin release for Windows x86-64."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SOURCE = REPO_ROOT / "plugins" / "llmwiki-research"
VERSION = "0.2.0"
BASENAME = f"llmwiki-research-{VERSION}-windows-x86_64"
MARKETPLACE_NAME = "llmwiki-research-release"
RELEASE_FILES = (
    "install-common.ps1",
    "install.ps1",
    "uninstall.ps1",
    "rollback.ps1",
    "RELEASE_NOTES.md",
)
EXCLUDED_PLUGIN_PARTS = {"runtime", "release", "__pycache__"}


class ReleaseBuildError(RuntimeError):
    """Raised when deterministic release construction cannot continue."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def copy_plugin_template(target: Path) -> None:
    for source in sorted(PLUGIN_SOURCE.rglob("*")):
        relative = source.relative_to(PLUGIN_SOURCE)
        if any(part in EXCLUDED_PLUGIN_PARTS for part in relative.parts):
            continue
        destination = target / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def copy_core(target: Path) -> list[str]:
    manifest_path = PLUGIN_SOURCE / "release" / "core-files.txt"
    listed = [
        line.strip()
        for line in manifest_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not listed or len(listed) != len(set(listed)):
        raise ReleaseBuildError("The bundled Core file manifest is invalid.")
    for relative_text in listed:
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ReleaseBuildError("The bundled Core file manifest is unsafe.")
        source = REPO_ROOT / relative
        if not source.is_file():
            raise ReleaseBuildError("A declared bundled Core file is missing.")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    asset_root = REPO_ROOT / "assets" / "research-cockpit"
    if not asset_root.is_dir():
        raise ReleaseBuildError("The research cockpit assets are missing.")
    shutil.copytree(asset_root, target / "assets" / "research-cockpit")
    shutil.copyfile(REPO_ROOT / "LICENSE", target / "LICENSE")
    return listed


def read_runtime_lock() -> dict[str, Any]:
    path = PLUGIN_SOURCE / "release" / "python-runtime-win-amd64.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "platform",
        "architecture",
        "python_version",
        "implementation",
        "archive",
        "url",
        "sha256",
    }
    if set(payload) != required or payload["schema_version"] != 1:
        raise ReleaseBuildError("The Python runtime lock is incompatible.")
    return payload


def acquire(url: str, destination: Path, expected_hash: str, *, offline: bool) -> None:
    if destination.is_file() and sha256(destination) == expected_hash:
        return
    if destination.exists():
        destination.unlink()
    if offline:
        raise ReleaseBuildError("A locked release artifact is not available offline.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with (
            urllib.request.urlopen(url, timeout=120) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        if sha256(temporary) != expected_hash:
            raise ReleaseBuildError(
                "A downloaded release artifact failed SHA-256 validation."
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def requirement_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        marker = "--hash=sha256:"
        if marker not in line:
            raise ReleaseBuildError("A runtime requirement is not hash locked.")
        value = line.split(marker, 1)[1].split()[0]
        if len(value) != 64:
            raise ReleaseBuildError("A runtime requirement hash is malformed.")
        hashes.add(value)
    return hashes


def prepare_wheels(cache: Path, requirements: Path, *, offline: bool) -> Path:
    wheel_dir = cache / "wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    expected = requirement_hashes(requirements)
    observed = {sha256(path) for path in wheel_dir.glob("*.whl")}
    if not expected.issubset(observed):
        if offline:
            raise ReleaseBuildError("Locked runtime wheels are not available offline.")
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "--only-binary=:all:",
            "--require-hashes",
            "--platform",
            "win_amd64",
            "--python-version",
            "3.13",
            "--implementation",
            "cp",
            "--abi",
            "cp313",
            "--dest",
            str(wheel_dir),
            "--requirement",
            str(requirements),
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ReleaseBuildError("Locked runtime wheel acquisition failed.")
        observed = {sha256(path) for path in wheel_dir.glob("*.whl")}
    if expected != expected.intersection(observed):
        raise ReleaseBuildError("The runtime wheel cache is incomplete.")
    return wheel_dir


def install_runtime(
    python_archive: Path,
    wheel_dir: Path,
    requirements: Path,
    runtime_root: Path,
) -> None:
    python_root = runtime_root / "python"
    python_root.mkdir(parents=True)
    with zipfile.ZipFile(python_archive) as archive:
        archive.extractall(python_root)
    pth_files = list(python_root.glob("python*._pth"))
    if len(pth_files) != 1:
        raise ReleaseBuildError("The embedded Python path file is unavailable.")
    pth_files[0].write_text(
        "python313.zip\n.\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
        newline="\r\n",
    )
    site_packages = python_root / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-deps",
        "--no-compile",
        "--require-hashes",
        "--find-links",
        str(wheel_dir),
        "--target",
        str(site_packages),
        "--requirement",
        str(requirements),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ReleaseBuildError("Installing the locked runtime wheels failed.")
    python_executable = python_root / "python.exe"
    smoke = subprocess.run(
        [
            str(python_executable),
            "-I",
            "-B",
            "-c",
            "import jsonschema,mcp,openpyxl,PIL,pypdf,yaml; print('runtime-ok')",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if smoke.returncode != 0 or smoke.stdout.strip() != "runtime-ok":
        raise ReleaseBuildError(
            "The embedded Python runtime failed its import smoke test."
        )


def git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def package_inventory(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.name != "SHA256SUMS"
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def write_inventory(root: Path) -> None:
    listed = {path.relative_to(root).as_posix() for path in package_inventory(root)} | {
        "FILELIST.txt",
        "SHA256SUMS",
    }
    (root / "FILELIST.txt").write_text(
        "\n".join(sorted(listed)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    files = package_inventory(root)
    (root / "SHA256SUMS").write_text(
        "\n".join(
            f"{sha256(path)}  {path.relative_to(root).as_posix()}" for path in files
        )
        + "\n",
        encoding="ascii",
        newline="\n",
    )


def deterministic_zip(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(
            source.rglob("*"), key=lambda p: p.relative_to(source.parent).as_posix()
        ):
            if not path.is_file():
                continue
            relative = path.relative_to(source.parent).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 7, 19, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build(output_dir: Path, cache_dir: Path, *, offline: bool) -> dict[str, Any]:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ReleaseBuildError("J-05B release construction requires Windows x86-64.")
    if sys.version_info[:2] != (3, 13):
        raise ReleaseBuildError(
            "J-05B release construction requires a Python 3.13 build host."
        )
    manifest = json.loads(
        (PLUGIN_SOURCE / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    if manifest.get("name") != "llmwiki-research" or manifest.get("version") != VERSION:
        raise ReleaseBuildError(
            "The Plugin manifest version does not match the release builder."
        )
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    runtime_lock = read_runtime_lock()
    python_archive = cache_dir / str(runtime_lock["archive"])
    acquire(
        str(runtime_lock["url"]),
        python_archive,
        str(runtime_lock["sha256"]),
        offline=offline,
    )
    requirements = PLUGIN_SOURCE / "release" / "requirements-win-amd64.txt"
    wheel_dir = prepare_wheels(cache_dir, requirements, offline=offline)
    with tempfile.TemporaryDirectory(
        prefix="llmwiki-release-", dir=output_dir
    ) as temporary:
        stage = Path(temporary) / BASENAME
        plugin_target = stage / "plugins" / "llmwiki-research"
        plugin_target.mkdir(parents=True)
        copy_plugin_template(plugin_target)
        core_files = copy_core(plugin_target / "runtime" / "core")
        install_runtime(
            python_archive, wheel_dir, requirements, plugin_target / "runtime"
        )
        write_json(
            plugin_target / "runtime" / "core" / "BUILD.json",
            {
                "schema_version": 1,
                "kind": "llmwiki-bundled-core",
                "plugin_version": VERSION,
                "source_revision": git_revision(),
                "files": core_files,
                "c07": {"status": "deferred", "processing_status": "not_started"},
            },
        )
        marketplace = {
            "name": MARKETPLACE_NAME,
            "interface": {"displayName": "LLM Wiki Research Release"},
            "plugins": [
                {
                    "name": "llmwiki-research",
                    "source": {"source": "local", "path": "./plugins/llmwiki-research"},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Productivity",
                }
            ],
        }
        write_json(stage / ".agents" / "plugins" / "marketplace.json", marketplace)
        for name in RELEASE_FILES:
            shutil.copyfile(PLUGIN_SOURCE / "release" / name, stage / name)
        write_json(
            stage / "release.json",
            {
                "schema_version": 1,
                "kind": "llmwiki-codex-plugin-release",
                "plugin_name": "llmwiki-research",
                "version": VERSION,
                "marketplace_name": MARKETPLACE_NAME,
                "platform": "windows",
                "architecture": "x86_64",
                "python_version": runtime_lock["python_version"],
                "source_revision": git_revision(),
                "default_workspace": "%LOCALAPPDATA%\\LLMWiki\\workspace",
            },
        )
        write_inventory(stage)
        forbidden = str(REPO_ROOT).encode("utf-8")
        for path in package_inventory(stage):
            if (
                path.suffix.lower() in {".py", ".json", ".md", ".txt", ".ps1", ".cmd"}
                and forbidden in path.read_bytes()
            ):
                raise ReleaseBuildError(
                    "A release text file contains the source checkout path."
                )
        final_dir = output_dir / BASENAME
        if final_dir.exists():
            shutil.rmtree(final_dir)
        shutil.move(str(stage), final_dir)
    zip_path = output_dir / f"{BASENAME}.zip"
    zip_path.unlink(missing_ok=True)
    deterministic_zip(final_dir, zip_path)
    zip_hash = sha256(zip_path)
    hash_path = output_dir / f"{BASENAME}.zip.sha256"
    hash_path.write_text(
        f"{zip_hash}  {zip_path.name}\n", encoding="ascii", newline="\n"
    )
    result = {
        "ok": True,
        "version": VERSION,
        "release_directory": str(final_dir),
        "archive": str(zip_path),
        "sha256": zip_hash,
        "file_count": len(package_inventory(final_dir)) + 1,
    }
    return result


def default_cache_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local and Path(local).is_absolute():
        return Path(local) / "LLMWiki" / "release-cache" / "j-05b"
    return Path.home() / ".cache" / "llmwiki" / "release" / "j-05b"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        result = build(args.output_dir, args.cache_dir, offline=args.offline)
    except (OSError, ValueError, ReleaseBuildError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Built {result['archive']}")
        print(f"SHA-256 {result['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
