#!/usr/bin/env python3
"""Validate the immutable DOCX template without third-party dependencies.

This is a release-time validator only.  It does not create a report and it is
never part of case execution.  The validator checks the ZIP safety envelope,
table count, and the first row of every table against the signed structural
manifest in ``references/template-signature.json``.  Checking only the number
of tables is deliberately insufficient: a changed header must fail.
"""
import argparse
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
import xml.etree.ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
DOCUMENT_XML = "word/document.xml"
DEFAULT_SIGNATURE = Path(__file__).resolve().parents[1] / "references" / "template-signature.json"


class ValidationFailure(Exception):
    """A deterministic template validation failure."""


@dataclass(frozen=True)
class TemplateReport:
    """Machine-readable and human-readable validation result."""

    template: str
    signature: str
    source_table_count: int
    expected_source_table_count: int
    formal_table_count: int
    expected_formal_table_count: int
    checked_headers: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "template": self.template,
            "signature": self.signature,
            "source_table_count": self.source_table_count,
            "expected_source_table_count": self.expected_source_table_count,
            "formal_table_count": self.formal_table_count,
            "expected_formal_table_count": self.expected_formal_table_count,
            "checked_headers": self.checked_headers,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def error(message: str) -> None:
    """Write a single diagnostic to stderr."""

    print(f"ERROR: {message}", file=sys.stderr)


def is_safe_zip_name(name: str) -> bool:
    """Reject absolute paths and traversal in an archive member name."""

    if not name or name.startswith("/") or "\\" in name:
        return False
    parts = Path(name).parts
    if not parts or parts[0] in {"/", "\\"} or ".." in parts:
        return False
    return True


def safe_zip_names(archive: zipfile.ZipFile) -> list[str]:
    """Validate ZIP names and reject executable or macro payloads."""

    names: list[str] = []
    seen: set[str] = set()
    for info in archive.infolist():
        name = info.filename
        if not is_safe_zip_name(name):
            raise ValidationFailure(f"unsafe archive member path: {name!r}")
        if name in seen:
            raise ValidationFailure(f"duplicate archive member: {name!r}")
        seen.add(name)
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValidationFailure(f"symbolic link in template archive: {name!r}")
        lower = name.lower()
        if lower.endswith((".exe", ".dll", ".so", ".dylib", ".sh", ".bash")):
            raise ValidationFailure(f"executable member in template archive: {name!r}")
        if lower.endswith("vbaproject.bin") or "vba" in lower:
            raise ValidationFailure(f"macro member in template archive: {name!r}")
        names.append(name)
    return names


def read_signature(path: Path) -> dict[str, Any]:
    """Load and minimally validate the structural signature JSON."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationFailure(f"signature file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailure(f"cannot read signature file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationFailure("signature root must be a JSON object")
    for key in ("source_table_count", "formal_table_count", "first_rows"):
        if key not in raw:
            raise ValidationFailure(f"signature missing key: {key}")
    if not isinstance(raw["first_rows"], list):
        raise ValidationFailure("signature first_rows must be a list")
    if len(raw["first_rows"]) != int(raw["source_table_count"]):
        raise ValidationFailure("signature row count does not match source_table_count")
    return raw


def cell_text(cell: ET.Element) -> str:
    """Return visible Word text from one table cell."""

    return "".join(node.text or "" for node in cell.findall(".//w:t", NS))


def table_first_rows(document_xml: bytes) -> list[list[str]]:
    """Extract each table's first row as normalized visible cell text."""

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as exc:
        raise ValidationFailure(f"word/document.xml is not valid XML: {exc}") from exc
    rows: list[list[str]] = []
    for table in root.findall(".//w:tbl", NS):
        first_row = table.find("./w:tr", NS)
        if first_row is None:
            rows.append([])
            continue
        cells = first_row.findall("./w:tc", NS)
        rows.append([cell_text(cell) for cell in cells])
    return rows


