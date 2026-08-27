#!/usr/bin/env python3
"""Create a clean root-level ZIP package for the Skill.

The packager is intentionally conservative.  It packages an explicit
production allow-list only, rejects symlinks and unsafe members, and never
recursively sweeps the source tree.  The resulting archive has ``SKILL.md`` at
its root, which is the layout expected by the Aily import flow.
"""
import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

PACKAGE_MEMBERS = (
    "SKILL.md",
    "README.md",
    "agents/openai.yaml",
    "references/feishu-runtime-contract.md",
    "references/render-contract.md",
    "references/render-schema.json",
    "references/report-template.xml",
    "references/audio-contract.md",
    "references/audio-result-schema.json",
    "references/vision-contract.md",
    "references/vision-result-schema.json",
    "scripts/audio_tool.py",
    "scripts/material_tool.py",
    "scripts/report_tool.py",
    "scripts/vision_tool.py",
)


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
    required = [resolved / member for member in PACKAGE_MEMBERS]
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
    """Collect the explicit production allow-list and nothing else."""

    collected: list[PackageFile] = []
    for member in sorted(PACKAGE_MEMBERS):
        relative = Path(member)
        if relative.is_absolute() or ".." in relative.parts or is_hidden_relative(relative):
            raise PackageFailure(f"unsafe package member: {member}")
        path = root / relative
        if path.is_symlink():
            raise PackageFailure(f"symbolic link file is forbidden: {relative}")
        if not path.is_file():
            raise PackageFailure(f"required package file is missing: {relative}")
        collected.append(PackageFile(path, relative.as_posix(), sha256_file(path), path.stat().st_size))
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
