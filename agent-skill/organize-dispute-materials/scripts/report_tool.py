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

from evidence_contract import (
    AUDIO_PACK_SCHEMA,
    AUDIO_RESULT_SCHEMA,
    MAIN_AGENT_NAME,
    VISION_AGENT_ID,
    VISION_AGENT_NAME,
    VISION_RESULT_SCHEMA,
    VISION_TASK_SCHEMA,
    vision_agent_prompt,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent
REFERENCES = SKILL_ROOT / "references"
DEFAULT_TEMPLATE = REFERENCES / "report-template.xml"
DEFAULT_SCHEMA = REFERENCES / "render-schema.json"
SCALAR_PATTERN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")
ROW_PATTERN = re.compile(r"<!--([A-Za-z][A-Za-z0-9_]*_rows)-->")
UNRESOLVED_PATTERN = re.compile(r"\{\{[^{}]+\}\}")
WHITESPACE_PATTERN = re.compile(r"\s+")
NUMERIC_LITERAL_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![A-Za-z0-9])")
CORPUS_SECTION_PATTERN = re.compile(
    r"^=== [^\n]*?\[sha256=([0-9a-f]{64})\][^\n]* ===\n(.*?)(?=^=== |\Z)",
    re.MULTILINE | re.DOTALL,
)
BASE_FIELD_KEYS = ("case_name", "case_type", "filing_date", "case_status")
BASE_FIELD_NAMES = {
    "case_name": "案件名称",
    "case_type": "案件类型",
    "filing_date": "立案（收案）日期",
    "case_status": "案件状态",
}
NON_EVIDENTIARY_ROWS = {"completeness_rows", "quality_rows"}
VISION_PACK_SCHEMA = "vision-evidence-pack/v4"
EXPECTED_RUNTIME_TYPE = "dispute-material-run/v6.7"
EXPECTED_SKILL_VERSION = "6.8.1"
EXPECTED_COMPONENT_BUILD = "6.8.1-skill-6.8.1"
EXPECTED_OPERATION = "process_target_record"
STRONG_LEGACY_BASELINE_VERSIONS = {"6.7.0", "6.7.1", "6.7.2", "6.7.3", "6.7.4"}
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
EXPECTED_AGENT_CONTRACT = {
    "main_agent_name": MAIN_AGENT_NAME,
    "vision_agent_name": VISION_AGENT_NAME,
    "vision_agent_id": VISION_AGENT_ID,
    "vision_result_schema": VISION_RESULT_SCHEMA,
    "vision_transport": "native_agent_tool",
    "audio_transcription_service": "Feishu Minutes",
    "audio_result_schema": AUDIO_RESULT_SCHEMA,
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
INTERNAL_REPORT_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])(?:lark-cli|python3?|npm|node|subprocess|dispatch_id|component_build|required_skill_version)(?![A-Za-z0-9_])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])(?:runtime\.json|case-facts\.json|report_tool\.py|material_tool\.py|vision_tool\.py|audio_tool\.py)(?![A-Za-z0-9_])", re.IGNORECASE),
    re.compile(r"(?:^|[\s\"'])/(?:Users|private/tmp|tmp|var/folders|home)/", re.IGNORECASE),
    re.compile(r"odm-v\d+:[A-Za-z0-9_-]+:\d{13}:[a-z0-9]{6}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])(?:agent|rec|fld|tbl)_[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_])(?:openapi-conversation|AILY_WORKDIR|AGENT_TRACE)(?![A-Za-z0-9_])", re.IGNORECASE),
)
EXPLANATORY_PARENTHESIS_PATTERN = re.compile(
    r"(?:待核|未载明|不适用|已核验|已确认|风险|建议|说明|备注|原因|推测|可能|分析|摘要|初步)[^。；\n]{0,16}（[^（）\n]{1,80}）"
)
SEMANTIC_ATTRIBUTES = {"width", "span", "rowspan", "colspan", "href", "url", "checked", "type"}
QUALITY_RESULTS = {
    "事实来源核验": "用户可见事实均通过材料原文支持校验",
    "当前程序识别": "当前程序、前置程序和关联案件已分层核验",
    "金额复核": "请求金额及计算字段已按当前请求材料复核",
    "空值语义": "未载明、不适用、待核和可留空字段已按约定处理",
    "交付内容规范": "报告正文仅包含案件事实、分析结论和交付所需信息",
}


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


def document_table_width_vectors(root: ET.Element) -> list[tuple[int, ...]]:
    vectors: list[tuple[int, ...]] = []
    for table in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1].lower() == "table"):
        colgroup = next((item for item in table if item.tag.rsplit("}", 1)[-1].lower() == "colgroup"), None)
        if colgroup is None:
            raise ReportError("REPORT_LAYOUT_INVALID", "表格缺少 colgroup 列宽定义")
        try:
            vector = tuple(
                width
                for item in colgroup if item.tag.rsplit("}", 1)[-1].lower() == "col"
                for width in [int(item.attrib["width"])] * int(item.attrib.get("span", "1"))
            )
        except (KeyError, ValueError) as exc:
            raise ReportError("REPORT_LAYOUT_INVALID", "表格列宽定义不合法") from exc
        if not vector:
            raise ReportError("REPORT_LAYOUT_INVALID", "表格列宽定义为空")
        vectors.append(vector)
    return vectors


def document_table_widths(root: ET.Element) -> list[int]:
    return [sum(vector) for vector in document_table_width_vectors(root)]


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


def source_literal_supported_by_sections(value: str, sections: Sequence[str]) -> bool:
    keys = [source_key(section) for section in sections]
    normalized = source_key(value)
    if not normalized:
        return True
    if any(normalized in key for key in keys):
        return True
    parts = [source_key(item) for item in re.split(r"[；;、|/\n]+", value) if source_key(item)]
    return len(parts) > 1 and all(any(part in key for key in keys) for part in parts)


def corpus_sections(source_corpus: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    for match in CORPUS_SECTION_PATTERN.finditer(source_corpus):
        sections.setdefault(match.group(1), []).append(match.group(2))
    return {source_hash: "\n".join(values) for source_hash, values in sections.items()}


def validate_source_references(
    facts: dict[str, Any], schema: dict[str, Any], source_corpus: str,
) -> dict[str, int]:
    """Bind every substantive visible fact to quoted text from named source hashes."""

    expected = {
        path: value for path, value in fact_items(facts, schema, evidentiary_only=True)
        if value and not is_declared_unknown(value)
    }
    source_refs = facts.get("source_refs")
    if not isinstance(source_refs, dict):
        raise ReportError("SOURCE_REFERENCE_INVALID", "case-facts.json 缺少 source_refs 对象")
    missing = sorted(set(expected) - set(source_refs))
    extra = sorted(set(source_refs) - set(expected))
    if missing or extra:
        raise ReportError(
            "SOURCE_REFERENCE_INVALID", "事实来源引用必须与全部实质事实逐项对应",
            {"missing": missing[:40], "extra": extra[:40]},
        )
    sections = corpus_sections(source_corpus)
    if not sections:
        raise ReportError("SOURCE_REFERENCE_INVALID", "核验语料缺少 source sha256 分段标识")
    quote_count = 0
    for path, value in expected.items():
        refs = source_refs.get(path)
        if not isinstance(refs, list) or not refs:
            raise ReportError("SOURCE_REFERENCE_INVALID", "实质事实缺少来源引用", {"field": path})
        cited_texts: list[str] = []
        seen: set[tuple[str, str]] = set()
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict) or set(ref) != {"source_sha256", "quote"}:
                raise ReportError("SOURCE_REFERENCE_INVALID", "来源引用字段集不正确", {"field": path, "index": index})
            source_hash = scalarish(ref.get("source_sha256"))
            quote = scalar_text(ref.get("quote"), f"source_refs.{path}[{index}].quote")
            if not re.fullmatch(r"[0-9a-f]{64}", source_hash) or source_hash not in sections:
                raise ReportError("SOURCE_REFERENCE_INVALID", "来源引用未绑定当前语料中的材料哈希", {"field": path, "index": index})
            key = (source_hash, source_key(quote))
            if not key[1] or key in seen or key[1] not in source_key(sections[source_hash]):
                raise ReportError("SOURCE_QUOTE_UNSUPPORTED", "来源引文未在指定材料中逐字出现", {"field": path, "index": index})
            seen.add(key)
            cited_texts.append(sections[source_hash])
            quote_count += 1
        if not source_literal_supported_by_sections(value, cited_texts):
            raise ReportError("FACT_SOURCE_RELATION_UNSUPPORTED", "事实值未被其指定来源材料支持", {"field": path})
    return {"source_reference_count": len(expected), "source_quote_count": quote_count}


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


