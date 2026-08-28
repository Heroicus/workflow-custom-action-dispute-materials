#!/usr/bin/env python3
"""Build, render and verify one source-backed dispute-material report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SKILL_ROOT = Path(__file__).resolve().parent.parent
REFERENCES = SKILL_ROOT / "references"
DEFAULT_TEMPLATE = REFERENCES / "report-template.xml"
DEFAULT_SCHEMA = REFERENCES / "render-schema.json"
SCALAR_PATTERN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")
ROW_PATTERN = re.compile(r"<!--([A-Za-z][A-Za-z0-9_]*_rows)-->")
UNRESOLVED_PATTERN = re.compile(r"\{\{[^{}]+\}\}")
WHITESPACE_PATTERN = re.compile(r"\s+")
NUMERIC_LITERAL_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![A-Za-z0-9])")
BASE_FIELD_KEYS = ("case_name", "case_type", "filing_date", "case_status")
BASE_FIELD_NAMES = {
    "case_name": "案件名称",
    "case_type": "案件类型",
    "filing_date": "立案（收案）日期",
    "case_status": "案件状态",
}
NON_EVIDENTIARY_ROWS = {"completeness_rows", "quality_rows"}
VISION_PACK_SCHEMA = "vision-evidence-pack/v2"
AUDIO_PACK_SCHEMA = "audio-evidence-pack/v1"
EXPECTED_RUNTIME_TYPE = "dispute-material-run/v6.7"
EXPECTED_SKILL_VERSION = "6.7.0"
EXPECTED_COMPONENT_BUILD = "6.7.0-skill-6.7.0"
EXPECTED_OPERATION = "process_target_record"
EXPECTED_APP_TOKEN = "K4nObpF5la8ertskcVccv2LknNh"
EXPECTED_TABLE_ID = "tbllz7nrxSIH8frX"
EXPECTED_BASELINE_FIELD = "材料处理基线"
EXPECTED_FIELD_CONTRACT = {
    "case_number": {"id": "fldnDqIuar", "name": "案件编号", "type": "auto_number", "access": "read_only"},
    "case_name": {"id": "fldZ1S4MD3", "name": "案件名称", "type": "text", "access": "read_write"},
    "case_type": {"id": "fldZCjfhMY", "name": "案件类型", "type": "select", "access": "read_write", "options": ["诉讼", "仲裁"]},
    "filing_date": {"id": "fld9zzBKtm", "name": "立案（收案）日期", "type": "datetime", "access": "read_write"},
    "case_status": {"id": "fldRlZJrNA", "name": "案件状态", "type": "select", "access": "read_write", "options": ["待立案", "审理中", "已结案", "已归档"]},
    "attachments": {"id": "fldOz2CYX4", "name": "案件文档", "type": "attachment", "access": "read_only"},
    "uploader": {"id": "fldpXEeboF", "name": "上传人", "type": "user", "access": "read_only"},
    "processing_status": {"id": "fldHeuCxLE", "name": "AI处理状态", "type": "select", "access": "read_write", "options": ["待处理", "分析中", "已完成", "分析失败"]},
    "analysis_result": {"id": "fldDH6CfUI", "name": "AI分析结果", "type": "text", "access": "read_write"},
    "execution_log": {"id": "fldeBcCdyM", "name": "执行日志/失败原因", "type": "text", "access": "read_write"},
    "material_baseline": {"id": "fldeOvHTNp", "name": "材料处理基线", "type": "text", "access": "read_write"},
}
EXPECTED_MODEL_CONTRACT = {
    "main_model": "Deepseek-V4-Pro",
    "vision_agent_name": "纠纷材料视觉核验员",
    "vision_model": "Doubao-Seed-2.1-turbo",
    "vision_result_schema": "vision-evidence/v2",
    "audio_transcription_service": "Feishu Minutes",
    "audio_result_schema": "audio-evidence/v1",
    "write_policy": "main_agent_only",
}
DECLARED_UNKNOWN_VALUES = {"未载明", "不适用", "待核"}
NON_EVIDENCE_NAME_PATTERN = re.compile(
    r"送达地址确认书|证据材料清单|证据目录|起诉状|仲裁申请书|答辩书|质证意见|裁决书|判决书|庭审笔录"
)
PRC_ID_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
BANK_CARD_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?:\d[ -]?){15,18}\d(?![0-9A-Za-z])")
BANK_CARD_CONTEXT_PATTERN = re.compile(r"银行卡|银行账户|银行账号|收款账户|收款账号|付款账户|付款账号|开户行|卡号")


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReportError("ARTIFACT_UNAVAILABLE", f"无法读取工件：{path}", {"reason": str(exc)}) from exc
    return digest.hexdigest()


def json_values(source: str) -> Iterator[Any]:
    """Yield JSON values from a clean response or a proxy/log wrapper."""

    decoder = json.JSONDecoder()
    stripped = source.strip()
    if not stripped:
        return
    try:
        yield json.loads(stripped)
        return
    except json.JSONDecodeError:
        pass
    cursor = 0
    while cursor < len(source):
        match = re.search(r"[\[{]", source[cursor:])
        if not match:
            return
        start = cursor + match.start()
        try:
            value, end = decoder.raw_decode(source, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        yield value
        cursor = end


def walk_values(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        for nested in json_values(value):
            yield from walk_values(nested)


def read_response_values(path: Path) -> list[Any]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("RESPONSE_UNAVAILABLE", f"无法读取响应：{path}", {"reason": str(exc)}) from exc
    values = list(json_values(source))
    if not values:
        raise ReportError("RESPONSE_INVALID", f"响应中没有可解析 JSON：{path}")
    for root in values:
        for item in walk_values(root):
            if isinstance(item, dict) and item.get("ok") is False:
                raise ReportError("REMOTE_OPERATION_FAILED", "远程操作返回失败", {"error": item.get("error", item)})
    return values


def scalar_text(value: Any, path: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    raise ReportError("FACT_VALUE_INVALID", f"{path} 必须是字符串、数字、布尔值或 null")


def scalarish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        return scalarish(value[0]) if value else ""
    if isinstance(value, dict):
        for key in ("text", "value", "name", "url", "link", "id"):
            result = scalarish(value.get(key))
            if result:
                return result
    return ""


def is_declared_unknown(value: Any) -> bool:
    """Return whether a fact explicitly carries one of the domain unknown states."""

    return scalarish(value) in DECLARED_UNKNOWN_VALUES


def escaped(value: Any, path: str) -> str:
    text = scalar_text(value, path)
    return html.escape(text, quote=False).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")


def document_table_widths(root: ET.Element) -> list[int]:
    widths: list[int] = []
    for table in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1].lower() == "table"):
        colgroup = next((item for item in table if item.tag.rsplit("}", 1)[-1].lower() == "colgroup"), None)
        if colgroup is None:
            raise ReportError("REPORT_LAYOUT_INVALID", "表格缺少 colgroup 列宽定义")
        try:
            width = sum(
                int(item.attrib["width"]) * int(item.attrib.get("span", "1"))
                for item in colgroup if item.tag.rsplit("}", 1)[-1].lower() == "col"
            )
        except (KeyError, ValueError) as exc:
            raise ReportError("REPORT_LAYOUT_INVALID", "表格列宽定义不合法") from exc
        widths.append(width)
    return widths


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
            "SCHEMA_INVALID", "模板行标记与渲染结构不一致",
            {"template_only": sorted(template_rows - schema_rows), "schema_only": sorted(schema_rows - template_rows)},
        )
    try:
        root = ET.fromstring(f"<report-root>{template}</report-root>")
    except ET.ParseError as exc:
        raise ReportError("TEMPLATE_INVALID", "固定模板不是有效 XML", {"reason": str(exc)}) from exc
    expected_width = schema.get("table_width")
    try:
        table_widths = document_table_widths(root)
    except ReportError as exc:
        raise ReportError("TEMPLATE_INVALID", "固定模板列宽不合法", exc.details) from exc
    if not isinstance(expected_width, int) or set(table_widths) != {expected_width}:
        raise ReportError("TEMPLATE_INVALID", "固定模板所有表格必须使用同一总宽", {"widths": sorted(set(table_widths))})
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


def validate_fact_shape(facts: dict[str, Any], template: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    scalars = facts.get("scalars")
    rows = facts.get("rows")
    base_fields = facts.get("base_fields")
    if not isinstance(scalars, dict) or not isinstance(rows, dict) or not isinstance(base_fields, dict):
        raise ReportError("FACTS_INVALID", "facts 必须包含 scalars、rows 和 base_fields 对象")
    scalar_markers = set(SCALAR_PATTERN.findall(template))
    row_markers = set(schema["dynamic_rows"])
    problems = {
        "missing_scalars": sorted(scalar_markers - set(scalars)),
        "unknown_scalars": sorted(set(scalars) - scalar_markers),
        "missing_rows": sorted(row_markers - set(rows)),
        "unknown_rows": sorted(set(rows) - row_markers),
        "missing_base_fields": sorted(set(BASE_FIELD_KEYS) - set(base_fields)),
        "unknown_base_fields": sorted(set(base_fields) - set(BASE_FIELD_KEYS)),
    }
    if any(problems.values()):
        raise ReportError("FACT_CONTRACT_INCOMPLETE", "case-facts.json 不是完整脚手架", problems)
    for marker, columns in schema["dynamic_rows"].items():
        values = rows[marker]
        if not isinstance(values, list):
            raise ReportError("FACT_ROW_INVALID", f"rows.{marker} 必须是数组")
        for index, row in enumerate(values):
            if not isinstance(row, dict):
                raise ReportError("FACT_ROW_INVALID", f"rows.{marker}[{index}] 必须是对象")
            unknown = sorted(set(row) - set(columns))
            if unknown:
                raise ReportError("FACT_ROW_INVALID", f"rows.{marker}[{index}] 包含未知字段", {"unknown": unknown})
    for name in schema.get("required_scalars", []):
        if not scalar_text(scalars.get(name), f"scalars.{name}"):
            raise ReportError("REQUIRED_FACT_MISSING", f"缺少必填字段 scalars.{name}")
    return scalars, rows, base_fields


def render_report(facts: dict[str, Any], template: str, schema: dict[str, Any]) -> str:
    scalars, rows, _ = validate_fact_shape(facts, template, schema)
    case_number = scalar_text(scalars.get("case_number"), "scalars.case_number")
    prepared_scalars = dict(scalars)
    prepared_scalars["opponent_id"] = mask_identifier(scalar_text(prepared_scalars.get("opponent_id"), "scalars.opponent_id"))
    prepared_scalars["opponent_contact"] = mask_contact(scalar_text(prepared_scalars.get("opponent_contact"), "scalars.opponent_contact"))
    prepared_scalars["our_contact"] = mask_contact(scalar_text(prepared_scalars.get("our_contact"), "scalars.our_contact"))
    if not scalar_text(prepared_scalars.get("document_title"), "scalars.document_title"):
        prepared_scalars["document_title"] = f"{case_number} 诉讼/仲裁案件材料梳理报告"
    output = template
    for marker in sorted(SCALAR_PATTERN.findall(template)):
        output = output.replace(f"{{{{{marker}}}}}", escaped(prepared_scalars.get(marker), f"scalars.{marker}"))
    for marker, columns in schema["dynamic_rows"].items():
        replacement = "".join(render_row(marker, columns, row, index) for index, row in enumerate(rows[marker]))
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


def source_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", html.unescape(value or "")).lower()


def source_literal_supported(value: str, corpus_key: str) -> bool:
    normalized = source_key(value)
    if not normalized:
        return True
    if normalized in corpus_key:
        return True
    parts = [source_key(item) for item in re.split(r"[；;、|/\n]+", value) if source_key(item)]
    return len(parts) > 1 and all(item in corpus_key for item in parts)


def element_text(element: ET.Element) -> str:
    return normalize_text("".join(element.itertext()))


def table_rows(table: ET.Element) -> list[list[str]]:
    return [
        [element_text(cell) for cell in list(row) if local_name(cell.tag) in {"td", "th"}]
        for row in table.iter() if local_name(row.tag) == "tr"
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


def compare_table(expected_rows: list[list[str]], actual_rows: list[list[str]], allows_dynamic_rows: bool, position: int) -> None:
    if allows_dynamic_rows:
        if len(actual_rows) < len(expected_rows):
            raise ReportError("REPORT_STRUCTURE_INVALID", "动态表格缺少固定行", {"position": position})
    elif len(expected_rows) != len(actual_rows):
        raise ReportError(
            "REPORT_STRUCTURE_INVALID", "固定表格行数不正确",
            {"position": position, "expected": len(expected_rows), "actual": len(actual_rows)},
        )
    for row_index, (expected_cells, actual_cells) in enumerate(zip(expected_rows, actual_rows)):
        if len(expected_cells) != len(actual_cells):
            raise ReportError("REPORT_STRUCTURE_INVALID", "表格列数不正确", {"position": position, "row": row_index})
        for cell_index, (template_cell, actual_cell) in enumerate(zip(expected_cells, actual_cells)):
            static_text, has_marker = template_cell_pattern(template_cell)
            if has_marker:
                if static_text and static_text not in actual_cell:
                    raise ReportError("REPORT_STRUCTURE_INVALID", "表格固定标签不正确", {"position": position, "row": row_index, "cell": cell_index})
            elif template_cell != actual_cell:
                raise ReportError(
                    "REPORT_STRUCTURE_INVALID", "表格固定内容不正确",
                    {"position": position, "row": row_index, "cell": cell_index, "expected": template_cell, "actual": actual_cell},
                )


def compare_structure(actual: ET.Element, template_root: ET.Element, template: str, schema: dict[str, Any]) -> None:
    expected = structure_signature(template_root)
    received = structure_signature(actual)
    actual_counts = {
        "h1_count": sum(kind == "h1" for kind, _ in received),
        "h2_count": sum(kind == "h2" for kind, _ in received),
        "table_count": sum(kind == "table" for kind, _ in received),
    }
    if actual_counts != schema["structure"]:
        raise ReportError("REPORT_STRUCTURE_INVALID", "章节或表格数量不正确", {"expected": schema["structure"], "actual": actual_counts})
    if len(received) != len(expected):
        raise ReportError("REPORT_STRUCTURE_INVALID", "章节与表格序列长度不正确")
    table_flags = iter(dynamic_table_flags(template))
    for index, ((expected_kind, expected_value), (actual_kind, actual_value)) in enumerate(zip(expected, received)):
        if expected_kind != actual_kind:
            raise ReportError("REPORT_STRUCTURE_INVALID", "章节与表格顺序不正确", {"position": index})
        if expected_kind in {"h1", "h2"}:
            if expected_value != actual_value:
                raise ReportError("REPORT_STRUCTURE_INVALID", "章节标题不正确", {"position": index, "expected": expected_value, "actual": actual_value})
        else:
            compare_table(expected_value, actual_value, next(table_flags), index)


def fact_items(
    facts: dict[str, Any], schema: dict[str, Any], evidentiary_only: bool = False,
) -> Iterable[tuple[str, str]]:
    scalars = facts.get("scalars", {})
    if isinstance(scalars, dict):
        for name, value in scalars.items():
            if name not in {"case_number", "document_title", "opponent_id", "opponent_contact", "our_contact"}:
                yield f"scalars.{name}", scalar_text(value, f"scalars.{name}")
    rows = facts.get("rows", {})
    if not isinstance(rows, dict):
        return
    for marker, values in rows.items():
        if marker not in schema["dynamic_rows"] or not isinstance(values, list):
            continue
        if evidentiary_only and marker in NON_EVIDENTIARY_ROWS:
            continue
        for index, row in enumerate(values):
            if not isinstance(row, dict):
                continue
            for column, value in row.items():
                if column != "index":
                    path = f"rows.{marker}[{index}].{column}"
                    yield path, scalar_text(value, path)


def fact_values(facts: dict[str, Any], schema: dict[str, Any], evidentiary_only: bool = False) -> Iterable[str]:
    for _, value in fact_items(facts, schema, evidentiary_only):
        yield value


def all_visible_fact_items(facts: dict[str, Any], schema: dict[str, Any]) -> Iterable[tuple[str, str]]:
    scalars = facts.get("scalars", {})
    if isinstance(scalars, dict):
        for name, value in scalars.items():
            if name != "document_title":
                path = f"scalars.{name}"
                yield path, scalar_text(value, path)
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
                    path = f"rows.{marker}[{index}].{column}"
                    yield path, scalar_text(value, path)


def numeric_literals(value: str) -> set[str]:
    values: set[str] = set()
    for match in NUMERIC_LITERAL_PATTERN.finditer(value):
        literal = match.group(0).replace(",", "")
        if len(literal.replace(".", "")) >= 4:
            values.add(literal)
    return values


def canonical_numeric_literals(value: str) -> set[str]:
    return {item.rstrip("0").rstrip(".") if "." in item else item for item in numeric_literals(value)}


def manifest_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ReportError("MATERIAL_MANIFEST_INVALID", "材料清单没有附件项")
    leaves: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if not isinstance(item, dict):
            raise ReportError("MATERIAL_MANIFEST_INVALID", "材料清单项必须是对象")
        children = item.get("children")
        if isinstance(children, list) and children:
            for child in children:
                visit(child)
        elif item.get("status") != "ignored":
            leaves.append(item)

    for item in items:
        visit(item)
    if not leaves:
        raise ReportError("MATERIAL_MANIFEST_INVALID", "材料清单没有可处理内容")
    return leaves


def mask_identifier(value: str) -> str:
    return PRC_ID_PATTERN.sub(lambda match: match.group(0)[:6] + "********" + match.group(0)[-4:], value)


def mask_contact(value: str) -> str:
    return MOBILE_PATTERN.sub(lambda match: match.group(0)[:3] + "****" + match.group(0)[-4:], value)


def luhn_valid(value: str) -> bool:
    digits = [int(item) for item in re.sub(r"\D", "", value)]
    if not 16 <= len(digits) <= 19:
        return False
    parity = len(digits) % 2
    total = 0
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def contains_bank_card(value: str) -> bool:
    for match in BANK_CARD_PATTERN.finditer(value):
        if PRC_ID_PATTERN.fullmatch(match.group(0).replace(" ", "").replace("-", "")):
            continue
        context = value[max(0, match.start() - 12):min(len(value), match.end() + 12)]
        if BANK_CARD_CONTEXT_PATTERN.search(context) or luhn_valid(match.group(0)):
            return True
    return False


def build_scaffold(case_number: str, manifest: dict[str, Any], template: str, schema: dict[str, Any]) -> dict[str, Any]:
    entries = manifest_entries(manifest)
    scalars = {name: "" for name in sorted(set(SCALAR_PATTERN.findall(template)))}
    scalars["case_number"] = case_number
    rows: dict[str, list[dict[str, str]]] = {name: [] for name in schema["dynamic_rows"]}
    for index, item in enumerate(entries, 1):
        name = scalarish(item.get("file_name"))
        status = scalarish(item.get("status")) or "failed"
        availability = {"complete": "是", "partial": "部分", "failed": "否"}.get(status, "否")
        note = {"complete": "解析完成", "partial": "仅部分内容可读取", "failed": "该文件无法解析"}.get(status, "该文件无法解析")
        rows["completeness_rows"].append({
            "index": str(index), "source_check_item": name, "available": availability, "source_note": note,
        })
    counts = Counter(scalarish(item.get("status")) or "failed" for item in entries)
    rows["quality_rows"] = [{
        "index": "1", "check": "材料读取情况",
        "result": f"共{len(entries)}项，完整{counts['complete']}项，部分{counts['partial']}项，无法解析{counts['failed']}项",
    }] + [
        {"index": str(index), "check": check, "result": ""}
        for index, check in enumerate(schema.get("required_quality_checks", [])[1:], 2)
    ]
    return {"scalars": scalars, "rows": rows, "base_fields": {key: "" for key in BASE_FIELD_KEYS}}


def validate_manifest_coverage(rows: dict[str, Any], manifest: dict[str, Any]) -> Counter[str]:
    entries = manifest_entries(manifest)
    expected = [scalarish(item.get("file_name")) for item in entries]
    evidence = [scalarish(row.get("name")) for row in rows["evidence_rows"]]
    completeness = [scalarish(row.get("source_check_item")) for row in rows["completeness_rows"]]
    unknown_evidence = sorted((Counter(evidence) - Counter(expected)).elements())
    if unknown_evidence or Counter(completeness) != Counter(expected):
        raise ReportError(
            "MATERIAL_COVERAGE_INCOMPLETE", "证据清单引用了未知材料，或材料完整性表未覆盖全部材料",
            {
                "expected": len(expected), "evidence_rows": len(evidence), "completeness_rows": len(completeness),
                "unknown_evidence": unknown_evidence[:10],
                "missing_completeness": sorted((Counter(expected) - Counter(completeness)).elements())[:10],
            },
        )
    if not rows["quality_rows"]:
        raise ReportError("MATERIAL_COVERAGE_INCOMPLETE", "AI 输出质量自检表缺少材料读取情况")
    return Counter(scalarish(item.get("status")) or "failed" for item in entries)


def validate_semantic_completion(
    scalars: dict[str, Any], rows: dict[str, Any], schema: dict[str, Any], corpus_key: str,
) -> None:
    missing = [
        name for name in schema.get("semantic_required_scalars", [])
        if not scalar_text(scalars.get(name), f"scalars.{name}")
    ]
    if missing:
        raise ReportError(
            "SEMANTIC_FACT_MISSING", "核心事实字段不得留空；无唯一值时使用未载明、不适用或待核",
            {"fields": missing},
        )

    evidence_violations = [
        scalarish(row.get("name")) for row in rows.get("evidence_rows", [])
        if NON_EVIDENCE_NAME_PATTERN.search(scalarish(row.get("name")))
    ]
    if evidence_violations:
        raise ReportError(
            "MATERIAL_EVIDENCE_CONFLATED", "程序文书、当事人提交材料或工作清单不得列入证据总表",
            {"names": evidence_violations[:20]},
        )

    has_civil_complaint = source_key("民事起诉状") in corpus_key
    has_court_service = source_key("人民法院送达地址确认书") in corpus_key
    has_award = source_key("裁决书") in corpus_key
    case_type = scalar_text(scalars.get("case_type"), "scalars.case_type")
    if has_civil_complaint and has_court_service and case_type != "诉讼":
        raise ReportError(
            "CURRENT_PROCEEDING_MISCLASSIFIED", "当前材料同时存在民事起诉状和法院送达材料，当前程序不得归类为仲裁",
            {"actual": case_type, "expected": "诉讼"},
        )
    if has_civil_complaint and has_award:
        if not scalar_text(scalars.get("related_case"), "scalars.related_case"):
            raise ReportError("PROCEEDING_CHAIN_MISSING", "当前诉讼与前置仲裁未分层登记，缺少关联案件")
        if not rows.get("procedure_rows") or not rows.get("conflict_rows"):
            raise ReportError("PROCEEDING_CHAIN_MISSING", "存在前置仲裁和当前诉讼时，程序节点与阶段冲突登记不能为空")

    request_text = " ".join(
        scalar_text(value, f"rows.request_rows[{index}].{key}")
        for index, row in enumerate(rows.get("request_rows", [])) if isinstance(row, dict)
        for key, value in row.items() if key != "index"
    )
    request_numbers = canonical_numeric_literals(request_text)
    for name in schema.get("request_amount_scalars", []):
        value = scalar_text(scalars.get(name), f"scalars.{name}")
        unsupported = canonical_numeric_literals(value) - request_numbers
        if value and unsupported:
            raise ReportError(
                "CALCULATION_NOT_REQUEST_BACKED", "金额汇总不得把已支付款或证据金额误写为诉讼请求金额",
                {"field": name, "values": sorted(unsupported)},
            )

    calculation_rows = rows.get("calculation_detail_rows", [])
    has_calculation_request = any(
        re.search(r"利息|违约金|资金占用|逾期", " ".join(
            scalarish(value) for key, value in row.items() if key != "index"
        ))
        for row in rows.get("request_rows", []) if isinstance(row, dict)
    )
    if calculation_rows and not has_calculation_request:
        raise ReportError(
            "CALCULATION_SECTION_MISUSED",
            "没有利息、违约金或资金占用请求时，7.2 分段计算明细必须为空",
        )
    incomplete_calculations: list[int] = []
    for index, row in enumerate(calculation_rows):
        if not isinstance(row, dict):
            continue
        if scalarish(row.get("amount")) and (
            not scalarish(row.get("period"))
            or not scalarish(row.get("base"))
            or not scalarish(row.get("daily_rate"))
            or not scalarish(row.get("days"))
        ):
            incomplete_calculations.append(index)
    if incomplete_calculations:
        raise ReportError(
            "CALCULATION_DETAIL_INCOMPLETE",
            "7.2 分段计算明细填写金额时必须同时有材料支持的期间、基数、日利率和天数",
            {"rows": incomplete_calculations[:20]},
        )

    truncated: list[str] = []
    for index, row in enumerate(rows.get("our_argument_rows", [])):
        if not isinstance(row, dict):
            continue
        focus = scalarish(row.get("focus"))
        record = scalarish(row.get("our_record"))
        if focus and record.startswith(focus) and len(record) >= len(focus) + 4:
            truncated.append(f"rows.our_argument_rows[{index}].focus")
    for marker, values in rows.items():
        if not isinstance(values, list):
            continue
        for index, row in enumerate(values):
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                text = scalarish(value)
                if re.search(r"至\s*\d{4}(?:年\d{1,2}月?)?$", text):
                    truncated.append(f"rows.{marker}[{index}].{key}")
    if truncated:
        raise ReportError("FACT_TEXT_TRUNCATED", "结构化事实存在明显截断文本", {"fields": sorted(set(truncated))[:20]})

    checks = {
        scalarish(row.get("check")): scalarish(row.get("result"))
        for row in rows.get("quality_rows", []) if isinstance(row, dict)
    }
    missing_checks = [
        item for item in schema.get("required_quality_checks", [])
        if item not in checks or not checks[item]
    ]
    if missing_checks:
        raise ReportError("QUALITY_GATE_INCOMPLETE", "AI输出质量自检缺少必检项", {"checks": missing_checks})


def validate_vision_evidence(
    evidence: dict[str, Any], source_corpus: str | None = None,
    vision_tasks: dict[str, Any] | None = None, vision_tasks_sha256: str | None = None,
) -> dict[str, Any]:
    if evidence.get("schema_version") != VISION_PACK_SCHEMA:
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包版本不正确")
    policy = evidence.get("policy")
    summary = evidence.get("summary")
    tasks = evidence.get("tasks")
    unresolved = evidence.get("unresolved")
    artifacts = evidence.get("artifacts")
    if not isinstance(policy, dict) or not isinstance(summary, dict) or not isinstance(tasks, list) or not isinstance(unresolved, list) or not isinstance(artifacts, dict):
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包结构不完整")
    if policy != {
        "main_writer": "Deepseek-V4-Pro",
        "vision_worker": "Doubao-Seed-2.1-turbo",
        "single_writer": True,
        "vision_worker_write_scope": "read_only_evidence",
    }:
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包不符合主智能体单写入契约")
    expected = summary.get("expected")
    received = summary.get("received")
    failed = summary.get("failed")
    unresolved_count = summary.get("unresolved")
    if not all(isinstance(value, int) and value >= 0 for value in (expected, received, failed, unresolved_count)):
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包统计值不合法")
    complete = summary.get("complete")
    partial = summary.get("partial")
    if not all(isinstance(value, int) and value >= 0 for value in (complete, partial)):
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包完成统计值不合法")
    if expected != received or received != len(tasks) or complete + partial + failed != received or failed or unresolved_count or unresolved:
        raise ReportError(
            "VISION_EVIDENCE_INCOMPLETE", "视觉子智能体结果未全部核清",
            {"expected": expected, "received": received, "failed": failed, "unresolved": unresolved_count},
        )
    task_map: dict[str, tuple[str, str]] = {}
    for item in tasks:
        if not isinstance(item, dict):
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务必须是对象")
        task_id = scalarish(item.get("task_id"))
        source_hash = scalarish(item.get("source_sha256"))
        image_hash = scalarish(item.get("image_sha256"))
        producer = item.get("producer")
        if (
            not re.fullmatch(r"vis_[0-9a-f]{20}", task_id)
            or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", image_hash)
            or item.get("status") != "complete"
            or not isinstance(producer, dict)
            or producer.get("agent_name") != "纠纷材料视觉核验员"
            or producer.get("model") != "Doubao-Seed-2.1-turbo"
        ):
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务身份、哈希或状态不正确", {"task_id": task_id})
        if task_id in task_map:
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务 ID 重复", {"task_id": task_id})
        verbatim_text = item.get("verbatim_text")
        regions = item.get("uncertain_regions")
        if (
            not isinstance(verbatim_text, str)
            or not verbatim_text.strip()
            or not isinstance(regions, list)
            or any(not isinstance(region, dict) or region.get("critical") is True for region in regions)
        ):
            raise ReportError("VISION_EVIDENCE_INCOMPLETE", "视觉证据任务仍有未决关键字段", {"task_id": task_id})
        task_map[task_id] = (source_hash, image_hash)
    required_artifacts = {"vision_tasks_sha256", "source_corpus_sha256", "verified_corpus_sha256"}
    if set(artifacts) != required_artifacts or any(
        value is not None and not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in artifacts.values()
    ):
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包工件哈希不正确")
    if vision_tasks is not None:
        raw_expected_tasks = vision_tasks.get("tasks")
        if vision_tasks.get("schema_version") != "vision-task/v1" or not isinstance(raw_expected_tasks, list):
            raise ReportError("VISION_TASKS_INVALID", "视觉任务清单版本或结构不正确")
        expected_map: dict[str, tuple[str, str]] = {}
        for item in raw_expected_tasks:
            if not isinstance(item, dict):
                raise ReportError("VISION_TASKS_INVALID", "视觉任务必须是对象")
            task_id = scalarish(item.get("task_id"))
            if not task_id or task_id in expected_map:
                raise ReportError("VISION_TASKS_INVALID", "视觉任务 ID 缺失或重复", {"task_id": task_id})
            expected_map[task_id] = (scalarish(item.get("source_sha256")), scalarish(item.get("image_sha256")))
        if expected_map != task_map or expected != len(expected_map):
            raise ReportError("VISION_EVIDENCE_MISMATCH", "视觉证据与本次任务清单不一致")
    if vision_tasks_sha256 is not None and artifacts.get("vision_tasks_sha256") != vision_tasks_sha256:
        raise ReportError("VISION_EVIDENCE_MISMATCH", "视觉任务清单文件哈希与证据包不一致")
    if source_corpus is not None:
        if artifacts.get("verified_corpus_sha256") != hashlib.sha256(source_corpus.encode("utf-8")).hexdigest():
            raise ReportError("VISION_CORPUS_MISMATCH", "最终材料语料哈希与视觉证据包不一致")
        corpus_key = source_key(source_corpus)
        missing: list[str] = []
        for item in tasks:
            text = scalarish(item.get("verbatim_text"))
            task_id = scalarish(item.get("task_id"))
            if text and source_key(text) not in corpus_key:
                missing.append(task_id)
        if missing:
            raise ReportError("VISION_CORPUS_MISMATCH", "视觉逐字转录未进入最终材料语料", {"task_ids": missing[:20]})
    return {"vision_expected": expected, "vision_received": received}


def response_keyed_texts(value: Any, key: str) -> set[str]:
    return {
        scalarish(item.get(key)) for item in walk_values(value)
        if isinstance(item, dict) and scalarish(item.get(key))
    }


def saved_audio_response(path: Path, expected_hash: str, label: str) -> dict[str, Any]:
    if not path.is_file() or sha256_file(path) != expected_hash:
        raise ReportError("AUDIO_REMOTE_READBACK_CHANGED", f"{label}响应不存在或哈希不一致")
    roots = read_response_values(path)
    response = next((item for item in reversed(roots) if isinstance(item, dict)), None)
    if not isinstance(response, dict) or response.get("ok") is not True or response.get("identity") != "user":
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", f"{label}响应不是用户身份成功结果")
    return response


def audio_path_from_response(value: str, cwd: Path, allowed_root: Path) -> Path | None:
    candidate = Path(value).expanduser()
    candidate = (cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        candidate.relative_to(allowed_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def validate_audio_remote_artifacts(
    item: dict[str, Any], receipts_dir: Path, transcripts_dir: Path, transcript: Path,
) -> None:
    task_id = scalarish(item.get("task_id"))
    file_token = scalarish(item.get("file_token"))
    minute_token = scalarish(item.get("minute_token"))
    minute_url = scalarish(item.get("minute_url"))
    remote = item.get("remote_readback")
    required_hashes = {
        "drive_upload_response_sha256", "minute_upload_response_sha256", "minute_detail_response_sha256",
    }
    if not isinstance(remote, dict) or set(remote) != required_hashes or any(
        not re.fullmatch(r"[0-9a-f]{64}", scalarish(remote.get(key))) for key in required_hashes
    ):
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据缺少完整远端响应哈希", {"task_id": task_id})

    raw_dir = receipts_dir.resolve() / "raw"
    drive = saved_audio_response(
        raw_dir / f"{task_id}.drive.json", scalarish(remote["drive_upload_response_sha256"]), "云空间上传",
    )
    if response_keyed_texts(drive, "file_token") != {file_token}:
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "云空间响应与音频回执 file_token 不一致", {"task_id": task_id})

    minute = saved_audio_response(
        raw_dir / f"{task_id}.minute.json", scalarish(remote["minute_upload_response_sha256"]), "妙记生成",
    )
    if response_keyed_texts(minute, "minute_token") != {minute_token}:
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "妙记生成响应与音频回执 token 不一致", {"task_id": task_id})
    response_urls = response_keyed_texts(minute, "minute_url") or response_keyed_texts(minute, "url")
    if (response_urls and response_urls != {minute_url}) or (not response_urls and minute_url):
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "妙记生成响应 URL 与音频回执不一致", {"task_id": task_id})

    detail = saved_audio_response(
        raw_dir / f"{task_id}.detail.json", scalarish(remote["minute_detail_response_sha256"]), "妙记逐字稿读回",
    )
    minute_items = [
        value for value in walk_values(detail)
        if isinstance(value, dict) and scalarish(value.get("minute_token"))
    ]
    if {scalarish(value.get("minute_token")) for value in minute_items} != {minute_token}:
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "逐字稿响应未唯一绑定目标妙记", {"task_id": task_id})
    target_items = [value for value in minute_items if scalarish(value.get("minute_token")) == minute_token]
    if any(value.get("error") not in (None, "", False, {}, []) for value in target_items):
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "目标妙记的逐字稿响应仍包含错误", {"task_id": task_id})
    cwd = transcripts_dir.resolve().parent
    remote_paths = {
        path.resolve()
        for target in target_items
        for value in walk_values(target)
        if isinstance(value, dict) and isinstance(value.get("transcript_file"), str)
        for path in [audio_path_from_response(value["transcript_file"], cwd, transcripts_dir)]
        if path is not None
    }
    if remote_paths != {transcript.resolve()}:
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "逐字稿远端路径与音频回执不一致", {"task_id": task_id})


def validate_audio_evidence(
    evidence: dict[str, Any], source_corpus: str | None = None,
    input_corpus: str | None = None, audio_tasks: dict[str, Any] | None = None,
    audio_tasks_sha256: str | None = None, receipts_dir: Path | None = None,
    transcripts_dir: Path | None = None,
) -> dict[str, Any]:
    if evidence.get("schema_version") != AUDIO_PACK_SCHEMA:
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据包版本不正确")
    policy = evidence.get("policy")
    summary = evidence.get("summary")
    tasks = evidence.get("tasks")
    unresolved = evidence.get("unresolved")
    artifacts = evidence.get("artifacts")
    if (
        policy != {
            "provider": "Feishu Minutes",
            "identity": "user",
            "remote_transcript_readback": True,
            "main_writer": "Deepseek-V4-Pro",
            "single_writer": True,
        }
        or not isinstance(summary, dict)
        or not isinstance(tasks, list)
        or not isinstance(unresolved, list)
        or not isinstance(artifacts, dict)
    ):
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据包结构或单写入策略不正确")
    expected = summary.get("expected")
    received = summary.get("received")
    complete = summary.get("complete")
    failed = summary.get("failed")
    reused = summary.get("reused")
    if not all(type(value) is int and value >= 0 for value in (expected, received, complete, failed, reused)):
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据包统计值不合法")
    if expected != received or received != complete or received != len(tasks) or failed or unresolved:
        raise ReportError(
            "AUDIO_EVIDENCE_INCOMPLETE", "音频逐字稿未全部完成远端读回",
            {"expected": expected, "received": received, "complete": complete, "failed": failed},
        )
    task_map: dict[str, tuple[str, str]] = {}
    transcript_missing: list[str] = []
    corpus_key = source_key(source_corpus or "")
    if tasks and (receipts_dir is None or transcripts_dir is None):
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据校验缺少回执或逐字稿根目录")
    for item in tasks:
        if not isinstance(item, dict):
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据任务必须是对象")
        task_id = scalarish(item.get("task_id"))
        source_hash = scalarish(item.get("source_sha256"))
        media_hash = scalarish(item.get("media_sha256"))
        file_token = scalarish(item.get("file_token"))
        minute_token = scalarish(item.get("minute_token"))
        minute_url = scalarish(item.get("minute_url"))
        transcript_hash = scalarish(item.get("transcript_sha256"))
        provider = item.get("provider")
        if (
            not re.fullmatch(r"aud_[0-9a-f]{20}", task_id)
            or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", media_hash)
            or not re.fullmatch(r"[A-Za-z0-9_-]{4,256}", file_token)
            or not re.fullmatch(r"[a-z0-9]{8,128}", minute_token)
            or (minute_url and not re.fullmatch(rf"https://[^\s]+/minutes/{re.escape(minute_token)}(?:[/?#].*)?", minute_url))
            or not re.fullmatch(r"[0-9a-f]{64}", transcript_hash)
            or item.get("status") != "complete"
            or provider != {"service": "Feishu Minutes", "identity": "user", "mode": "remote_transcript_readback"}
        ):
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据身份、哈希、妙记或状态不正确", {"task_id": task_id})
        if task_id in task_map:
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据任务 ID 重复", {"task_id": task_id})
        transcript = Path(scalarish(item.get("transcript_path"))).resolve()
        try:
            transcript.relative_to(transcripts_dir.resolve() if transcripts_dir is not None else Path("/__invalid__"))
        except ValueError as exc:
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频逐字稿路径不在本次输出目录", {"task_id": task_id}) from exc
        if not transcript.is_file() or sha256_file(transcript) != transcript_hash:
            raise ReportError("AUDIO_TRANSCRIPT_CHANGED", "音频逐字稿不存在或哈希不一致", {"task_id": task_id})
        transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
        if source_corpus is not None and source_key(transcript_text) not in corpus_key:
            transcript_missing.append(task_id)
        validate_audio_remote_artifacts(item, receipts_dir, transcripts_dir, transcript)  # type: ignore[arg-type]
        task_map[task_id] = (source_hash, media_hash)
    if transcript_missing:
        raise ReportError("AUDIO_CORPUS_MISMATCH", "音频逐字稿未进入最终材料语料", {"task_ids": transcript_missing[:20]})
    required_artifacts = {"audio_tasks_sha256", "input_corpus_sha256", "verified_corpus_sha256"}
    if set(artifacts) != required_artifacts or any(
        value is not None and not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in artifacts.values()
    ):
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据包工件哈希不正确")
    if input_corpus is not None and artifacts.get("input_corpus_sha256") != hashlib.sha256(input_corpus.encode("utf-8")).hexdigest():
        raise ReportError("AUDIO_CORPUS_MISMATCH", "音频证据输入语料不是本次视觉核验语料")
    if source_corpus is not None and artifacts.get("verified_corpus_sha256") != hashlib.sha256(source_corpus.encode("utf-8")).hexdigest():
        raise ReportError("AUDIO_CORPUS_MISMATCH", "最终材料语料哈希与音频证据包不一致")
    if audio_tasks is not None:
        raw_expected_tasks = audio_tasks.get("tasks")
        if audio_tasks.get("schema_version") != "audio-task/v1" or not isinstance(raw_expected_tasks, list):
            raise ReportError("AUDIO_TASKS_INVALID", "音频任务清单版本或结构不正确")
        expected_map: dict[str, tuple[str, str]] = {}
        for item in raw_expected_tasks:
            if not isinstance(item, dict):
                raise ReportError("AUDIO_TASKS_INVALID", "音频任务必须是对象")
            task_id = scalarish(item.get("task_id"))
            if not task_id or task_id in expected_map:
                raise ReportError("AUDIO_TASKS_INVALID", "音频任务 ID 缺失或重复", {"task_id": task_id})
            expected_map[task_id] = (scalarish(item.get("source_sha256")), scalarish(item.get("media_sha256")))
        if expected_map != task_map or expected != len(expected_map):
            raise ReportError("AUDIO_EVIDENCE_MISMATCH", "音频证据与本次任务清单不一致")
    if audio_tasks_sha256 is not None and artifacts.get("audio_tasks_sha256") != audio_tasks_sha256:
        raise ReportError("AUDIO_EVIDENCE_MISMATCH", "音频任务清单文件哈希与证据包不一致")
    return {"audio_expected": expected, "audio_received": received, "audio_reused": reused}


def audio_minutes_baseline(evidence: dict[str, Any]) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    tasks = evidence.get("tasks")
    if not isinstance(tasks, list):
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据包缺少任务")
    for item in tasks:
        if not isinstance(item, dict):
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据任务必须是对象")
        media_hash = scalarish(item.get("media_sha256"))
        current = {
            "file_token": scalarish(item.get("file_token")),
            "minute_token": scalarish(item.get("minute_token")),
            "minute_url": scalarish(item.get("minute_url")),
            "transcript_sha256": scalarish(item.get("transcript_sha256")),
        }
        existing = values.get(media_hash)
        if existing is not None and existing != current:
            raise ReportError("AUDIO_EVIDENCE_INVALID", "同一音频哈希对应了冲突的妙记结果", {"media_sha256": media_hash})
        values[media_hash] = current
    return values


def validate_fact_evidence(
    facts: dict[str, Any], template: str, schema: dict[str, Any], source_corpus: str,
    vision_corpus: str, manifest: dict[str, Any],
    vision_evidence: dict[str, Any], vision_tasks: dict[str, Any], vision_tasks_sha256: str,
    audio_evidence: dict[str, Any], audio_tasks: dict[str, Any], audio_tasks_sha256: str,
    audio_receipts_dir: Path, audio_transcripts_dir: Path,
) -> dict[str, Any]:
    scalars, rows, base_fields = validate_fact_shape(facts, template, schema)
    counts = validate_manifest_coverage(rows, manifest)
    if counts["partial"] or counts["failed"]:
        raise ReportError(
            "MATERIAL_EXTRACTION_INCOMPLETE", "存在未完整解析的附件，不得生成完成报告",
            {"partial": counts["partial"], "failed": counts["failed"]},
        )
    artifacts = manifest.get("artifacts")
    if manifest.get("schema_version") != "2.0" or not isinstance(artifacts, dict):
        raise ReportError("MATERIAL_MANIFEST_INVALID", "材料清单缺少工件哈希与媒体任务绑定")
    manifest_vision_ids = artifacts.get("vision_task_ids")
    manifest_audio_ids = artifacts.get("audio_task_ids")
    actual_vision_ids = sorted(
        scalarish(item.get("task_id")) for item in vision_tasks.get("tasks", []) if isinstance(item, dict)
    )
    actual_audio_ids = sorted(
        scalarish(item.get("task_id")) for item in audio_tasks.get("tasks", []) if isinstance(item, dict)
    )
    if manifest_vision_ids != actual_vision_ids or manifest_audio_ids != actual_audio_ids:
        raise ReportError("MEDIA_TASK_BINDING_MISMATCH", "图片或音频任务与附件提取清单不一致")
    vision_summary = validate_vision_evidence(vision_evidence, vision_corpus, vision_tasks, vision_tasks_sha256)
    audio_summary = validate_audio_evidence(
        audio_evidence, source_corpus, vision_corpus, audio_tasks, audio_tasks_sha256,
        audio_receipts_dir, audio_transcripts_dir,
    )
    readable_corpus = "\n".join(
        line for line in source_corpus.splitlines()
        if not line.startswith(("=== ", "--- "))
    )
    if len(source_key(readable_corpus)) < 20:
        raise ReportError("SOURCE_CORPUS_EMPTY", "附件没有产生可用正文或已核验逐字稿")
    vision_artifacts = vision_evidence.get("artifacts")
    if not isinstance(vision_artifacts, dict) or artifacts.get("source_corpus_sha256") != vision_artifacts.get("source_corpus_sha256"):
        raise ReportError("SOURCE_CORPUS_CHANGED", "初始语料与材料提取清单的哈希不一致")
    corpus_numeric = re.sub(r"[\s,，]", "", source_corpus)
    literals = set().union(*(numeric_literals(value) for value in fact_values(facts, schema, evidentiary_only=True)))
    unsupported_numbers = sorted(literal for literal in literals if not re.search(rf"(?<!\d){re.escape(literal)}(?!\d)", corpus_numeric))
    if unsupported_numbers:
        raise ReportError(
            "FACT_NUMERIC_UNSUPPORTED", "结构化事实含有未在材料文本或 OCR 文本中出现的数字",
            {"values": unsupported_numbers[:20], "total": len(unsupported_numbers)},
        )
    corpus_key = source_key(source_corpus)
    unsupported_facts = [
        path for path, value in fact_items(facts, schema, evidentiary_only=True)
        if value and not is_declared_unknown(value) and not source_literal_supported(value, corpus_key)
    ]
    if unsupported_facts:
        raise ReportError(
            "FACT_LITERAL_UNSUPPORTED", "用户可见事实含有无原文支持的内容",
            {"fields": unsupported_facts[:40], "total": len(unsupported_facts)},
        )
    privacy_violations = [
        path for path, value in all_visible_fact_items(facts, schema)
        if PRC_ID_PATTERN.search(value) or MOBILE_PATTERN.search(value) or contains_bank_card(value)
    ]
    if privacy_violations:
        raise ReportError(
            "PERSONAL_DATA_UNMASKED", "报告事实包含未脱敏身份证号、手机号或银行卡号",
            {"fields": privacy_violations[:40]},
        )
    validate_semantic_completion(scalars, rows, schema, corpus_key)
    case_type = scalar_text(scalars.get("case_type"), "scalars.case_type")
    case_status = scalar_text(scalars.get("case_status"), "scalars.case_status")
    base_case_type = scalar_text(base_fields.get("case_type"), "base_fields.case_type")
    base_case_status = scalar_text(base_fields.get("case_status"), "base_fields.case_status")
    if case_type not in {"诉讼", "仲裁", *DECLARED_UNKNOWN_VALUES} or base_case_type not in {"", "诉讼", "仲裁"}:
        raise ReportError("BASE_FIELD_INVALID", "报告案件类型必须是诉讼、仲裁或明确未知态；Base 只能写已知选项")
    allowed_status = {"", "待立案", "审理中", "已结案", "已归档"}
    if case_status not in (allowed_status | DECLARED_UNKNOWN_VALUES) or base_case_status not in allowed_status:
        raise ReportError("BASE_FIELD_INVALID", "报告案件状态必须是 Base 选项或明确未知态")
    if case_type in {"诉讼", "仲裁"} and base_case_type != case_type:
        raise ReportError("BASE_FIELD_MISMATCH", "报告案件类型与 Base 回写值不一致")
    if case_status in allowed_status - {""} and base_case_status != case_status:
        raise ReportError("BASE_FIELD_MISMATCH", "报告案件状态与 Base 回写值不一致")
    known_party_names = [
        value for name in ("our_name", "opponent_name")
        if (value := scalar_text(scalars.get(name), f"scalars.{name}")) and not is_declared_unknown(value)
    ]
    if known_party_names and not scalar_text(base_fields.get("case_name"), "base_fields.case_name"):
        raise ReportError("BASE_FIELD_MISSING", "已识别当事人但未生成案件名称")
    return {
        "status": "valid", "numeric_literal_count": len(literals), "material_count": sum(counts.values()),
        "complete_materials": counts["complete"], "partial_materials": counts["partial"], "failed_materials": counts["failed"],
        **vision_summary, **audio_summary,
    }


def validate_report(source: str, template: str, schema: dict[str, Any], facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if UNRESOLVED_PATTERN.search(source) or ROW_PATTERN.search(source):
        raise ReportError("TEMPLATE_MARKER_REMAINS", "报告仍有模板标记")
    actual = parse_fragment(source)
    template_root = parse_fragment(template)
    compare_structure(actual, template_root, template, schema)
    actual_widths = document_table_widths(actual)
    expected_width = schema.get("table_width")
    if not isinstance(expected_width, int) or set(actual_widths) != {expected_width}:
        raise ReportError(
            "REPORT_LAYOUT_INVALID", "报告表格总宽不一致",
            {"expected": expected_width, "actual": sorted(set(actual_widths))},
        )
    full_text = element_text(actual)
    template_text = element_text(template_root)
    forbidden = [item for item in schema.get("forbidden_text", []) if item and full_text.count(item) > template_text.count(item)]
    if forbidden:
        raise ReportError("REPORT_TEXT_INVALID", "报告包含禁用占位词或错误术语", {"matches": forbidden})
    if facts is not None:
        validate_fact_shape(facts, template, schema)
        case_number = scalar_text(facts["scalars"].get("case_number"), "scalars.case_number")
        title = next((element_text(item) for item in actual if local_name(item.tag) == "title"), "")
        if case_number and case_number not in title:
            raise ReportError("REPORT_TITLE_INVALID", "文档标题缺少案件编号", {"case_number": case_number})
        normalized_report = normalize_text(full_text)
        missing = sorted({value for value in fact_values(facts, schema) if len(normalize_text(value)) >= 2 and normalize_text(value) not in normalized_report})
        if missing:
            raise ReportError("REPORT_FACT_MISSING", "结构化事实未完整写入报告", {"values": missing[:20], "total": len(missing)})
    return {"status": "valid", "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), **schema["structure"]}


def validate_remote_matches_expected(remote_source: str, expected_source: str) -> None:
    remote_root = parse_fragment(remote_source)
    expected_root = parse_fragment(expected_source)
    remote_title = next((element_text(item) for item in remote_root if local_name(item.tag) == "title"), "")
    expected_title = next((element_text(item) for item in expected_root if local_name(item.tag) == "title"), "")
    if remote_title != expected_title or structure_signature(remote_root) != structure_signature(expected_root):
        raise ReportError("REPORT_REMOTE_MISMATCH", "远程文档章节与表格单元格未精确匹配本地渲染结果")


def extract_document_content(path: Path, expected_document_token: str = "") -> tuple[str, int | None]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("REPORT_UNAVAILABLE", f"无法读取报告：{path}", {"reason": str(exc)}) from exc
    if not source.lstrip().startswith(("{", "[")):
        if expected_document_token:
            raise ReportError("REPORT_READBACK_INVALID", "远程文档读回必须是带对象标识的 JSON 回执")
        return source, None
    try:
        root = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ReportError("REPORT_READBACK_INVALID", "文档读回不是单一 JSON 对象") from exc
    data = root.get("data") if isinstance(root, dict) else None
    document = data.get("document") if isinstance(data, dict) else None
    if (
        not isinstance(root, dict) or root.get("ok") is not True or root.get("identity") != "user"
        or not isinstance(document, dict)
    ):
        raise ReportError("REPORT_READBACK_INVALID", "文档读回不是用户身份的成功 fetch 回执")
    document_id = scalarish(document.get("document_id"))
    revision_id = document.get("revision_id")
    content = document.get("content")
    if (
        expected_document_token and document_id != expected_document_token
        or not isinstance(revision_id, int) or revision_id < 0
        or not isinstance(content, str) or "<title" not in content or "<table" not in content
    ):
        raise ReportError(
            "REPORT_READBACK_INVALID", "文档读回未精确绑定目标 token、revision 和正文",
            {"expected_document_token": expected_document_token, "actual_document_token": document_id},
        )
    return content, revision_id


def record_field_map(path: Path, expected_record_id: str) -> dict[str, Any]:
    root = read_json(path)
    data = root.get("data")
    fields = data.get("fields") if isinstance(data, dict) else None
    rows = data.get("data") if isinstance(data, dict) else None
    record_ids = data.get("record_id_list") if isinstance(data, dict) else None
    if (
        root.get("ok") is not True or root.get("identity") != "user"
        or record_ids != [expected_record_id]
        or not isinstance(fields, list) or not fields or not all(isinstance(item, str) for item in fields)
        or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list)
        or len(rows[0]) != len(fields)
    ):
        raise ReportError(
            "BASE_READBACK_INVALID", "Base 读回未精确绑定目标记录或缺少完整字段行",
            {"expected_record_id": expected_record_id, "actual_record_ids": record_ids},
        )
    return dict(zip(fields, rows[0]))


def record_attachment_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({
        scalarish(item.get("file_token")) for item in value if isinstance(item, dict) and scalarish(item.get("file_token"))
    })


def record_uploader_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({
        scalarish(item.get("id")) for item in value if isinstance(item, dict) and scalarish(item.get("id"))
    })


def validate_runtime_record_snapshot(runtime: dict[str, Any], record_values: dict[str, Any]) -> None:
    mismatches: list[str] = []
    if record_attachment_ids(record_values.get("案件文档")) != sorted(runtime.get("attachment_ids", [])):
        mismatches.append("attachment_ids")
    if record_uploader_ids(record_values.get("上传人")) != sorted(runtime.get("uploader_open_ids", [])):
        mismatches.append("uploader_open_ids")
    if scalarish(record_values.get("案件编号")) != scalarish(runtime.get("case_number")):
        mismatches.append("case_number")
    if mismatches:
        raise ReportError("RUNTIME_SOURCE_CHANGED", "运行期间 Base 案件编号、附件或上传人已变更", {"fields": mismatches})


def snapshot_existing_report(
    runtime: dict[str, Any], record_values: dict[str, Any], report_readback: Path,
) -> tuple[str, dict[str, Any]]:
    """Verify and snapshot the exact report revision before a supplement overwrite."""

    validate_runtime_model_contract(runtime)
    if scalarish(runtime.get("mode")) != "supplement":
        raise ReportError("INVALID_RUNTIME_INPUT", "只有 supplement 运行可以快照旧报告")
    validate_dispatch_ownership(runtime, record_values)
    validate_runtime_record_snapshot(runtime, record_values)
    document_token = scalarish(runtime.get("existing_document_token"))
    report_url = scalarish(runtime.get("existing_report_url"))
    if normalize_url(scalarish(record_values.get("AI分析结果"))) != report_url:
        raise ReportError("REPORT_STATE_INVALID", "Base 报告链接与 supplement 运行信封不一致")
    content, revision_id = extract_document_content(report_readback, document_token)
    if revision_id is None:
        raise ReportError("REPORT_READBACK_INVALID", "旧报告读回缺少 revision")
    report_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        baseline = json.loads(scalarish(record_values.get("材料处理基线")))
    except json.JSONDecodeError as exc:
        raise ReportError("REPORT_STATE_INVALID", "材料处理基线不是合法 JSON") from exc
    if not isinstance(baseline, dict) or scalarish(baseline.get("document_token")) != document_token:
        raise ReportError("REPORT_STATE_INVALID", "材料处理基线未绑定旧报告 token")
    title = next((element_text(item) for item in parse_fragment(content) if local_name(item.tag) == "title"), "")
    case_number = scalarish(runtime.get("case_number"))
    if case_number not in title:
        raise ReportError("REPORT_STATE_INVALID", "旧报告标题未绑定当前案件编号")
    current_baseline = scalarish(baseline.get("contract_version")) == EXPECTED_SKILL_VERSION
    if current_baseline:
        expected = {
            "app_token": EXPECTED_APP_TOKEN,
            "table_id": EXPECTED_TABLE_ID,
            "record_id": scalarish(runtime.get("record_id")),
            "case_number": case_number,
            "document_token": document_token,
            "report_url": report_url,
            "document_revision_id": revision_id,
            "report_content_sha256": report_hash,
        }
        mismatches = [key for key, value in expected.items() if baseline.get(key) != value]
        if mismatches:
            raise ReportError(
                "REPORT_STATE_INVALID", "远程报告修订号或内容已脱离当前基线",
                {"fields": mismatches},
            )
    else:
        legacy_url = scalarish(baseline.get("report_url"))
        if legacy_url and legacy_url != report_url:
            raise ReportError("REPORT_STATE_INVALID", "旧基线中的报告 URL 与当前记录不一致")
    return content, {
        "document_token": document_token,
        "report_url": report_url,
        "document_revision_id": revision_id,
        "report_content_sha256": report_hash,
        "legacy_baseline_migration": not current_baseline,
    }


def normalize_date(value: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    return match.group(0) if match else value.strip()


def normalize_url(value: str) -> str:
    match = re.search(r"https://[^\s)]+", value)
    return match.group(0) if match else value.strip()


def comparable(field: str, value: Any) -> Any:
    text = scalarish(value)
    if field == BASE_FIELD_NAMES["filing_date"]:
        return normalize_date(text)
    if field == "AI分析结果":
        return normalize_url(text)
    if field == "材料处理基线" and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def validate_runtime_model_contract(runtime: dict[str, Any]) -> None:
    allowed_keys = {
        "type", "operation", "app_token", "table_id", "record_id", "dispatch_id", "mode", "case_number",
        "attachment_ids", "new_attachment_ids", "uploader_open_ids", "existing_document_token",
        "existing_report_url", "component_build", "required_skill_version", "model_contract",
        "baseline_field_name", "field_contract",
    }
    if set(runtime) != allowed_keys:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封字段集不正确", {"fields": sorted(runtime)})
    if runtime.get("type") != EXPECTED_RUNTIME_TYPE:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封版本不正确")
    if runtime.get("required_skill_version") != EXPECTED_SKILL_VERSION:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封要求的 Skill 版本不正确")
    if runtime.get("component_build") != EXPECTED_COMPONENT_BUILD:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封中的小组件 build 不正确")
    if (
        runtime.get("operation") != EXPECTED_OPERATION
        or runtime.get("app_token") != EXPECTED_APP_TOKEN
        or runtime.get("table_id") != EXPECTED_TABLE_ID
        or runtime.get("baseline_field_name") != EXPECTED_BASELINE_FIELD
        or runtime.get("field_contract") != EXPECTED_FIELD_CONTRACT
    ):
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封的 Base、操作或字段契约不正确")
    contract = runtime.get("model_contract")
    if contract != EXPECTED_MODEL_CONTRACT:
        raise ReportError("MODEL_CONTRACT_MISMATCH", "运行信封中的主子模型契约不正确")
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    mode = scalarish(runtime.get("mode"))
    case_number = scalarish(runtime.get("case_number"))
    attachment_ids = runtime.get("attachment_ids")
    new_attachment_ids = runtime.get("new_attachment_ids")
    uploader_ids = runtime.get("uploader_open_ids")
    if (
        not re.fullmatch(r"rec[A-Za-z0-9_-]{1,125}", record_id)
        or not re.fullmatch(rf"odm-v67:{re.escape(record_id)}:\d{{13}}:[a-z0-9]{{6}}", dispatch_id)
        or mode not in {"initial", "supplement"} or not case_number
        or not isinstance(attachment_ids, list) or not attachment_ids
        or not all(isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{4,256}", item) for item in attachment_ids)
        or len(set(attachment_ids)) != len(attachment_ids)
        or not isinstance(new_attachment_ids, list) or not set(new_attachment_ids).issubset(set(attachment_ids))
        or len(set(new_attachment_ids)) != len(new_attachment_ids)
        or not isinstance(uploader_ids, list) or not uploader_ids
        or not all(isinstance(item, str) and re.fullmatch(r"ou_[A-Za-z0-9_-]+", item) for item in uploader_ids)
        or len(set(uploader_ids)) != len(uploader_ids)
    ):
        raise ReportError("INVALID_RUNTIME_INPUT", "运行信封的记录、任务、附件或上传人不合法")
    existing_token = scalarish(runtime.get("existing_document_token"))
    existing_url = scalarish(runtime.get("existing_report_url"))
    if mode == "initial":
        if new_attachment_ids != attachment_ids or existing_token or existing_url:
            raise ReportError("INVALID_RUNTIME_INPUT", "initial 信封的新附件或旧文档字段不正确")
    elif (
        not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", existing_token)
        or existing_url != f"https://aixuexi.feishu.cn/docx/{existing_token}"
    ):
        raise ReportError("INVALID_RUNTIME_INPUT", "supplement 信封未精确绑定旧报告 token 和 URL")


def validate_dispatch_ownership(
    runtime: dict[str, Any], record_values: dict[str, Any] | None,
    allowed_messages: Sequence[str] = ("处理中",),
) -> None:
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    if not dispatch_id or record_values is None:
        raise ReportError("DISPATCH_OWNERSHIP_UNVERIFIED", "缺少当前 Base 记录或任务标识，不能安全回写")
    status = scalarish(record_values.get("AI处理状态"))
    log = scalarish(record_values.get("执行日志/失败原因"))
    expected_logs = {f"任务 {dispatch_id}：{message}" for message in allowed_messages}
    if status != "分析中" or log not in expected_logs:
        raise ReportError(
            "DISPATCH_OWNERSHIP_LOST", "当前任务已不再持有该 Base 记录，拒绝覆盖新任务结果",
            {"status": status},
        )


def build_writeback(
    runtime: dict[str, Any], facts: dict[str, Any], manifest: dict[str, Any],
    vision_evidence: dict[str, Any], audio_evidence: dict[str, Any], source_corpus: str,
    document_token: str,
    report_url: str, document_revision_id: int, report_sha256: str,
    schema: dict[str, Any], record_values: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_runtime_model_contract(runtime)
    validate_dispatch_ownership(runtime, record_values)
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    mode = scalarish(runtime.get("mode"))
    if not record_id or not dispatch_id or mode not in {"initial", "supplement"}:
        raise ReportError("INVALID_RUNTIME_INPUT", "运行信封缺少 record_id、dispatch_id 或 mode")
    attachment_ids = runtime.get("attachment_ids")
    if not isinstance(attachment_ids, list) or not all(isinstance(item, str) and item for item in attachment_ids):
        raise ReportError("INVALID_RUNTIME_INPUT", "运行信封 attachment_ids 不合法")
    top_items = manifest.get("items")
    manifest_attachment_ids = sorted(
        scalarish(item.get("attachment_id")) for item in top_items if isinstance(item, dict)
    ) if isinstance(top_items, list) else []
    if manifest_attachment_ids != sorted(attachment_ids):
        raise ReportError("MATERIAL_DOWNLOAD_INCOMPLETE", "下载的顶层附件 token 与运行信封不一致")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", document_token)
        or report_url != f"https://aixuexi.feishu.cn/docx/{document_token}"
        or not isinstance(document_revision_id, int) or document_revision_id < 0
        or not re.fullmatch(r"[0-9a-f]{64}", report_sha256)
    ):
        raise ReportError("REPORT_STATE_INVALID", "报告 token、URL、revision 或内容哈希不合法")
    if mode == "supplement" and (
        document_token != scalarish(runtime.get("existing_document_token"))
        or report_url != scalarish(runtime.get("existing_report_url"))
    ):
        raise ReportError("REPORT_STATE_INVALID", "追加报告未绑定运行信封中的旧文档")
    base_fields = facts["base_fields"]
    patch: dict[str, Any] = {}
    for key in BASE_FIELD_KEYS:
        value = scalar_text(base_fields.get(key), f"base_fields.{key}")
        field_name = BASE_FIELD_NAMES[key]
        if mode == "supplement" and not value and record_values is not None:
            value = scalarish(record_values.get(field_name))
        patch[field_name] = f"{normalize_date(value)} 00:00:00" if key == "filing_date" and value else (value or None)
    title = scalar_text(facts["scalars"].get("document_title"), "scalars.document_title") or f"{facts['scalars']['case_number']} 诉讼/仲裁案件材料梳理报告"
    baseline = {
        "app_token": EXPECTED_APP_TOKEN,
        "table_id": EXPECTED_TABLE_ID,
        "record_id": record_id,
        "case_number": scalarish(runtime.get("case_number")),
        "document_token": document_token,
        "report_url": report_url,
        "document_revision_id": document_revision_id,
        "report_content_sha256": report_sha256,
        "processed_attachment_ids": sorted(set(attachment_ids)),
        "contract_version": schema["schema_version"],
        "component_build": scalarish(runtime.get("component_build")),
        "skill_version": scalarish(runtime.get("required_skill_version")),
        "source_corpus_sha256": hashlib.sha256(source_corpus.encode("utf-8")).hexdigest(),
        "vision_verification": {
            "schema_version": scalarish(vision_evidence.get("schema_version")),
            "expected": scalarish(vision_evidence.get("summary", {}).get("expected")),
            "received": scalarish(vision_evidence.get("summary", {}).get("received")),
            "verified_corpus_sha256": scalarish(vision_evidence.get("artifacts", {}).get("verified_corpus_sha256")),
        },
        "audio_verification": {
            "schema_version": scalarish(audio_evidence.get("schema_version")),
            "expected": scalarish(audio_evidence.get("summary", {}).get("expected")),
            "received": scalarish(audio_evidence.get("summary", {}).get("received")),
        },
        "audio_minutes": audio_minutes_baseline(audio_evidence),
    }
    patch.update({
        "AI分析结果": f"[{title}]({report_url})",
        "材料处理基线": json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
        "AI处理状态": "分析中",
        "执行日志/失败原因": f"任务 {dispatch_id}：结果已写入，待最终校验",
    })
    rollback_fields: dict[str, Any] = {}
    if record_values is not None:
        for field in patch:
            if field in {"AI处理状态", "执行日志/失败原因"}:
                continue
            value = scalarish(record_values.get(field))
            if field == BASE_FIELD_NAMES["filing_date"] and value:
                value = f"{normalize_date(value)} 00:00:00"
            rollback_fields[field] = value or None
    return {"update_records": {record_id: patch}}, {
        "record_id": record_id, "dispatch_id": dispatch_id, "mode": mode,
        "phase": "staged", "fields": patch, "rollback_fields": rollback_fields,
    }


def build_failure(
    runtime: dict[str, Any], error_code: str, record_values: dict[str, Any] | None,
    rollback_fields: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_runtime_model_contract(runtime)
    validate_dispatch_ownership(runtime, record_values, ("处理中", "结果已写入，待最终校验"))
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    mode = scalarish(runtime.get("mode"))
    if not record_id or not dispatch_id or mode not in {"initial", "supplement"} or not error_code:
        raise ReportError("INVALID_RUNTIME_INPUT", "无法构建失败回写")
    patch: dict[str, Any] = {
        "AI处理状态": "分析失败",
        "执行日志/失败原因": f"任务 {dispatch_id}：失败：{error_code}",
    }
    if mode == "supplement" and rollback_fields:
        patch.update(rollback_fields)
    if mode == "initial":
        patch["AI分析结果"] = None
        patch["材料处理基线"] = None
    return {"update_records": {record_id: patch}}, {
        "record_id": record_id, "dispatch_id": dispatch_id, "mode": mode,
        "phase": "failed", "fields": patch,
    }


def build_finalize(
    runtime: dict[str, Any], record_values: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_runtime_model_contract(runtime)
    validate_dispatch_ownership(runtime, record_values, ("结果已写入，待最终校验",))
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    patch = {
        "AI处理状态": "已完成",
        "执行日志/失败原因": f"任务 {dispatch_id}：已完成",
    }
    return {"update_records": {record_id: patch}}, {
        "record_id": record_id, "dispatch_id": dispatch_id,
        "mode": scalarish(runtime.get("mode")), "phase": "completed", "fields": patch,
    }


def validate_writeback(readback: Path, expectation: dict[str, Any]) -> dict[str, Any]:
    expected_record_id = scalarish(expectation.get("record_id"))
    if not re.fullmatch(r"rec[A-Za-z0-9_-]{1,125}", expected_record_id):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "回写期望值缺少合法 record_id")
    actual = record_field_map(readback, expected_record_id)
    expected = expectation.get("fields")
    if not isinstance(expected, dict):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "回写期望值缺少 fields")
    mismatches: dict[str, Any] = {}
    for field, value in expected.items():
        expected_value = comparable(field, value)
        actual_value = comparable(field, actual.get(field))
        if expected_value != actual_value:
            mismatches[field] = {"expected": expected_value, "actual": actual_value}
    if mismatches:
        raise ReportError("BASE_WRITEBACK_VERIFY_FAILED", "Base 同记录回写读回不一致", {"mismatches": mismatches})
    return {"status": "valid", "verified_field_count": len(expected)}


def validate_permission_response(root: dict[str, Any], member_id: str) -> dict[str, Any]:
    data = root.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if root.get("ok") is not True or root.get("identity") != "user" or not isinstance(items, list):
        raise ReportError(
            "DOC_PERMISSION_READBACK_INVALID",
            "权限校验输入不是用户身份的协作者列表远端读回",
            {"required_shape": "ok=true,identity=user,data.items[]"},
        )
    list_items = [item for item in items if isinstance(item, dict)]
    matched = [item for item in list_items if scalarish(item.get("member_id")) == member_id]
    if not any(scalarish(item.get("perm")) == "full_access" for item in matched):
        raise ReportError(
            "DOC_PERMISSION_GRANT_FAILED",
            "协作者列表读回未包含目标上传人的 full_access",
            {
                "member_id_found": bool(matched),
                "permissions": sorted({scalarish(item.get("perm")) for item in matched}),
            },
        )
    return {"status": "valid", "member_id": member_id, "permission": "full_access"}


def validate_permission(path: Path, member_id: str, document_token: str) -> dict[str, Any]:
    receipt = read_json(path)
    if set(receipt) != {"receipt_type", "operation", "resource", "response_sha256", "response"}:
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "权限读回回执字段集不正确")
    resource = receipt.get("resource")
    response = receipt.get("response")
    if (
        receipt.get("receipt_type") != "drive-member-list/v1"
        or receipt.get("operation") != "drive.member-list"
        or resource != {"type": "docx", "token": document_token}
        or not isinstance(response, dict)
    ):
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "权限读回未绑定目标 docx token")
    canonical_response = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if receipt.get("response_sha256") != hashlib.sha256(canonical_response).hexdigest():
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "权限读回响应哈希不一致")
    output = validate_permission_response(response, member_id)
    output["document_token"] = document_token
    return output


def capture_permission(document_token: str, member_id: str, output_path: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,256}", document_token):
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "目标文档 token 不合法")
    if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", member_id):
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "目标上传人 open_id 不合法")
    executable = shutil.which("lark-cli")
    if not executable:
        raise ReportError("DOC_PERMISSION_READBACK_FAILED", "运行环境找不到 lark-cli")
    try:
        completed = subprocess.run(
            [
                executable, "drive", "+member-list", "--as", "user", "--token", document_token,
                "--type", "docx", "--format", "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReportError("DOC_PERMISSION_READBACK_FAILED", "协作者列表远端读回失败", {"reason": str(exc)}) from exc
    if completed.returncode != 0:
        raise ReportError(
            "DOC_PERMISSION_READBACK_FAILED",
            "协作者列表远端读回失败",
            {"returncode": completed.returncode, "stderr": completed.stderr.strip()[-1200:]},
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "协作者列表不是有效 JSON") from exc
    if not isinstance(response, dict):
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "协作者列表远端读回不是对象")
    validate_permission_response(response, member_id)
    canonical_response = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt = {
        "receipt_type": "drive-member-list/v1",
        "operation": "drive.member-list",
        "resource": {"type": "docx", "token": document_token},
        "response_sha256": hashlib.sha256(canonical_response).hexdigest(),
        "response": response,
    }
    atomic_write(output_path, json.dumps(receipt, ensure_ascii=False, indent=2))
    return {
        "status": "captured",
        "document_token": document_token,
        "member_id": member_id,
        "permission": "full_access",
        "output": str(output_path.resolve()),
    }


def atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build, render and verify a dispute report.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scaffold = subparsers.add_parser("scaffold")
    scaffold.add_argument("--case-number", required=True)
    scaffold.add_argument("--manifest", type=Path, required=True)
    scaffold.add_argument("--output", type=Path, required=True)
    evidence = subparsers.add_parser("validate-facts")
    evidence.add_argument("--facts", type=Path, required=True)
    evidence.add_argument("--source-corpus", type=Path, required=True)
    evidence.add_argument("--vision-corpus", type=Path, required=True)
    evidence.add_argument("--manifest", type=Path, required=True)
    evidence.add_argument("--vision-evidence", type=Path, required=True)
    evidence.add_argument("--vision-tasks", type=Path, required=True)
    evidence.add_argument("--audio-evidence", type=Path, required=True)
    evidence.add_argument("--audio-tasks", type=Path, required=True)
    evidence.add_argument("--audio-receipts-dir", type=Path, required=True)
    evidence.add_argument("--audio-transcripts-dir", type=Path, required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--facts", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--facts", type=Path)
    validate.add_argument("--document-token", default="")
    validate.add_argument("--expected-report", type=Path)
    ownership = subparsers.add_parser("validate-dispatch")
    ownership.add_argument("--runtime", type=Path, required=True)
    ownership.add_argument("--record-readback", type=Path, required=True)
    snapshot = subparsers.add_parser("snapshot-existing")
    snapshot.add_argument("--runtime", type=Path, required=True)
    snapshot.add_argument("--record-readback", type=Path, required=True)
    snapshot.add_argument("--report-readback", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--metadata", type=Path, required=True)
    verify_snapshot = subparsers.add_parser("verify-snapshot")
    verify_snapshot.add_argument("--input", type=Path, required=True)
    verify_snapshot.add_argument("--document-token", required=True)
    verify_snapshot.add_argument("--snapshot", type=Path, required=True)
    writeback = subparsers.add_parser("build-writeback")
    writeback.add_argument("--runtime", type=Path, required=True)
    writeback.add_argument("--facts", type=Path, required=True)
    writeback.add_argument("--manifest", type=Path, required=True)
    writeback.add_argument("--vision-evidence", type=Path, required=True)
    writeback.add_argument("--vision-tasks", type=Path, required=True)
    writeback.add_argument("--audio-evidence", type=Path, required=True)
    writeback.add_argument("--audio-tasks", type=Path, required=True)
    writeback.add_argument("--audio-receipts-dir", type=Path, required=True)
    writeback.add_argument("--audio-transcripts-dir", type=Path, required=True)
    writeback.add_argument("--source-corpus", type=Path, required=True)
    writeback.add_argument("--vision-corpus", type=Path, required=True)
    writeback.add_argument("--document-token", required=True)
    writeback.add_argument("--report-url", required=True)
    writeback.add_argument("--record-readback", type=Path, required=True)
    writeback.add_argument("--report-readback", type=Path, required=True)
    writeback.add_argument("--expected-report", type=Path, required=True)
    writeback.add_argument("--permissions-dir", type=Path, required=True)
    writeback.add_argument("--output", type=Path, required=True)
    writeback.add_argument("--expectation", type=Path, required=True)
    failure = subparsers.add_parser("build-failure")
    failure.add_argument("--runtime", type=Path, required=True)
    failure.add_argument("--error-code", required=True)
    failure.add_argument("--record-readback", type=Path, required=True)
    failure.add_argument("--output", type=Path, required=True)
    failure.add_argument("--expectation", type=Path, required=True)
    failure.add_argument("--staged-expectation", type=Path)
    finalize = subparsers.add_parser("build-finalize")
    finalize.add_argument("--runtime", type=Path, required=True)
    finalize.add_argument("--record-readback", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--expectation", type=Path, required=True)
    verify_writeback = subparsers.add_parser("validate-writeback")
    verify_writeback.add_argument("--input", type=Path, required=True)
    verify_writeback.add_argument("--expectation", type=Path, required=True)
    permission = subparsers.add_parser("validate-permission")
    permission.add_argument("--input", type=Path, required=True)
    permission.add_argument("--member-id", required=True)
    permission.add_argument("--document-token", required=True)
    capture = subparsers.add_parser("capture-permission")
    capture.add_argument("--document-token", required=True)
    capture.add_argument("--member-id", required=True)
    capture.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        template, schema = load_contract(DEFAULT_TEMPLATE, DEFAULT_SCHEMA)
        if args.command == "scaffold":
            manifest = read_json(args.manifest)
            result = build_scaffold(args.case_number.strip(), manifest, template, schema)
            atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2))
            output: dict[str, Any] = {"status": "created", "output": str(args.output.resolve()), "material_count": len(manifest_entries(manifest))}
        elif args.command == "validate-facts":
            try:
                corpus = args.source_corpus.read_text(encoding="utf-8")
                vision_corpus = args.vision_corpus.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("SOURCE_CORPUS_UNAVAILABLE", "无法读取材料文本或 OCR 汇总", {"reason": str(exc)}) from exc
            output = validate_fact_evidence(
                read_json(args.facts), template, schema, corpus, vision_corpus,
                read_json(args.manifest), read_json(args.vision_evidence), read_json(args.vision_tasks),
                sha256_file(args.vision_tasks), read_json(args.audio_evidence), read_json(args.audio_tasks),
                sha256_file(args.audio_tasks), args.audio_receipts_dir, args.audio_transcripts_dir,
            )
        elif args.command == "render":
            facts = read_json(args.facts)
            rendered = render_report(facts, template, schema)
            atomic_write(args.output, rendered)
            output = validate_report(rendered, template, schema, facts)
            output.update({"output": str(args.output.resolve()), "bytes": len(rendered.encode("utf-8"))})
        elif args.command == "validate":
            facts = read_json(args.facts) if args.facts else None
            content, revision_id = extract_document_content(args.input, args.document_token.strip())
            output = validate_report(content, template, schema, facts)
            if args.expected_report:
                try:
                    expected_source = args.expected_report.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ReportError("REPORT_UNAVAILABLE", "无法读取本地预期报告", {"reason": str(exc)}) from exc
                validate_remote_matches_expected(content, expected_source)
            if revision_id is not None:
                output["document_revision_id"] = revision_id
            output["input"] = str(args.input.resolve())
        elif args.command == "validate-dispatch":
            runtime = read_json(args.runtime)
            validate_runtime_model_contract(runtime)
            validate_dispatch_ownership(runtime, record_field_map(args.record_readback, scalarish(runtime.get("record_id"))))
            output = {"status": "valid", "dispatch_owner": scalarish(runtime.get("dispatch_id"))}
        elif args.command == "snapshot-existing":
            runtime = read_json(args.runtime)
            record_values = record_field_map(args.record_readback, scalarish(runtime.get("record_id")))
            content, metadata = snapshot_existing_report(runtime, record_values, args.report_readback)
            atomic_write(args.output, content)
            atomic_write(args.metadata, json.dumps(metadata, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "metadata": str(args.metadata.resolve()), **metadata}
        elif args.command == "verify-snapshot":
            content, revision_id = extract_document_content(args.input, args.document_token.strip())
            try:
                expected = args.snapshot.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("REPORT_UNAVAILABLE", "无法读取文档快照", {"reason": str(exc)}) from exc
            validate_remote_matches_expected(content, expected)
            output = {"status": "valid", "document_revision_id": revision_id}
        elif args.command == "build-writeback":
            try:
                corpus = args.source_corpus.read_text(encoding="utf-8")
                vision_corpus = args.vision_corpus.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("SOURCE_CORPUS_UNAVAILABLE", "无法读取最终材料语料", {"reason": str(exc)}) from exc
            runtime = read_json(args.runtime)
            validate_runtime_model_contract(runtime)
            facts = read_json(args.facts)
            manifest = read_json(args.manifest)
            vision_evidence = read_json(args.vision_evidence)
            vision_tasks = read_json(args.vision_tasks)
            audio_evidence = read_json(args.audio_evidence)
            audio_tasks = read_json(args.audio_tasks)
            validate_fact_evidence(
                facts, template, schema, corpus, vision_corpus, manifest,
                vision_evidence, vision_tasks, sha256_file(args.vision_tasks),
                audio_evidence, audio_tasks, sha256_file(args.audio_tasks),
                args.audio_receipts_dir, args.audio_transcripts_dir,
            )
            document_token = args.document_token.strip()
            remote_content, document_revision_id = extract_document_content(args.report_readback, document_token)
            validate_report(remote_content, template, schema, facts)
            try:
                expected_report = args.expected_report.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("REPORT_UNAVAILABLE", "无法读取本地预期报告", {"reason": str(exc)}) from exc
            validate_remote_matches_expected(remote_content, expected_report)
            try:
                permissions_dir = args.permissions_dir.resolve(strict=True)
            except OSError as exc:
                raise ReportError("DOC_PERMISSION_READBACK_INVALID", "权限读回目录不存在", {"reason": str(exc)}) from exc
            if not permissions_dir.is_dir():
                raise ReportError("DOC_PERMISSION_READBACK_INVALID", "权限读回路径不是目录")
            for member_id in runtime.get("uploader_open_ids", []):
                validate_permission(permissions_dir / f"{member_id}.json", member_id, document_token)
            record_id = scalarish(runtime.get("record_id"))
            record_values = record_field_map(args.record_readback, record_id)
            validate_runtime_record_snapshot(runtime, record_values)
            update, expectation = build_writeback(
                runtime, facts, manifest, vision_evidence, audio_evidence, corpus, document_token,
                args.report_url.strip(), document_revision_id if document_revision_id is not None else -1,
                hashlib.sha256(remote_content.encode("utf-8")).hexdigest(), schema, record_values,
            )
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "build-failure":
            runtime = read_json(args.runtime)
            rollback_fields = None
            if args.staged_expectation:
                staged = read_json(args.staged_expectation)
                value = staged.get("rollback_fields")
                if (
                    not isinstance(value, dict)
                    or staged.get("phase") != "staged"
                    or scalarish(staged.get("record_id")) != scalarish(runtime.get("record_id"))
                    or scalarish(staged.get("dispatch_id")) != scalarish(runtime.get("dispatch_id"))
                    or scalarish(staged.get("mode")) != scalarish(runtime.get("mode"))
                ):
                    raise ReportError("WRITEBACK_EXPECTATION_INVALID", "阶段回写期望不包含当前记录的回滚快照")
                rollback_fields = value
            update, expectation = build_failure(
                runtime, args.error_code.strip(),
                record_field_map(args.record_readback, scalarish(runtime.get("record_id"))), rollback_fields,
            )
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "build-finalize":
            runtime = read_json(args.runtime)
            update, expectation = build_finalize(
                runtime, record_field_map(args.record_readback, scalarish(runtime.get("record_id"))),
            )
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "validate-writeback":
            output = validate_writeback(args.input, read_json(args.expectation))
        elif args.command == "validate-permission":
            output = validate_permission(args.input, args.member_id.strip(), args.document_token.strip())
        else:
            output = capture_permission(args.document_token.strip(), args.member_id.strip(), args.output)
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ReportError as exc:
        print(json.dumps({"status": "invalid", "error_code": exc.code, "message": str(exc), "details": exc.details}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