def compare_rows(actual: list[list[str]], expected: list[list[str]]) -> list[str]:
    """Return precise header mismatch diagnostics."""

    errors: list[str] = []
    if len(actual) != len(expected):
        errors.append(f"table count mismatch: actual={len(actual)} expected={len(expected)}")
        return errors
    for index, (actual_row, expected_row) in enumerate(zip(actual, expected)):
        if actual_row != expected_row:
            errors.append(
                f"table {index} first row mismatch: actual={actual_row!r} expected={expected_row!r}"
            )
    return errors


def validate_template(template: Path, signature_path: Path) -> TemplateReport:
    """Validate a template and return a complete report."""

    errors: list[str] = []
    warnings: list[str] = []
    signature = read_signature(signature_path)
    actual_rows: list[list[str]] = []
    try:
        with zipfile.ZipFile(template, "r") as archive:
            names = safe_zip_names(archive)
            if DOCUMENT_XML not in names:
                raise ValidationFailure("template is missing word/document.xml")
            actual_rows = table_first_rows(archive.read(DOCUMENT_XML))
            content_types = archive.read("[Content_Types].xml") if "[Content_Types].xml" in names else b""
            if b"vbaProject" in content_types:
                raise ValidationFailure("template content types declare a VBA project")
    except FileNotFoundError as exc:
        raise ValidationFailure(f"template not found: {template}") from exc
    except zipfile.BadZipFile as exc:
        raise ValidationFailure(f"template is not a valid ZIP/DOCX: {exc}") from exc
    except KeyError as exc:
        raise ValidationFailure(f"required template member is missing: {exc}") from exc
    except OSError as exc:
        raise ValidationFailure(f"cannot read template: {exc}") from exc

    errors.extend(compare_rows(actual_rows, signature["first_rows"]))
    if len(actual_rows) == int(signature["source_table_count"]):
        formal_count = len(actual_rows) - 1
    else:
        formal_count = max(0, len(actual_rows) - 1)
    if len(actual_rows) != int(signature["source_table_count"]):
        errors.append(
            f"source table count mismatch: actual={len(actual_rows)} expected={signature['source_table_count']}"
        )
    if formal_count != int(signature["formal_table_count"]):
        errors.append(
            f"formal table count mismatch: actual={formal_count} expected={signature['formal_table_count']}"
        )
    if not actual_rows:
        errors.append("template has no tables")
    elif actual_rows[0] != signature["first_rows"][0]:
        errors.append("the first table is not the expected removable guide table")
    if len(set(tuple(row) for row in actual_rows)) != len(actual_rows):
        warnings.append("some table first rows are duplicated; review is still required")

    return TemplateReport(
        template=str(template),
        signature=str(signature_path),
        source_table_count=len(actual_rows),
        expected_source_table_count=int(signature["source_table_count"]),
        formal_table_count=formal_count,
        expected_formal_table_count=int(signature["formal_table_count"]),
        checked_headers=min(len(actual_rows), len(signature["first_rows"])),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Build the command-line interface."""

    parser = argparse.ArgumentParser(
        description="Validate DOCX template safety, table count, and every table first row."
    )
    parser.add_argument("template", type=Path, help="Path to reference-template.docx")
    parser.add_argument(
        "--signature",
        type=Path,
        default=DEFAULT_SIGNATURE,
        help="Structural signature JSON (default: package references/template-signature.json)",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON result")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry point with stable exit codes."""

    args = parse_args(argv)
    try:
        report = validate_template(args.template.resolve(), args.signature.resolve())
    except ValidationFailure as exc:
        if args.json:
            print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False))
        else:
            error(str(exc))
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        if report.ok:
            print(
                "OK: source_tables="
                f"{report.source_table_count}; formal_tables={report.formal_table_count}; "
                f"headers_checked={report.checked_headers}"
            )
        else:
            for message in report.errors:
                error(message)
            for message in report.warnings:
                print(f"WARNING: {message}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