def document_semantic_signature(root: ET.Element) -> tuple[Any, ...]:
    """Return visible structure, text and layout/link attributes."""

    def visit(element: ET.Element) -> tuple[Any, ...]:
        attributes = tuple(sorted(
            (local_name(key), normalize_text(value))
            for key, value in element.attrib.items()
            if local_name(key) in SEMANTIC_ATTRIBUTES
        ))
        return (
            local_name(element.tag),
            attributes,
            normalize_text(element.text or ""),
            tuple(visit(child) for child in element),
            normalize_text(element.tail or ""),
        )

    return tuple(visit(child) for child in root)


def internal_report_matches(text: str) -> list[str]:
    return sorted({match.group(0).strip() for pattern in INTERNAL_REPORT_PATTERNS for match in pattern.finditer(text)})


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
    if evidentiary_only:
        base_fields = facts.get("base_fields", {})
        if isinstance(base_fields, dict):
            for name in ("case_name", "filing_date"):
                path = f"base_fields.{name}"
                yield path, scalar_text(base_fields.get(name), path)


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
    base_fields = facts.get("base_fields", {})
    if isinstance(base_fields, dict):
        for name, value in base_fields.items():
            path = f"base_fields.{name}"
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


def quality_rows_for_manifest(entries: Sequence[dict[str, Any]], required_checks: Sequence[str]) -> list[dict[str, str]]:
    counts = Counter(scalarish(item.get("status")) or "failed" for item in entries)
    return [{
        "index": "1", "check": "材料读取情况",
        "result": f"共{len(entries)}项，完整{counts['complete']}项，部分{counts['partial']}项，无法解析{counts['failed']}项",
    }] + [
        {"index": str(index), "check": check, "result": QUALITY_RESULTS.get(check, "")}
        for index, check in enumerate(required_checks[1:], 2)
    ]


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
    rows["quality_rows"] = quality_rows_for_manifest(entries, schema.get("required_quality_checks", []))
    return {
        "scalars": scalars, "rows": rows, "base_fields": {key: "" for key in BASE_FIELD_KEYS},
        "source_refs": {},
    }


def validate_manifest_coverage(rows: dict[str, Any], manifest: dict[str, Any], schema: dict[str, Any]) -> Counter[str]:
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
    expected_quality = quality_rows_for_manifest(entries, schema.get("required_quality_checks", []))
    if rows["quality_rows"] != expected_quality:
        raise ReportError("QUALITY_GATE_INVALID", "质量自检必须由程序按材料清单确定性生成，不得由纠纷材料整理专员改写")
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


def vision_receipt_set_sha256(tasks: list[dict[str, Any]], receipts_dir: Path) -> str:
    entries = [
        {
            "task_id": scalarish(task.get("task_id")),
            "receipt_sha256": sha256_file(receipts_dir.resolve() / f"{scalarish(task.get('task_id'))}.receipt.json"),
        }
        for task in sorted(tasks, key=lambda item: scalarish(item.get("task_id")))
    ]
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_vision_native_artifacts(
    item: dict[str, Any], task: dict[str, Any], receipts_dir: Path,
) -> None:
    task_id = scalarish(task.get("task_id"))
    collection = item.get("collection")
    if not isinstance(collection, dict):
        raise ReportError("VISION_RECEIPT_INVALID", "视觉证据缺少收集回执", {"task_id": task_id})
    receipt_path = receipts_dir.resolve() / f"{task_id}.receipt.json"
    receipt = read_json(receipt_path)
    receipt_keys = {
        "schema_version", "task_id", "source_sha256", "image_sha256", "agent_name", "transport",
        "source_binding", "source_binding_sha256", "prompt_sha256", "response_sha256", "result_sha256",
    }
    if set(receipt) != receipt_keys or receipt.get("schema_version") != "vision-native-agent-receipt/v1":
        raise ReportError("VISION_RECEIPT_INVALID", "视觉收集回执结构不正确", {"task_id": task_id})
    expected_collection = {key: receipt[key] for key in receipt_keys - {
        "schema_version", "task_id", "source_sha256", "image_sha256",
    }}
    if (
        receipt.get("task_id") != task_id
        or receipt.get("source_sha256") != task.get("source_sha256")
        or receipt.get("image_sha256") != task.get("image_sha256")
        or collection != expected_collection
    ):
        raise ReportError("VISION_RECEIPT_INVALID", "视觉收集回执未绑定当前任务", {"task_id": task_id})
    source = receipt.get("source_binding")
    if not isinstance(source, dict):
        raise ReportError("VISION_RECEIPT_INVALID", "视觉收集回执缺少 Base 来源绑定", {"task_id": task_id})
    source_hash = hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if receipt.get("source_binding_sha256") != source_hash:
        raise ReportError("VISION_RECEIPT_INVALID", "视觉 Base 来源绑定哈希不一致", {"task_id": task_id})
    expected_result = {
        "schema_version": VISION_RESULT_SCHEMA,
        "task_id": task_id,
        "source_sha256": task.get("source_sha256"),
        "image_sha256": task.get("image_sha256"),
        "producer": item.get("producer"),
        "status": item.get("status"),
        "verbatim_text": item.get("verbatim_text"),
        "uncertain_regions": item.get("uncertain_regions"),
    }
    result_path = receipts_dir.resolve().parent / "vision-results" / f"{task_id}.json"
    if not result_path.is_file() or sha256_file(result_path) != scalarish(receipt.get("result_sha256")):
        raise ReportError("VISION_RECEIPT_INVALID", "视觉结果文件缺失或哈希不一致", {"task_id": task_id})
    if read_json(result_path) != expected_result:
        raise ReportError("VISION_RECEIPT_INVALID", "视觉结果文件与证据包不一致", {"task_id": task_id})
    prompt = vision_agent_prompt(task, source)
    if (
        receipt.get("agent_name") != VISION_AGENT_NAME
        or receipt.get("transport") != "native_agent_tool"
        or receipt.get("prompt_sha256") != hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        or receipt.get("response_sha256") != receipt.get("result_sha256")
    ):
        raise ReportError("VISION_RECEIPT_INVALID", "视觉原生调用身份或哈希不正确", {"task_id": task_id})


