#!/usr/bin/env python3
"""Build a safe, deterministic inventory for one local case directory.

The inventory helper is a release/test utility, not the Aily runtime.  It
never follows symbolic links, never executes a file, rejects an output file
inside the input tree, and avoids writing the caller's absolute path into the
JSON result.  File contents are hashed only after boundary checks pass.
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

IGNORED_NAMES = {".DS_Store", "Thumbs.db"}
IGNORED_PREFIXES = ("._",)
DEFAULT_MAX_BYTES = 512 * 1024 * 1024


class InventoryFailure(Exception):
    """A deterministic inventory input failure."""


@dataclass(frozen=True)
class InventoryConfig:
    """Validated inventory options."""

    input_root: Path
    output_path: Path
    case_id: str
    max_file_bytes: int


@dataclass(frozen=True)
class FileEntry:
    """Safe metadata for one regular file."""

    relative_path: str
    bytes: int
    extension: str
    sha256: Optional[str]
    hash_status: str
    readability_hint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "bytes": self.bytes,
            "extension": self.extension,
            "sha256": self.sha256,
            "hash_status": self.hash_status,
            "readability_hint": self.readability_hint,
        }


def fail(message: str) -> None:
    """Write a stable error message."""

    print(f"ERROR: {message}", file=sys.stderr)


def path_is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path is inside root, excluding root itself."""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def validate_config(input_path: Path, output_path: Path, case_id: str, max_file_bytes: int) -> InventoryConfig:
    """Resolve and validate input/output boundaries before scanning."""

    if max_file_bytes <= 0:
        raise InventoryFailure("--max-file-bytes must be positive")
    root = input_path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise InventoryFailure(f"input is not a directory: {root}")
    if root.is_symlink():
        raise InventoryFailure("input root must not be a symbolic link")
    output = output_path.expanduser().resolve()
    if path_is_within(output, root):
        raise InventoryFailure("output must be outside the input directory")
    safe_case_id = case_id.strip()
    if not safe_case_id or any(char in safe_case_id for char in "\\/\x00\n\r"):
        raise InventoryFailure("case id must be a non-empty single-line label")
    return InventoryConfig(root, output, safe_case_id, max_file_bytes)


def sha256_file(path: Path, max_file_bytes: int) -> Tuple[Optional[str], str]:
    """Hash a regular file, bounded by the configured size limit."""

    size = path.stat().st_size
    if size > max_file_bytes:
        return None, "skipped_size_limit"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), "computed"


def readability_hint(extension: str) -> str:
    """Classify only the need for a downstream reader; never claim readability."""

    if extension in {".m4a", ".mp3", ".wav", ".aac", ".larkcache"}:
        return "requires_authorized_media_reader"
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp"}:
        return "requires_image_reader_or_ocr"
    if extension in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".md"}:
        return "candidate_for_authorized_reader"
    return "requires_format_specific_reader"


def iter_regular_files(root: Path) -> tuple[list[Path], list[dict[str, str]]]:
    """Walk without following symlinked directories or files."""

    files: list[Path] = []
    skipped: list[dict[str, str]] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                skipped.append({"path": candidate.relative_to(root).as_posix(), "reason": "symbolic_link"})
            else:
                kept_dirs.append(name)
        directories[:] = kept_dirs
        for name in sorted(names):
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            if name in IGNORED_NAMES or name.startswith(IGNORED_PREFIXES):
                skipped.append({"path": relative, "reason": "ignored_metadata_file"})
                continue
            if candidate.is_symlink():
                skipped.append({"path": relative, "reason": "symbolic_link"})
                continue
            if not candidate.is_file():
                skipped.append({"path": relative, "reason": "not_regular_file"})
                continue
            files.append(candidate)
    return files, skipped


def build_inventory(config: InventoryConfig) -> dict[str, Any]:
    """Build a stable inventory without leaking the absolute root path."""

    paths, skipped = iter_regular_files(config.input_root)
    entries: list[FileEntry] = []
    errors: list[str] = []
    for path in paths:
        relative = path.relative_to(config.input_root).as_posix()
        try:
            size = path.stat().st_size
            digest, status = sha256_file(path, config.max_file_bytes)
        except (OSError, ValueError) as exc:
            errors.append(f"{relative}: {exc}")
            continue
        extension = path.suffix.lower() or "[no_extension]"
        entries.append(
            FileEntry(
                relative_path=relative,
                bytes=size,
                extension=extension,
                sha256=digest,
                hash_status=status,
                readability_hint=readability_hint(extension),
            )
        )
    entries.sort(key=lambda item: item.relative_path)
    hash_groups: dict[str, list[str]] = {}
    for entry in entries:
        if entry.sha256:
            hash_groups.setdefault(entry.sha256, []).append(entry.relative_path)
    duplicates = {key: value for key, value in hash_groups.items() if len(value) > 1}
    return {
        "schema_version": "1.1",
        "case_id": config.case_id,
        "file_count": len(entries),
        "type_counts": dict(sorted(Counter(entry.extension for entry in entries).items())),
        "duplicate_content": duplicates,
        "skipped": skipped,
        "errors": errors,
        "files": [entry.as_dict() for entry in entries],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write output atomically enough for a local release/test utility."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Build the CLI parser."""

    parser = argparse.ArgumentParser(description="Create a bounded case attachment inventory.")
    parser.add_argument("--input", required=True, type=Path, help="One local case directory")
    parser.add_argument("--output", required=True, type=Path, help="Output JSON outside the input tree")
    parser.add_argument("--case-id", default="local-case", help="Non-sensitive label for the inventory")
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Do not hash files larger than this (default: {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument("--json", action="store_true", help="Print the complete inventory JSON")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry point."""

    args = parse_args(argv)
    try:
        config = validate_config(args.input, args.output, args.case_id, args.max_file_bytes)
        payload = build_inventory(config)
        write_json(config.output_path, payload)
    except (InventoryFailure, OSError) as exc:
        fail(str(exc))
        return 2
    summary = {
        "ok": not payload["errors"],
        "case_id": payload["case_id"],
        "file_count": payload["file_count"],
        "duplicate_groups": len(payload["duplicate_content"]),
        "skipped_count": len(payload["skipped"]),
        "error_count": len(payload["errors"]),
        "output": str(config.output_path),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if not payload["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
