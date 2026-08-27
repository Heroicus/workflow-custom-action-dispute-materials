#!/usr/bin/env python3
"""Deterministically render and validate the fixed dispute report XML."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SKILL_ROOT = Path(__file__).resolve().parent.parent
REFERENCES = SKILL_ROOT / "references"
DEFAULT_TEMPLATE = REFERENCES / "report-template.xml"
DEFAULT_SCHEMA = REFERENCES / "render-schema.json"
SCALAR_PATTERN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")
ROW_PATTERN = re.compile(r"<!--([A-Za-z][A-Za-z0-9_]*_rows)-->")
UNRESOLVED_PATTERN = re.compile(r"\{\{[^{}]+\}\}")
WHITESPACE_PATTERN = re.compile(r"\s+")
NUMERIC_LITERAL_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![A-Za-z0-9])")


class ReportError(Exception):
    """A stable rendering or validation failure."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError("JSON_INVALID", f"无法读取 JSON：{path}", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise ReportError("JSON_INVALID", f"JSON 根节点必须是对象：{path}")
    return value


def scalar_text(value: Any, path: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    raise ReportError("FACT_VALUE_INVALID", f"{path} 必须是字符串、数字、布尔值或 null")


def escaped(value: Any, path: str) -> str:
    text = scalar_text(value, path)
    return html.escape(text, quote=False).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")


def load_contract(template_path: Path, schema_path: Path) -> tuple[str, dict[str, Any]]:
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("TEMPLATE_UNAVAILABLE", f"无法读取模板：{template_path}", {"reason": str(exc)}) from exc
    schema = read_json(schema_path)
    rows = schema.get("dynamic_rows")
    structure = schema.get("structure")
    if not isinstance(rows, dict) or not isinstance(structure, dict):
        raise ReportError("SCHEMA_INVALID", "render-schema.json 缺少 dynamic_rows 或 structure")
    template_rows = set(ROW_PATTERN.findall(template))
    schema_rows = set(rows)
    if template_rows != schema_rows:
        raise ReportError(
            "SCHEMA_INVALID",
            "模板行标记与渲染结构不一致",
            {"template_only": sorted(template_rows - schema_rows), "schema_only": sorted(schema_rows - template_rows)},
        )
    return template, schema


def render_row(marker: str, columns: Sequence[str], row: Any, index: int) -> str:
    if not isinstance(row, dict):
        raise ReportError("FACT_ROW_INVALID", f"rows.{marker}[{index}] 必须是对象")
    unknown = sorted(set(row) - set(columns))
    if unknown:
        raise ReportError("FACT_ROW_INVALID", f"rows.{marker}[{index}] 包含未知字段", {"unknown": unknown})
    cells = "".join(
        f"<td><p>{escaped(row.get(column), f'rows.{marker}[{index}].{column}')}</p></td>"
        for column in columns
    )
    return f"<tr>{cells}</tr>"


def render_report(facts: dict[str, Any], template: str, schema: dict[str, Any]) -> str:
    scalars = facts.get("scalars", {})
    rows = facts.get("rows", {})
    if not isinstance(scalars, dict) or not isinstance(rows, dict):
        raise ReportError("FACTS_INVALID", "facts 必须包含对象类型的 scalars 和 rows")

    scalar_markers = set(SCALAR_PATTERN.findall(template))
    unknown_scalars = sorted(set(scalars) - scalar_markers)
    unknown_rows = sorted(set(rows) - set(schema["dynamic_rows"]))
    if unknown_scalars or unknown_rows:
        raise ReportError(
            "FACTS_INVALID",
            "facts 包含模板未定义字段",
            {"unknown_scalars": unknown_scalars, "unknown_rows": unknown_rows},
        )
    for name in schema.get("required_scalars", []):
        if not scalar_text(scalars.get(name), f"scalars.{name}"):
            raise ReportError("REQUIRED_FACT_MISSING", f"缺少必填字段 scalars.{name}")

    case_number = scalar_text(scalars.get("case_number"), "scalars.case_number")
    prepared_scalars = dict(scalars)
    if not scalar_text(prepared_scalars.get("document_title"), "scalars.document_title"):
        prepared_scalars["document_title"] = f"{case_number} 诉讼/仲裁案件材料梳理报告"

    output = template
    for marker in sorted(scalar_markers):
        output = output.replace(f"{{{{{marker}}}}}", escaped(prepared_scalars.get(marker), f"scalars.{marker}"))
    for marker, columns in schema["dynamic_rows"].items():
        values = rows.get(marker, [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ReportError("FACT_ROW_INVALID", f"rows.{marker} 必须是数组")
        replacement = "".join(render_row(marker, columns, row, index) for index, row in enumerate(values))
        output = output.replace(f"<!--{marker}-->", replacement)

    leftovers = sorted(set(SCALAR_PATTERN.findall(output)) | set(ROW_PATTERN.findall(output)))
    if leftovers or UNRESOLVED_PATTERN.search(output):
        raise ReportError("TEMPLATE_MARKER_REMAINS", "渲染结果仍有模板标记", {"markers": leftovers})
    validate_report(output, template, schema, facts)
    return output


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_fragment(source: str) -> ET.Element:
    cleaned = source.strip().lstrip("\ufeff")
    if cleaned.startswith("<?xml"):
        cleaned = re.sub(r"^<\?xml[^>]*\?>", "", cleaned, count=1).lstrip()
    try:
        return ET.fromstring(f"<report-root>{cleaned}</report-root>")
    except ET.ParseError as exc:
        raise ReportError("REPORT_XML_INVALID", "报告正文不是有效 XML", {"reason": str(exc)}) from exc


def normalize_text(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", html.unescape(value or "")).strip()


def element_text(element: ET.Element) -> str:
    return normalize_text("".join(element.itertext()))


def table_rows(table: ET.Element) -> list[list[str]]:
    return [
        [element_text(cell) for cell in list(row) if local_name(cell.tag) in {"td", "th"}]
        for row in table.iter()
        if local_name(row.tag) == "tr"
    ]


def template_cell_pattern(value: str) -> tuple[str, bool]:
    return normalize_text(SCALAR_PATTERN.sub("", value)), bool(SCALAR_PATTERN.search(value))


def structure_signature(root: ET.Element) -> list[tuple[str, Any]]:
    signature: list[tuple[str, Any]] = []
    for element in root.iter():
        name = local_name(element.tag)
        if name in {"h1", "h2"}:
            signature.append((name, element_text(element)))
        elif name == "table":
            signature.append((name, table_rows(element)))
    return signature


def dynamic_table_flags(template: str) -> list[bool]:
    tables = re.findall(r"<table(?:\s[^>]*)?>.*?</table>", template, flags=re.DOTALL | re.IGNORECASE)
    return [bool(ROW_PATTERN.search(table)) for table in tables]


def compare_table(
    expected_rows: list[list[str]],
    actual_rows: list[list[str]],
    allows_dynamic_rows: bool,
    position: int,
) -> None:
    if allows_dynamic_rows:
        if len(actual_rows) < len(expected_rows):
            raise ReportError("REPORT_STRUCTURE_INVALID", "动态表格缺少固定行", {"position": position})
    elif len(expected_rows) != len(actual_rows):
        raise ReportError(
            "REPORT_STRUCTURE_INVALID",
            "固定表格行数不正确",
            {"position": position, "expected": len(expected_rows), "actual": len(actual_rows)},
        )
    for row_index, (expected_cells, actual_cells) in enumerate(zip(expected_rows, actual_rows)):
        if len(expected_cells) != len(actual_cells):
            raise ReportError(
                "REPORT_STRUCTURE_INVALID",
                "表格列数不正确",
                {"position": position, "row": row_index},
            )
        for cell_index, (template_cell, actual_cell) in enumerate(zip(expected_cells, actual_cells)):
            static_text, has_marker = template_cell_pattern(template_cell)
            if has_marker:
                if static_text and static_text not in actual_cell:
                    raise ReportError(
                        "REPORT_STRUCTURE_INVALID",
                        "表格固定标签不正确",
                        {"position": position, "row": row_index, "cell": cell_index},
                    )
            elif template_cell != actual_cell:
                raise ReportError(
                    "REPORT_STRUCTURE_INVALID",
                    "表格固定内容不正确",
                    {
                        "position": position,
                        "row": row_index,
                        "cell": cell_index,
                        "expected": template_cell,
                        "actual": actual_cell,
                    },
                )


def compare_structure(actual: ET.Element, template_root: ET.Element, template: str, schema: dict[str, Any]) -> None:
    expected = structure_signature(template_root)
    received = structure_signature(actual)
    expected_counts = schema["structure"]
    actual_counts = {
        "h1_count": sum(kind == "h1" for kind, _ in received),
        "h2_count": sum(kind == "h2" for kind, _ in received),
        "table_count": sum(kind == "table" for kind, _ in received),
    }
    if actual_counts != expected_counts:
        raise ReportError("REPORT_STRUCTURE_INVALID", "章节或表格数量不正确", {"expected": expected_counts, "actual": actual_counts})
    if len(received) != len(expected):
        raise ReportError("REPORT_STRUCTURE_INVALID", "章节与表格序列长度不正确")

    table_flags = iter(dynamic_table_flags(template))
    for index, ((expected_kind, expected_value), (actual_kind, actual_value)) in enumerate(zip(expected, received)):
        if expected_kind != actual_kind:
            raise ReportError("REPORT_STRUCTURE_INVALID", "章节与表格顺序不正确", {"position": index})
        if expected_kind in {"h1", "h2"}:
            if expected_value != actual_value:
                raise ReportError(
                    "REPORT_STRUCTURE_INVALID",
                    "章节标题不正确",
                    {"position": index, "expected": expected_value, "actual": actual_value},
                )
            continue
        compare_table(expected_value, actual_value, next(table_flags), index)


def fact_values(facts: dict[str, Any], schema: dict[str, Any]) -> Iterable[str]:
    scalars = facts.get("scalars", {})
    if isinstance(scalars, dict):
        for name, value in scalars.items():
            if name in {"case_number", "document_title"}:
                continue
            yield scalar_text(value, f"scalars.{name}")
    rows = facts.get("rows", {})
    if isinstance(rows, dict):
        for marker, values in rows.items():
            if marker not in schema["dynamic_rows"] or not isinstance(values, list):
                continue
            for index, row in enumerate(values):
                if not isinstance(row, dict):
                    continue
                for column, value in row.items():
                    if column == "index":
                        continue
                    yield scalar_text(value, f"rows.{marker}[{index}].{column}")


def numeric_literals(value: str) -> set[str]:
    """Extract material numeric literals while ignoring short clause numbers."""

    values: set[str] = set()
    for match in NUMERIC_LITERAL_PATTERN.finditer(value):
        literal = match.group(0).replace(",", "")
        if len(literal.replace(".", "")) >= 4:
            values.add(literal)
    return values


def validate_fact_evidence(facts: dict[str, Any], schema: dict[str, Any], source_corpus: str) -> dict[str, Any]:
    """Require every material numeric fact to occur in extracted material text."""

    corpus = re.sub(r"[\s,，]", "", source_corpus)
    literals = set().union(*(numeric_literals(value) for value in fact_values(facts, schema)))
    unsupported = sorted(
        literal
        for literal in literals
        if not re.search(rf"(?<!\d){re.escape(literal)}(?!\d)", corpus)
    )
    if unsupported:
        raise ReportError(
            "FACT_NUMERIC_UNSUPPORTED",
            "结构化事实含有未在材料文本或 OCR 文本中出现的数字",
            {"values": unsupported[:20], "total": len(unsupported)},
        )
    return {"status": "valid", "numeric_literal_count": len(literals)}


def validate_report(source: str, template: str, schema: dict[str, Any], facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if UNRESOLVED_PATTERN.search(source) or ROW_PATTERN.search(source):
        raise ReportError("TEMPLATE_MARKER_REMAINS", "报告仍有模板标记")
    actual = parse_fragment(source)
    template_root = parse_fragment(template)
    compare_structure(actual, template_root, template, schema)

    full_text = element_text(actual)
    template_text = element_text(template_root)
    forbidden = [
        item
        for item in schema.get("forbidden_text", [])
        if item and full_text.count(item) > template_text.count(item)
    ]
    if forbidden:
        raise ReportError("REPORT_TEXT_INVALID", "报告包含禁用占位词或错误术语", {"matches": forbidden})
    if facts is not None:
        case_number = scalar_text(facts.get("scalars", {}).get("case_number"), "scalars.case_number")
        title = next((element_text(item) for item in actual if local_name(item.tag) == "title"), "")
        if case_number and case_number not in title:
            raise ReportError("REPORT_TITLE_INVALID", "文档标题缺少案件编号", {"case_number": case_number})
        normalized_report = normalize_text(full_text)
        missing = sorted({value for value in fact_values(facts, schema) if len(normalize_text(value)) >= 2 and normalize_text(value) not in normalized_report})
        if missing:
            raise ReportError("REPORT_FACT_MISSING", "结构化事实未完整写入报告", {"values": missing[:20], "total": len(missing)})
    return {
        "status": "valid",
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "h1_count": schema["structure"]["h1_count"],
        "h2_count": schema["structure"]["h2_count"],
        "table_count": schema["structure"]["table_count"],
    }


def extract_document_content(path: Path) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("REPORT_UNAVAILABLE", f"无法读取报告：{path}", {"reason": str(exc)}) from exc
    stripped = source.lstrip()
    if not stripped.startswith("{"):
        return source
    try:
        payload = json.loads(source)
    except json.JSONDecodeError:
        return source
    candidates = [
        payload.get("content") if isinstance(payload, dict) else None,
        payload.get("data", {}).get("document", {}).get("content") if isinstance(payload, dict) else None,
        payload.get("document", {}).get("content") if isinstance(payload, dict) else None,
    ]
    content = next((item for item in candidates if isinstance(item, str) and item.strip()), None)
    if not content:
        raise ReportError("REPORT_UNAVAILABLE", "JSON 中没有文档 content")
    return content


def atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render and validate the fixed dispute report XML.")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="Render facts into the fixed XML template")
    render.add_argument("--facts", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="Validate local XML or lark-cli fetch JSON")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--facts", type=Path)

    evidence = subparsers.add_parser("validate-facts", help="Validate facts against extracted material text")
    evidence.add_argument("--facts", type=Path, required=True)
    evidence.add_argument("--source-corpus", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        template, schema = load_contract(args.template, args.schema)
        if args.command == "render":
            facts = read_json(args.facts)
            output = render_report(facts, template, schema)
            atomic_write(args.output, output)
            result = validate_report(output, template, schema, facts)
            result.update({"output": str(args.output.expanduser().resolve()), "bytes": len(output.encode("utf-8"))})
        elif args.command == "validate":
            source = extract_document_content(args.input)
            facts = read_json(args.facts) if args.facts else None
            result = validate_report(source, template, schema, facts)
            result["input"] = str(args.input.expanduser().resolve())
        else:
            facts = read_json(args.facts)
            try:
                source_corpus = args.source_corpus.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("SOURCE_CORPUS_UNAVAILABLE", "无法读取材料文本或 OCR 汇总", {"reason": str(exc)}) from exc
            result = validate_fact_evidence(facts, schema, source_corpus)
            result["facts"] = str(args.facts.expanduser().resolve())
            result["source_corpus"] = str(args.source_corpus.expanduser().resolve())
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ReportError as exc:
        print(json.dumps({"status": "invalid", "error_code": exc.code, "message": str(exc), "details": exc.details}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