def validate_vision_evidence(
    evidence: dict[str, Any], source_corpus: str | None = None,
    vision_tasks: dict[str, Any] | None = None, vision_tasks_sha256: str | None = None,
    receipts_dir: Path | None = None,
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
        "main_writer": MAIN_AGENT_NAME,
        "vision_worker": VISION_AGENT_NAME,
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
        collection = item.get("collection")
        collection_hashes = {
            "source_binding_sha256", "prompt_sha256", "response_sha256", "result_sha256",
        }
        source_binding = collection.get("source_binding") if isinstance(collection, dict) else None
        source_keys = {
            "schema_version", "app_token", "table_id", "record_id", "attachment_field_id",
            "attachment_field_name", "attachment_id", "attachment_name", "attachment_size",
            "attachment_sha256", "source_locator",
        }
        source_binding_hash = hashlib.sha256(
            json.dumps(source_binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest() if isinstance(source_binding, dict) else ""
        if (
            not re.fullmatch(r"vis_[0-9a-f]{20}", task_id)
            or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", image_hash)
            or item.get("status") != "complete"
            or not isinstance(producer, dict)
            or producer.get("agent_name") != VISION_AGENT_NAME
            or set(producer) != {"agent_name"}
            or not isinstance(collection, dict)
            or set(collection) != {
                "agent_name", "transport", "source_binding", *collection_hashes,
            }
            or collection.get("agent_name") != VISION_AGENT_NAME
            or collection.get("transport") != "native_agent_tool"
            or any(not re.fullmatch(r"[0-9a-f]{64}", scalarish(collection.get(key))) for key in collection_hashes)
            or not isinstance(source_binding, dict)
            or set(source_binding) != source_keys
            or source_binding.get("schema_version") != "vision-base-source/v1"
            or source_binding.get("attachment_field_name") != "案件文档"
            or source_binding.get("source_locator") != item.get("source_file")
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", scalarish(source_binding.get("attachment_id")))
            or not re.fullmatch(r"[0-9a-f]{64}", scalarish(source_binding.get("attachment_sha256")))
            or type(source_binding.get("attachment_size")) is not int
            or source_binding["attachment_size"] <= 0
            or collection.get("source_binding_sha256") != source_binding_hash
        ):
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务身份、哈希或状态不正确", {"task_id": task_id})
        if task_id in task_map:
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务 ID 重复", {"task_id": task_id})
        verbatim_text = item.get("verbatim_text")
        regions = item.get("uncertain_regions")
        regions_valid = isinstance(regions, list) and all(
            isinstance(region, dict)
            and set(region).issubset({"description", "critical", "source_ref"})
            and {"description", "critical"}.issubset(region)
            and isinstance(region.get("description"), str)
            and bool(region["description"].strip())
            and type(region.get("critical")) is bool
            and ("source_ref" not in region or isinstance(region.get("source_ref"), str) and bool(region["source_ref"].strip()))
            for region in regions
        )
        if (
            not isinstance(verbatim_text, str)
            or not verbatim_text.strip()
            or not regions_valid
            or any(region["critical"] is True for region in regions)
        ):
            raise ReportError("VISION_EVIDENCE_INCOMPLETE", "视觉证据任务仍有未决关键字段", {"task_id": task_id})
        task_map[task_id] = (source_hash, image_hash)
    required_artifacts = {
        "vision_tasks_sha256", "vision_receipts_sha256",
        "source_corpus_sha256", "verified_corpus_sha256",
    }
    if set(artifacts) != required_artifacts or any(
        value is not None and not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in artifacts.values()
    ):
        raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据包工件哈希不正确")
    if vision_tasks is not None:
        raw_expected_tasks = vision_tasks.get("tasks")
        if vision_tasks.get("schema_version") != VISION_TASK_SCHEMA or not isinstance(raw_expected_tasks, list):
            raise ReportError("VISION_TASKS_INVALID", "视觉任务清单版本或结构不正确")
        expected_map: dict[str, tuple[str, str]] = {}
        expected_task_keys = {
            "schema_version", "task_id", "source_file", "source_sha256", "unit", "page",
            "reason", "image_file", "image_sha256", "image_size", "image_transform",
            "ocr_text", "ocr_mean_confidence",
        }
        for item in raw_expected_tasks:
            if not isinstance(item, dict):
                raise ReportError("VISION_TASKS_INVALID", "视觉任务必须是对象")
            task_id = scalarish(item.get("task_id"))
            if set(item) != expected_task_keys or not task_id or task_id in expected_map:
                raise ReportError("VISION_TASKS_INVALID", "视觉任务 ID 缺失或重复", {"task_id": task_id})
            expected_map[task_id] = (scalarish(item.get("source_sha256")), scalarish(item.get("image_sha256")))
        if expected_map != task_map or expected != len(expected_map):
            raise ReportError("VISION_EVIDENCE_MISMATCH", "视觉证据与本次任务清单不一致")
        if receipts_dir is None:
            raise ReportError("VISION_RECEIPT_INVALID", "视觉证据校验缺少回执目录")
        expected_task_objects = {
            scalarish(item.get("task_id")): item for item in raw_expected_tasks if isinstance(item, dict)
        }
        for item in tasks:
            validate_vision_native_artifacts(
                item, expected_task_objects[scalarish(item.get("task_id"))], receipts_dir,
            )
        if artifacts.get("vision_receipts_sha256") != vision_receipt_set_sha256(raw_expected_tasks, receipts_dir):
            raise ReportError("VISION_RECEIPT_INVALID", "视觉回执集合哈希不一致")
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


def owned_audio_transcript(value: str, transcripts_dir: Path, task_id: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频逐字稿必须使用相对路径", {"task_id": task_id})
    root = transcripts_dir.resolve()
    transcript = (root / relative).resolve()
    try:
        transcript.relative_to(root)
    except ValueError as exc:
        raise ReportError("AUDIO_EVIDENCE_INVALID", "音频逐字稿越出本次输出目录", {"task_id": task_id}) from exc
    return transcript


def audio_transcript_locator(value: str) -> str:
    parts = [part for part in Path(value).parts if part not in {"/", "\\"}]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")


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
    returned_sizes = {
        value.get(key) for value in walk_values(drive) if isinstance(value, dict)
        for key in ("size", "size_bytes") if type(value.get(key)) is int
    }
    if returned_sizes and returned_sizes != {item.get("transmitted_size_bytes")}:
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "云空间响应字节数与音频快照不一致", {"task_id": task_id})

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
    remote_locations = {
        audio_transcript_locator(value["transcript_file"])
        for target in target_items
        for value in walk_values(target)
        if isinstance(value, dict) and isinstance(value.get("transcript_file"), str)
        and audio_transcript_locator(value["transcript_file"])
    }
    expected_location = audio_transcript_locator(
        transcript.resolve().relative_to(transcripts_dir.resolve()).as_posix()
    )
    if remote_locations != {expected_location}:
        raise ReportError("AUDIO_REMOTE_READBACK_INVALID", "逐字稿远程位置与音频回执不一致", {"task_id": task_id})


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
            "main_writer": "纠纷材料整理专员",
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
    task_map: dict[str, tuple[str, str, int]] = {}
    reuse_bindings: list[tuple[str, str, str]] = []
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
        transmitted_hash = scalarish(item.get("transmitted_sha256"))
        transmitted_size = item.get("transmitted_size_bytes")
        reuse_source = scalarish(item.get("reuse_source"))
        reuse_task_id = scalarish(item.get("reuse_task_id"))
        provider = item.get("provider")
        if (
            not re.fullmatch(r"aud_[0-9a-f]{20}", task_id)
            or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", media_hash)
            or source_hash != media_hash
            or not re.fullmatch(r"[A-Za-z0-9_-]{4,256}", file_token)
            or not re.fullmatch(r"[a-z0-9]{8,128}", minute_token)
            or (minute_url and not re.fullmatch(rf"https://[^\s]+/minutes/{re.escape(minute_token)}(?:[/?#].*)?", minute_url))
            or not re.fullmatch(r"[0-9a-f]{64}", transcript_hash)
            or transmitted_hash != media_hash
            or type(transmitted_size) is not int
            or transmitted_size <= 0
            or item.get("status") != "complete"
            or reuse_source not in {"new_upload", "same_run", "retry_state", "receipt_refresh"}
            or (reuse_source == "same_run" and (not re.fullmatch(r"aud_[0-9a-f]{20}", reuse_task_id) or reuse_task_id == task_id))
            or (reuse_source != "same_run" and bool(reuse_task_id))
            or provider != {"service": "Feishu Minutes", "identity": "user", "mode": "remote_transcript_readback"}
        ):
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据身份、哈希、妙记或状态不正确", {"task_id": task_id})
        if task_id in task_map:
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频证据任务 ID 重复", {"task_id": task_id})
        transcript = owned_audio_transcript(
            scalarish(item.get("transcript_file")),
            transcripts_dir if transcripts_dir is not None else Path("/__invalid__"), task_id,
        )
        if not transcript.is_file() or sha256_file(transcript) != transcript_hash:
            raise ReportError("AUDIO_TRANSCRIPT_CHANGED", "音频逐字稿不存在或哈希不一致", {"task_id": task_id})
        transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
        if source_corpus is not None and source_key(transcript_text) not in corpus_key:
            transcript_missing.append(task_id)
        validate_audio_remote_artifacts(item, receipts_dir, transcripts_dir, transcript)  # type: ignore[arg-type]
        task_map[task_id] = (source_hash, media_hash, transmitted_size)
        if reuse_source == "same_run":
            reuse_bindings.append((task_id, reuse_task_id, media_hash))
    for task_id, reuse_task_id, media_hash in reuse_bindings:
        reused_task = task_map.get(reuse_task_id)
        if reused_task is None or reused_task[1] != media_hash:
            raise ReportError("AUDIO_EVIDENCE_INVALID", "音频复用任务未绑定同一媒体", {"task_id": task_id})
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
        if audio_tasks.get("schema_version") != "audio-task/v2" or not isinstance(raw_expected_tasks, list):
            raise ReportError("AUDIO_TASKS_INVALID", "音频任务清单版本或结构不正确")
        expected_map: dict[str, tuple[str, str, int]] = {}
        for item in raw_expected_tasks:
            if not isinstance(item, dict):
                raise ReportError("AUDIO_TASKS_INVALID", "音频任务必须是对象")
            task_id = scalarish(item.get("task_id"))
            if not task_id or task_id in expected_map:
                raise ReportError("AUDIO_TASKS_INVALID", "音频任务 ID 缺失或重复", {"task_id": task_id})
            size = item.get("size_bytes")
            source_hash = scalarish(item.get("source_sha256"))
            media_hash = scalarish(item.get("media_sha256"))
            media_file = scalarish(item.get("media_file"))
            media_suffix = scalarish(item.get("media_suffix")).lower()
            if (
                type(size) is not int or size <= 0
                or source_hash != media_hash
                or Path(media_file).suffix.lower() != media_suffix
            ):
                raise ReportError("AUDIO_TASKS_INVALID", "音频任务大小不正确", {"task_id": task_id})
            expected_map[task_id] = (
                source_hash, media_hash, size,
            )
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
    vision_receipts_dir: Path, audio_receipts_dir: Path, audio_transcripts_dir: Path,
) -> dict[str, Any]:
    scalars, rows, base_fields = validate_fact_shape(facts, template, schema)
    counts = validate_manifest_coverage(rows, manifest, schema)
    if counts["partial"] or counts["failed"]:
        raise ReportError(
            "MATERIAL_EXTRACTION_INCOMPLETE", "存在未完整解析的附件，不得生成完成报告",
            {"partial": counts["partial"], "failed": counts["failed"]},
        )
    artifacts = manifest.get("artifacts")
    if manifest.get("schema_version") != "2.0" or not isinstance(artifacts, dict):
        raise ReportError("MATERIAL_MANIFEST_INVALID", "材料清单缺少工件哈希与媒体任务绑定")
    for name in ("download_receipt_sha256", "download_seal_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", scalarish(artifacts.get(name))):
            raise ReportError("MATERIAL_MANIFEST_INVALID", "材料清单缺少附件下载绑定", {"field": name})
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
    vision_summary = validate_vision_evidence(
        vision_evidence, vision_corpus, vision_tasks, vision_tasks_sha256, vision_receipts_dir,
    )
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
    source_reference_summary = validate_source_references(facts, schema, source_corpus)
    privacy_violations = [
        path for path, value in all_visible_fact_items(facts, schema)
        if PRC_ID_PATTERN.search(value) or MOBILE_PATTERN.search(value) or contains_bank_card(value)
    ]
    if privacy_violations:
        raise ReportError(
            "PERSONAL_DATA_UNMASKED", "报告事实包含未脱敏身份证号、手机号或银行卡号",
            {"fields": privacy_violations[:40]},
        )
    process_violations = [
        path for path, value in all_visible_fact_items(facts, schema)
        if internal_report_matches(value)
    ]
    if process_violations:
        raise ReportError(
            "REPORT_PROCESS_LEAK", "结构化事实或 Base 回写字段包含内部运行过程信息",
            {"fields": process_violations[:40]},
        )
    parenthetical_violations = [
        path for path, value in all_visible_fact_items(facts, schema)
        if EXPLANATORY_PARENTHESIS_PATTERN.search(value)
    ]
    if parenthetical_violations:
        raise ReportError(
            "REPORT_PROCESS_LEAK", "结构化事实包含推理式括号说明",
            {"fields": parenthetical_violations[:40]},
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
    actual_case_name = scalar_text(base_fields.get("case_name"), "base_fields.case_name")
    known_party_names = [
        value for name in ("our_name", "opponent_name")
        if (value := scalar_text(scalars.get(name), f"scalars.{name}")) and not is_declared_unknown(value)
    ]
    if known_party_names and (
        not actual_case_name or any(source_key(name) not in source_key(actual_case_name) for name in known_party_names)
    ):
        raise ReportError(
            "BASE_FIELD_MISMATCH", "案件名称必须来自正式材料并包含已核验双方主体",
            {"required_parties": known_party_names, "actual": actual_case_name},
        )
    filing_date = scalar_text(scalars.get("filing_date"), "scalars.filing_date")
    expected_filing_date = "" if not filing_date or is_declared_unknown(filing_date) else normalize_date(filing_date)
    actual_filing_date = scalar_text(base_fields.get("filing_date"), "base_fields.filing_date")
    actual_filing_date = normalize_date(actual_filing_date) if actual_filing_date else ""
    if actual_filing_date != expected_filing_date:
        raise ReportError(
            "BASE_FIELD_MISMATCH", "Base 立案日期必须与报告中已核验立案日期一致",
            {"expected": expected_filing_date, "actual": actual_filing_date},
        )
    return {
        "status": "valid", "numeric_literal_count": len(literals), "material_count": sum(counts.values()),
        "complete_materials": counts["complete"], "partial_materials": counts["partial"], "failed_materials": counts["failed"],
        **source_reference_summary, **vision_summary, **audio_summary,
    }


def validate_report(source: str, template: str, schema: dict[str, Any], facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if UNRESOLVED_PATTERN.search(source) or ROW_PATTERN.search(source):
        raise ReportError("TEMPLATE_MARKER_REMAINS", "报告仍有模板标记")
    actual = parse_fragment(source)
    template_root = parse_fragment(template)
    compare_structure(actual, template_root, template, schema)
    actual_widths = document_table_widths(actual)
    actual_vectors = document_table_width_vectors(actual)
    template_vectors = document_table_width_vectors(template_root)
    expected_width = schema.get("table_width")
    if not isinstance(expected_width, int) or set(actual_widths) != {expected_width}:
        raise ReportError(
            "REPORT_LAYOUT_INVALID", "报告表格总宽不一致",
            {"expected": expected_width, "actual": sorted(set(actual_widths))},
        )
    if actual_vectors != template_vectors:
        raise ReportError(
            "REPORT_LAYOUT_INVALID", "报告表格列宽向量与固定模板不一致",
            {"mismatched_tables": [index for index, pair in enumerate(zip(actual_vectors, template_vectors)) if pair[0] != pair[1]][:20]},
        )
    full_text = element_text(actual)
    template_text = element_text(template_root)
    forbidden = [item for item in schema.get("forbidden_text", []) if item and full_text.count(item) > template_text.count(item)]
    if forbidden:
        raise ReportError("REPORT_TEXT_INVALID", "报告包含禁用占位词或错误术语", {"matches": forbidden})
    internal_matches = internal_report_matches(full_text)
    if internal_matches:
        raise ReportError("REPORT_PROCESS_LEAK", "报告包含内部运行过程信息", {"matches": internal_matches[:20]})
    parenthetical_matches = sorted(set(EXPLANATORY_PARENTHESIS_PATTERN.findall(full_text)))
    if parenthetical_matches:
        raise ReportError("REPORT_PROCESS_LEAK", "报告包含推理式括号说明", {"matches": parenthetical_matches[:20]})
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
    if document_semantic_signature(remote_root) != document_semantic_signature(expected_root):
        raise ReportError("REPORT_REMOTE_MISMATCH", "远程文档正文未精确匹配本地渲染结果")


def verify_candidate_report(
    report_readback: Path, document_token: str, expected_report: Path,
    expected_revision: int, expected_sha256: str,
) -> dict[str, Any]:
    """Ensure a rollback target is still exactly this dispatch's candidate."""

    if expected_revision < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ReportError("REPORT_STATE_INVALID", "候选报告的修订号或正文哈希不合法")
    content, revision_id = extract_document_content(report_readback, document_token)
    actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if revision_id != expected_revision or actual_hash != expected_sha256:
        raise ReportError("REPORT_ROLLBACK_CONFLICT", "候选报告已被其他修订变更，拒绝覆盖")
    try:
        expected_source = expected_report.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("REPORT_UNAVAILABLE", "无法读取本地预期报告", {"reason": str(exc)}) from exc
    validate_remote_matches_expected(content, expected_source)
    return {"status": "valid", "document_revision_id": revision_id, "report_content_sha256": actual_hash}


def semantically_matches(source: str, expected: str) -> bool:
    try:
        validate_remote_matches_expected(source, expected)
    except ReportError:
        return False
    return True


def classify_document_state(
    report_readback: Path, document_token: str, original_report: Path,
    original_metadata: Path, candidate_report: Path,
) -> dict[str, Any]:
    """Classify a supplement document before a monotonic roll-forward."""

    metadata = read_json(original_metadata)
    try:
        original = original_report.read_text(encoding="utf-8")
        candidate = candidate_report.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("REPORT_UNAVAILABLE", "无法读取旧报告快照或本次候选报告", {"reason": str(exc)}) from exc
    if (
        scalarish(metadata.get("document_token")) != document_token
        or scalarish(metadata.get("report_url")) != f"https://aixuexi.feishu.cn/docx/{document_token}"
        or not isinstance(metadata.get("document_revision_id"), int)
        or metadata.get("document_revision_id") < 0
        or scalarish(metadata.get("report_content_sha256")) != hashlib.sha256(original.encode("utf-8")).hexdigest()
    ):
        raise ReportError("REPORT_STATE_INVALID", "旧报告快照元数据与快照正文不一致")
    content, revision_id = extract_document_content(report_readback, document_token)
    if revision_id is None:
        raise ReportError("REPORT_READBACK_INVALID", "当前报告读回缺少 revision")
    current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    original_identity = (
        revision_id == metadata.get("document_revision_id")
        and current_hash == scalarish(metadata.get("report_content_sha256"))
        and semantically_matches(content, original)
    )
    if original_identity:
        state = "original"
    elif semantically_matches(content, candidate):
        state = "candidate"
    else:
        raise ReportError(
            "REPORT_ROLLBACK_CONFLICT", "当前报告既不是任务前快照，也不是本次候选正文，禁止覆盖",
            {"document_revision_id": revision_id, "report_content_sha256": current_hash},
        )
    return {
        "status": "valid", "state": state, "document_token": document_token,
        "document_revision_id": revision_id, "report_content_sha256": current_hash,
        "stage_required": state == "candidate",
    }


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

    validate_runtime_agent_contract(runtime)
    if scalarish(runtime.get("mode")) != "supplement":
        raise ReportError("INVALID_RUNTIME_INPUT", "只有 supplement 运行可以快照旧报告")
    validate_dispatch_ownership(runtime, record_values)
    validate_runtime_record_snapshot(runtime, record_values)
    document_token = scalarish(runtime.get("existing_document_token"))
    report_url = scalarish(runtime.get("existing_report_url"))
    base_report_title, base_report_url = report_field_reference(record_values.get("AI分析结果"))
    if base_report_url and base_report_url != report_url:
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
    baseline_version = scalarish(baseline.get("contract_version"))
    current_baseline = baseline_version == EXPECTED_SKILL_VERSION
    strong_baseline = current_baseline or baseline_version in STRONG_LEGACY_BASELINE_VERSIONS
    if not strong_baseline and not re.fullmatch(r"6\.5\.\d+", baseline_version):
        raise ReportError("REPORT_STATE_INVALID", "当前材料处理基线版本不支持安全迁移")
    title = next((element_text(item) for item in parse_fragment(content) if local_name(item.tag) == "title"), "")
    case_number = scalarish(runtime.get("case_number"))
    if not base_report_title or base_report_title != title:
        raise ReportError("REPORT_STATE_INVALID", "Base 报告标题与远端报告标题不一致")
    if case_number not in title:
        raise ReportError("REPORT_STATE_INVALID", "旧报告标题未绑定当前案件编号")
    if strong_baseline:
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
        expected_build = f"{baseline_version}-skill-{baseline_version}"
        if scalarish(baseline.get("component_build")) != expected_build:
            mismatches.append("component_build")
        if scalarish(baseline.get("skill_version")) != baseline_version:
            mismatches.append("skill_version")
        processed = baseline.get("processed_attachment_ids")
        current_attachments = set(runtime.get("attachment_ids", []))
        if (
            not isinstance(processed, list)
            or not processed
            or len(processed) != len(set(processed))
            or not all(isinstance(item, str) and item in current_attachments for item in processed)
        ):
            mismatches.append("processed_attachment_ids")
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
        "baseline_contract_version": baseline_version,
    }


def normalize_date(value: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        return match.group(0)
    chinese = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", value)
    if chinese:
        return f"{chinese.group(1)}-{int(chinese.group(2)):02d}-{int(chinese.group(3)):02d}"
    return value.strip()


def report_field_reference(value: Any) -> tuple[str, str]:
    """Return the display title and explicit URL from a Base report field."""

    text = normalize_text(scalarish(value))
    if not text:
        return "", ""
    markdown = re.fullmatch(r"\[([^\]]+)\]\((https://aixuexi\.feishu\.cn/docx/[A-Za-z0-9_-]{8,128})\)", text)
    if markdown:
        return normalize_text(markdown.group(1)), markdown.group(2)
    url_match = re.search(r"https://aixuexi\.feishu\.cn/docx/[A-Za-z0-9_-]{8,128}", text)
    if not url_match:
        return text, ""
    title = normalize_text((text[:url_match.start()] + text[url_match.end():]).strip("[]()"))
    return title, url_match.group(0)


def baseline_object(value: Any) -> dict[str, Any] | None:
    text = scalarish(value)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def analysis_result_matches(expected_value: Any, actual_value: Any, actual_baseline_value: Any) -> bool:
    """Compare the report field by title and canonical Base baseline identity.

    The raw Base API can collapse a text hyperlink to its display title.  A
    title-only readback is valid only when the same record's baseline binds it
    to the exact expected document token and URL.
    """

    expected_title, expected_url = report_field_reference(expected_value)
    actual_title, actual_url = report_field_reference(actual_value)
    if not expected_title or not expected_url or actual_title != expected_title:
        return False
    if actual_url:
        return actual_url == expected_url
    baseline = baseline_object(actual_baseline_value)
    if baseline is None:
        return False
    token = scalarish(baseline.get("document_token"))
    return (
        expected_url == f"https://aixuexi.feishu.cn/docx/{token}"
        and scalarish(baseline.get("report_url")) == expected_url
    )


def comparable(field: str, value: Any) -> Any:
    text = scalarish(value)
    if field == BASE_FIELD_NAMES["filing_date"]:
        return normalize_date(text)
    if field == "材料处理基线" and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def validate_runtime_recovery_contract(runtime: dict[str, Any]) -> None:
    """Validate immutable routing and ownership data without pinning a release.

    Failure recovery must still work when a rolling deployment intentionally
    rejects the package/build pair.  It may never relax the Base target, field
    contract, record or dispatch identity.
    """

    allowed_keys = {
        "type", "operation", "app_token", "table_id", "record_id", "dispatch_id", "mode", "case_number",
        "attachment_ids", "new_attachment_ids", "uploader_open_ids", "existing_document_token",
        "existing_report_url", "component_build", "required_skill_version", "agent_contract",
        "baseline_field_name", "field_contract",
    }
    if set(runtime) != allowed_keys:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封字段集不正确", {"fields": sorted(runtime)})
    if runtime.get("type") != EXPECTED_RUNTIME_TYPE:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封版本不正确")
    if (
        runtime.get("operation") != EXPECTED_OPERATION
        or runtime.get("app_token") != EXPECTED_APP_TOKEN
        or runtime.get("table_id") != EXPECTED_TABLE_ID
        or runtime.get("baseline_field_name") != EXPECTED_BASELINE_FIELD
        or runtime.get("field_contract") != EXPECTED_FIELD_CONTRACT
    ):
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封的 Base、操作或字段契约不正确")
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


def validate_runtime_agent_contract(runtime: dict[str, Any]) -> None:
    validate_runtime_recovery_contract(runtime)
    if runtime.get("required_skill_version") != EXPECTED_SKILL_VERSION:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封要求的 Skill 版本不正确")
    if runtime.get("component_build") != EXPECTED_COMPONENT_BUILD:
        raise ReportError("RUNTIME_CONTRACT_MISMATCH", "运行信封中的小组件 build 不正确")
    if runtime.get("agent_contract") != EXPECTED_AGENT_CONTRACT:
        raise ReportError("AGENT_CONTRACT_MISMATCH", "运行信封中的主子智能体契约不正确")


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
    validate_runtime_agent_contract(runtime)
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
        "authorized_uploader_open_ids": sorted(set(runtime.get("uploader_open_ids", []))),
        "contract_version": schema["schema_version"],
        "component_build": scalarish(runtime.get("component_build")),
        "skill_version": scalarish(runtime.get("required_skill_version")),
        "source_corpus_sha256": hashlib.sha256(source_corpus.encode("utf-8")).hexdigest(),
        "download_receipt_sha256": scalarish(manifest.get("artifacts", {}).get("download_receipt_sha256")),
        "download_seal_sha256": scalarish(manifest.get("artifacts", {}).get("download_seal_sha256")),
        "vision_verification": {
            "schema_version": scalarish(vision_evidence.get("schema_version")),
            "expected": scalarish(vision_evidence.get("summary", {}).get("expected")),
            "received": scalarish(vision_evidence.get("summary", {}).get("received")),
            "receipts_sha256": scalarish(vision_evidence.get("artifacts", {}).get("vision_receipts_sha256")),
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
    return {"update_records": {record_id: patch}}, {
        "record_id": record_id, "dispatch_id": dispatch_id, "mode": mode,
        "phase": "staged", "fields": patch,
        "source_snapshot": {
            "case_number": scalarish(runtime.get("case_number")),
            "attachment_ids": sorted(attachment_ids),
            "uploader_open_ids": sorted(runtime.get("uploader_open_ids", [])),
        },
        "report_binding": baseline,
    }


def expectation_belongs_to_runtime(
    expectation: dict[str, Any], runtime: dict[str, Any], phases: set[str],
) -> bool:
    return (
        scalarish(expectation.get("phase")) in phases
        and scalarish(expectation.get("record_id")) == scalarish(runtime.get("record_id"))
        and scalarish(expectation.get("dispatch_id")) == scalarish(runtime.get("dispatch_id"))
        and scalarish(expectation.get("mode")) == scalarish(runtime.get("mode"))
        and isinstance(expectation.get("fields"), dict)
    )


def expectation_matches_record(record_values: dict[str, Any], expectation: dict[str, Any]) -> bool:
    try:
        validate_writeback_values(record_values, expectation)
    except ReportError:
        return False
    return True


def classify_base_state(
    runtime: dict[str, Any], record_values: dict[str, Any] | None,
    staged_expectation: dict[str, Any] | None = None,
    final_expectation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_runtime_recovery_contract(runtime)
    if record_values is None:
        raise ReportError("BASE_READBACK_INVALID", "事务分类缺少当前 Base 读回")
    for state, expectation, phases in (
        ("completed", final_expectation, {"completed"}),
        ("staged", staged_expectation, {"staged"}),
    ):
        if expectation is None:
            continue
        if not expectation_belongs_to_runtime(expectation, runtime, phases):
            raise ReportError("WRITEBACK_EXPECTATION_INVALID", "事务期望不属于当前任务")
        if expectation_matches_record(record_values, expectation):
            return {"status": "valid", "state": state, "dispatch_id": scalarish(runtime.get("dispatch_id"))}
    try:
        validate_dispatch_ownership(runtime, record_values, ("处理中",))
        return {"status": "valid", "state": "processing", "dispatch_id": scalarish(runtime.get("dispatch_id"))}
    except ReportError:
        pass
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    if (
        scalarish(record_values.get("AI处理状态")) == "分析失败"
        and re.fullmatch(rf"任务 {re.escape(dispatch_id)}：失败：[A-Z][A-Z0-9_]{{1,63}}", scalarish(record_values.get("执行日志/失败原因")))
    ):
        return {"status": "valid", "state": "failed", "dispatch_id": dispatch_id}
    raise ReportError("BASE_STATE_CONFLICT", "当前 Base 既不属于处理、阶段或完成状态，禁止补偿覆盖")


def build_failure(
    runtime: dict[str, Any], error_code: str, record_values: dict[str, Any] | None,
    staged_expectation: dict[str, Any] | None = None,
    final_expectation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_runtime_recovery_contract(runtime)
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    mode = scalarish(runtime.get("mode"))
    if (
        not record_id or not dispatch_id or mode not in {"initial", "supplement"}
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", error_code)
    ):
        raise ReportError("INVALID_RUNTIME_INPUT", "无法构建失败回写")
    classification = classify_base_state(runtime, record_values, staged_expectation, final_expectation)
    if classification["state"] == "failed":
        raise ReportError("BASE_STATE_CONFLICT", "当前任务已经进入失败终态，不重复写入")
    patch: dict[str, Any] = {
        "AI处理状态": "分析失败",
        "执行日志/失败原因": f"任务 {dispatch_id}：失败：{error_code}",
    }
    if mode == "initial" and classification["state"] == "processing":
        if record_values is None:
            raise ReportError("BASE_READBACK_INVALID", "初始失败缺少当前 Base 读回")
        if scalarish(record_values.get("AI分析结果")) or scalarish(record_values.get("材料处理基线")):
            raise ReportError("BASE_STATE_CONFLICT", "初始处理状态已经出现报告绑定，禁止清空未知写入")
        patch["AI分析结果"] = None
        patch["材料处理基线"] = None
    return {"update_records": {record_id: patch}}, {
        "record_id": record_id, "dispatch_id": dispatch_id, "mode": mode,
        "phase": "failed", "fields": patch, "from_state": classification["state"],
    }


def validate_report_binding_evidence(
    runtime: dict[str, Any], binding: Any, report_readback: Path, expected_report: Path,
    permissions_dir: Path, template: str, schema: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "回写期望缺少报告绑定")
    document_token = scalarish(binding.get("document_token"))
    report_url = scalarish(binding.get("report_url"))
    expected_revision = binding.get("document_revision_id")
    expected_hash = scalarish(binding.get("report_content_sha256"))
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", document_token)
        or report_url != f"https://aixuexi.feishu.cn/docx/{document_token}"
        or not isinstance(expected_revision, int) or expected_revision < 0
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
    ):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "阶段回写期望的报告绑定不合法")
    content, revision_id = extract_document_content(report_readback, document_token)
    if revision_id != expected_revision or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash:
        raise ReportError("REPORT_STATE_INVALID", "远程报告修订号或正文已脱离阶段基线")
    try:
        expected_source = expected_report.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("REPORT_UNAVAILABLE", "无法读取本地预期报告", {"reason": str(exc)}) from exc
    validate_report(content, template, schema)
    validate_remote_matches_expected(content, expected_source)
    try:
        permission_root = permissions_dir.resolve(strict=True)
    except OSError as exc:
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "最终权限读回目录不存在", {"reason": str(exc)}) from exc
    if not permission_root.is_dir():
        raise ReportError("DOC_PERMISSION_READBACK_INVALID", "最终权限读回路径不是目录")
    for member_id in runtime.get("uploader_open_ids", []):
        validate_permission(permission_root / f"{member_id}.json", member_id, document_token)
    return binding


def validate_completion_evidence(
    runtime: dict[str, Any], record_values: dict[str, Any] | None,
    staged_expectation: dict[str, Any], report_readback: Path, expected_report: Path,
    permissions_dir: Path, template: str, schema: dict[str, Any],
) -> dict[str, Any]:
    """Prove the staged record still names the same source, report and access set."""

    validate_runtime_agent_contract(runtime)
    validate_dispatch_ownership(runtime, record_values, ("结果已写入，待最终校验",))
    if record_values is None:
        raise ReportError("BASE_READBACK_INVALID", "最终校验缺少当前 Base 读回")
    validate_runtime_record_snapshot(runtime, record_values)
    if not expectation_belongs_to_runtime(staged_expectation, runtime, {"staged"}):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "阶段回写期望不属于当前任务")
    validate_writeback_values(record_values, staged_expectation)
    return validate_report_binding_evidence(
        runtime, staged_expectation.get("report_binding"), report_readback, expected_report,
        permissions_dir, template, schema,
    )


def build_finalize(
    runtime: dict[str, Any], record_values: dict[str, Any] | None,
    staged_expectation: dict[str, Any], report_readback: Path, expected_report: Path,
    permissions_dir: Path, template: str, schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_runtime_agent_contract(runtime)
    binding = validate_completion_evidence(
        runtime, record_values, staged_expectation, report_readback, expected_report, permissions_dir, template, schema,
    )
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    patch = {
        "AI处理状态": "已完成",
        "执行日志/失败原因": f"任务 {dispatch_id}：已完成",
    }
    expected_fields = dict(staged_expectation["fields"])
    expected_fields.update(patch)
    return {"update_records": {record_id: patch}}, {
        "record_id": record_id, "dispatch_id": dispatch_id,
        "mode": scalarish(runtime.get("mode")), "phase": "completed", "fields": expected_fields,
        "source_snapshot": staged_expectation["source_snapshot"],
        "report_binding": binding,
    }


def validate_final_completion(
    runtime: dict[str, Any], record_values: dict[str, Any] | None,
    final_expectation: dict[str, Any], report_readback: Path, expected_report: Path,
    permissions_dir: Path, template: str, schema: dict[str, Any],
) -> dict[str, Any]:
    """Fresh post-commit proof over Base, document content and collaborators."""

    validate_runtime_agent_contract(runtime)
    if record_values is None:
        raise ReportError("BASE_READBACK_INVALID", "完成校验缺少当前 Base 读回")
    if not expectation_belongs_to_runtime(final_expectation, runtime, {"completed"}):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "完成期望不属于当前任务")
    validate_runtime_record_snapshot(runtime, record_values)
    validate_writeback_values(record_values, final_expectation)
    binding = validate_report_binding_evidence(
        runtime, final_expectation.get("report_binding"), report_readback, expected_report,
        permissions_dir, template, schema,
    )
    return {
        "status": "completed", "record_id": scalarish(runtime.get("record_id")),
        "dispatch_id": scalarish(runtime.get("dispatch_id")),
        "processing_status": "已完成", "report_url": scalarish(binding.get("report_url")),
        "error_code": "",
    }


def validate_writeback_values(actual: dict[str, Any], expectation: dict[str, Any]) -> dict[str, Any]:
    expected_record_id = scalarish(expectation.get("record_id"))
    if not re.fullmatch(r"rec[A-Za-z0-9_-]{1,125}", expected_record_id):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "回写期望值缺少合法 record_id")
    expected = expectation.get("fields")
    if not isinstance(expected, dict):
        raise ReportError("WRITEBACK_EXPECTATION_INVALID", "回写期望值缺少 fields")
    mismatches: dict[str, Any] = {}
    for field, value in expected.items():
        if field == "AI分析结果":
            expected_reference = report_field_reference(value)
            actual_reference = report_field_reference(actual.get(field))
            matches = (
                actual_reference == ("", "")
                if expected_reference == ("", "")
                else analysis_result_matches(value, actual.get(field), actual.get("材料处理基线"))
            )
            if not matches:
                mismatches[field] = {
                    "expected": expected_reference,
                    "actual": actual_reference,
                }
            continue
        expected_value = comparable(field, value)
        actual_value = comparable(field, actual.get(field))
        if expected_value != actual_value:
            mismatches[field] = {"expected": expected_value, "actual": actual_value}
    source_snapshot = expectation.get("source_snapshot")
    if isinstance(source_snapshot, dict):
        expected_case_number = scalarish(source_snapshot.get("case_number"))
        expected_attachments = source_snapshot.get("attachment_ids")
        expected_uploaders = source_snapshot.get("uploader_open_ids")
        if (
            not isinstance(expected_attachments, list)
            or not isinstance(expected_uploaders, list)
            or scalarish(actual.get("案件编号")) != expected_case_number
            or record_attachment_ids(actual.get("案件文档")) != sorted(expected_attachments)
            or record_uploader_ids(actual.get("上传人")) != sorted(expected_uploaders)
        ):
            mismatches["$source_snapshot"] = "Base 案件编号、附件或上传人已变更"
    report_binding = expectation.get("report_binding")
    if isinstance(report_binding, dict) and baseline_object(actual.get("材料处理基线")) != report_binding:
        mismatches["$report_binding"] = "材料处理基线已变更"
    if mismatches:
        raise ReportError("BASE_WRITEBACK_VERIFY_FAILED", "Base 同记录回写读回不一致", {"mismatches": mismatches})
    return {"status": "valid", "verified_field_count": len(expected)}


def validate_writeback(readback: Path, expectation: dict[str, Any]) -> dict[str, Any]:
    expected_record_id = scalarish(expectation.get("record_id"))
    actual = record_field_map(readback, expected_record_id)
    return validate_writeback_values(actual, expectation)


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
    evidence.add_argument("--vision-receipts-dir", type=Path, required=True)
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
    verify_candidate = subparsers.add_parser("verify-candidate")
    verify_candidate.add_argument("--input", type=Path, required=True)
    verify_candidate.add_argument("--document-token", required=True)
    verify_candidate.add_argument("--expected-report", type=Path, required=True)
    verify_candidate.add_argument("--expected-revision", type=int, required=True)
    verify_candidate.add_argument("--expected-sha256", required=True)
    document_state = subparsers.add_parser("classify-document-state")
    document_state.add_argument("--input", type=Path, required=True)
    document_state.add_argument("--document-token", required=True)
    document_state.add_argument("--original-report", type=Path, required=True)
    document_state.add_argument("--original-metadata", type=Path, required=True)
    document_state.add_argument("--candidate-report", type=Path, required=True)
    writeback = subparsers.add_parser("build-writeback")
    writeback.add_argument("--runtime", type=Path, required=True)
    writeback.add_argument("--facts", type=Path, required=True)
    writeback.add_argument("--manifest", type=Path, required=True)
    writeback.add_argument("--vision-evidence", type=Path, required=True)
    writeback.add_argument("--vision-tasks", type=Path, required=True)
    writeback.add_argument("--vision-receipts-dir", type=Path, required=True)
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
    writeback.add_argument("--output", type=Path, required=True)
    writeback.add_argument("--expectation", type=Path, required=True)
    failure = subparsers.add_parser("build-failure")
    failure.add_argument("--runtime", type=Path, required=True)
    failure.add_argument("--error-code", required=True)
    failure.add_argument("--record-readback", type=Path, required=True)
    failure.add_argument("--output", type=Path, required=True)
    failure.add_argument("--expectation", type=Path, required=True)
    failure.add_argument("--staged-expectation", type=Path)
    failure.add_argument("--final-expectation", type=Path)
    finalize = subparsers.add_parser("build-finalize")
    finalize.add_argument("--runtime", type=Path, required=True)
    finalize.add_argument("--record-readback", type=Path, required=True)
    finalize.add_argument("--staged-expectation", type=Path, required=True)
    finalize.add_argument("--report-readback", type=Path, required=True)
    finalize.add_argument("--expected-report", type=Path, required=True)
    finalize.add_argument("--permissions-dir", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--expectation", type=Path, required=True)
    verify_writeback = subparsers.add_parser("validate-writeback")
    verify_writeback.add_argument("--input", type=Path, required=True)
    verify_writeback.add_argument("--expectation", type=Path, required=True)
    base_state = subparsers.add_parser("classify-base-state")
    base_state.add_argument("--runtime", type=Path, required=True)
    base_state.add_argument("--record-readback", type=Path, required=True)
    base_state.add_argument("--staged-expectation", type=Path)
    base_state.add_argument("--final-expectation", type=Path)
    completion = subparsers.add_parser("validate-completion")
    completion.add_argument("--runtime", type=Path, required=True)
    completion.add_argument("--record-readback", type=Path, required=True)
    completion.add_argument("--final-expectation", type=Path, required=True)
    completion.add_argument("--report-readback", type=Path, required=True)
    completion.add_argument("--expected-report", type=Path, required=True)
    completion.add_argument("--permissions-dir", type=Path, required=True)
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
                sha256_file(args.audio_tasks), args.vision_receipts_dir,
                args.audio_receipts_dir, args.audio_transcripts_dir,
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
            validate_runtime_agent_contract(runtime)
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
        elif args.command == "verify-candidate":
            output = verify_candidate_report(
                args.input, args.document_token.strip(), args.expected_report,
                args.expected_revision, args.expected_sha256.strip(),
            )
        elif args.command == "classify-document-state":
            output = classify_document_state(
                args.input, args.document_token.strip(), args.original_report,
                args.original_metadata, args.candidate_report,
            )
        elif args.command == "build-writeback":
            try:
                corpus = args.source_corpus.read_text(encoding="utf-8")
                vision_corpus = args.vision_corpus.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("SOURCE_CORPUS_UNAVAILABLE", "无法读取最终材料语料", {"reason": str(exc)}) from exc
            runtime = read_json(args.runtime)
            validate_runtime_agent_contract(runtime)
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
                args.vision_receipts_dir, args.audio_receipts_dir, args.audio_transcripts_dir,
            )
            document_token = args.document_token.strip()
            remote_content, document_revision_id = extract_document_content(args.report_readback, document_token)
            validate_report(remote_content, template, schema, facts)
            try:
                expected_report = args.expected_report.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("REPORT_UNAVAILABLE", "无法读取本地预期报告", {"reason": str(exc)}) from exc
            validate_remote_matches_expected(remote_content, expected_report)
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
            staged = read_json(args.staged_expectation) if args.staged_expectation else None
            final = read_json(args.final_expectation) if args.final_expectation else None
            update, expectation = build_failure(
                runtime, args.error_code.strip(),
                record_field_map(args.record_readback, scalarish(runtime.get("record_id"))), staged, final,
            )
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "build-finalize":
            runtime = read_json(args.runtime)
            update, expectation = build_finalize(
                runtime, record_field_map(args.record_readback, scalarish(runtime.get("record_id"))),
                read_json(args.staged_expectation), args.report_readback, args.expected_report,
                args.permissions_dir, template, schema,
            )
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "validate-writeback":
            output = validate_writeback(args.input, read_json(args.expectation))
        elif args.command == "classify-base-state":
            runtime = read_json(args.runtime)
            output = classify_base_state(
                runtime,
                record_field_map(args.record_readback, scalarish(runtime.get("record_id"))),
                read_json(args.staged_expectation) if args.staged_expectation else None,
                read_json(args.final_expectation) if args.final_expectation else None,
            )
        elif args.command == "validate-completion":
            runtime = read_json(args.runtime)
            output = validate_final_completion(
                runtime,
                record_field_map(args.record_readback, scalarish(runtime.get("record_id"))),
                read_json(args.final_expectation), args.report_readback, args.expected_report,
                args.permissions_dir, template, schema,
            )
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
