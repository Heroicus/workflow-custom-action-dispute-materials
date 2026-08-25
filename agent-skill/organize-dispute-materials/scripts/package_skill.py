#!/usr/bin/env python3
"""Create a clean root-level ZIP package for the Skill.

The packager is intentionally conservative.  It rejects hidden files,
symlinks, path traversal, bytecode, local IDE state, and files outside the
source root.  The resulting archive has ``SKILL.md`` at its root, which is the
layout expected by the Aily import flow.
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

EXCLUDED_NAMES = {".DS_Store", "Thumbs.db"}
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


class PackageFailure(Exception):
    """A source tree is not safe to package."""


@dataclass(frozen=True)
class PackageFile:
    """One archive member and its digest."""

    source: Path
    member: str
    sha256: str
    size: int


def fail(message: str) -> None:
    """Write a stable error."""

    print(f"ERROR: {message}", file=sys.stderr)


def is_hidden_relative(path: Path) -> bool:
    """Return true if any component is a hidden metadata component."""

    return any(part.startswith(".") for part in path.parts)


def validate_source(root: Path) -> Path:
    """Validate the source root and required package files."""

    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise PackageFailure(f"source is not a directory: {resolved}")
    required = [
        resolved / "SKILL.md",
        resolved / "README.md",
        resolved / "agents" / "openai.yaml",
        resolved / "references" / "feishu-runtime-contract.md",
        resolved / "references" / "template-signature.json",
        resolved / "assets" / "reference-template.docx",
        resolved / "scripts" / "validate_template.py",
        resolved / "scripts" / "inventory_attachments.py",
        resolved / "scripts" / "validate_delivery.py",
    ]
    missing = [str(item.relative_to(resolved)) for item in required if not item.is_file()]
    if missing:
        raise PackageFailure(f"required package files are missing: {', '.join(missing)}")
    return resolved


def sha256_file(path: Path) -> str:
    """Hash a regular file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_files(root: Path) -> list[PackageFile]:
    """Collect only regular, non-hidden files below root."""

    collected: list[PackageFile] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(root)
            if name in EXCLUDED_DIRS:
                continue
            if path.is_symlink():
                raise PackageFailure(f"symbolic link directory is forbidden: {relative}")
            if is_hidden_relative(relative):
                raise PackageFailure(f"hidden directory is forbidden: {relative}")
            kept_dirs.append(name)
        directories[:] = kept_dirs
        for name in sorted(names):
            path = current_path / name
            relative = path.relative_to(root)
            if path.is_symlink():
                raise PackageFailure(f"symbolic link file is forbidden: {relative}")
            if is_hidden_relative(relative) or name in EXCLUDED_NAMES:
                raise PackageFailure(f"hidden or metadata file is forbidden: {relative}")
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                raise PackageFailure(f"bytecode file is forbidden: {relative}")
            if not path.is_file():
                raise PackageFailure(f"non-regular file is forbidden: {relative}")
            mode = path.stat().st_mode & 0o777
            if mode & 0o111:
                raise PackageFailure(f"executable bit is forbidden in package source: {relative}")
            member = relative.as_posix()
            collected.append(PackageFile(path, member, sha256_file(path), path.stat().st_size))
    collected.sort(key=lambda item: item.member)
    return collected


def write_package(files: list[PackageFile], output: Path) -> dict[str, object]:
    """Write a deterministic ZIP and return its manifest."""

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.is_dir():
        raise PackageFailure(f"output is a directory: {output}")
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in files:
            info = zipfile.ZipInfo(item.member)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, item.source.read_bytes())
    temporary.replace(output)
    return {
        "output": str(output),
        "sha256": sha256_file(output),
        "file_count": len(files),
        "uncompressed_bytes": sum(item.size for item in files),
        "members": [{"path": item.member, "bytes": item.size, "sha256": item.sha256} for item in files],
    }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Build the CLI parser."""

    parser = argparse.ArgumentParser(description="Package a clean root-level Aily Skill ZIP.")
    parser.add_argument("--source", required=True, type=Path, help="Skill source directory")
    parser.add_argument("--output", required=True, type=Path, help="Output ZIP path")
    parser.add_argument("--json", action="store_true", help="Print the full manifest JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry point."""

    args = parse_args(argv)
    try:
        root = validate_source(args.source)
        files = collect_files(root)
        if not any(item.member == "SKILL.md" for item in files):
            raise PackageFailure("SKILL.md is not at the archive root")
        manifest = write_package(files, args.output)
    except (PackageFailure, OSError, zipfile.BadZipFile) as exc:
        fail(str(exc))
        return 2
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({key: manifest[key] for key in ("output", "sha256", "file_count", "uncompressed_bytes")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
