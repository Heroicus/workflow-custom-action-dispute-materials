#!/usr/bin/env python3
"""Build, render and verify one source-backed dispute-material report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
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
EXACT_SOURCE_SCALARS = {
    "case_type", "cause", "tribunal", "case_docket", "our_position", "our_role",
    "our_legal_entity", "our_name", "our_credit_code", "our_legal_representative",
    "opponent_name", "opponent_id", "opponent_role", "judge", "clerk", "judgment_number",
}
NON_EVIDENTIARY_ROWS = {"evidence_rows", "completeness_rows", "quality_rows"}
VISION_PACK_SCHEMA = "vision-evidence-pack/v1"


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
        raise ReportError("VISION_TASKS_UNAVAILABLE", f"无法读取视觉任务清单：{path}", {"reason": str(exc)}) from exc
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
            "SCHEMA_INVALID", "模板行标记与渲染结构不一致",
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


def fact_values(facts: dict[str, Any], schema: dict[str, Any], evidentiary_only: bool = False) -> Iterable[str]:
    scalars = facts.get("scalars", {})
    if isinstance(scalars, dict):
        for name, value in scalars.items():
            if name not in {"case_number", "document_title"}:
                yield scalar_text(value, f"scalars.{name}")
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
                    yield scalar_text(value, f"rows.{marker}[{index}].{column}")


def numeric_literals(value: str) -> set[str]:
    values: set[str] = set()
    for match in NUMERIC_LITERAL_PATTERN.finditer(value):
        literal = match.group(0).replace(",", "")
        if len(literal.replace(".", "")) >= 4:
            values.add(literal)
    return values


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


def evidence_type(name: str) -> str:
    suffix = Path(name.removesuffix(".larkcache")).suffix.lower()
    if suffix == ".pdf":
        return "PDF"
    if suffix in {".doc", ".docx"}:
        return "Word"
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "表格"
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return "图片"
    if suffix in {".m4a", ".mp3", ".wav", ".aac"}:
        return "音频"
    return "文件"


def build_scaffold(case_number: str, manifest: dict[str, Any], template: str, schema: dict[str, Any]) -> dict[str, Any]:
    entries = manifest_entries(manifest)
    scalars = {name: "" for name in sorted(set(SCALAR_PATTERN.findall(template)))}
    scalars["case_number"] = case_number
    rows: dict[str, list[dict[str, str]]] = {name: [] for name in schema["dynamic_rows"]}
    for index, item in enumerate(entries, 1):
        name = scalarish(item.get("file_name"))
        status = scalarish(item.get("status")) or "failed"
        pages = scalarish(item.get("page_count"))
        methods = item.get("methods") if isinstance(item.get("methods"), list) else []
        rows["evidence_rows"].append({
            "index": str(index), "name": name, "type": evidence_type(name), "purpose": "",
            "original_status": "", "pages": pages, "date": "", "existing_note": "",
        })
        availability = {"complete": "是", "partial": "部分", "failed": "否"}.get(status, "否")
        note = {"complete": "解析完成", "partial": "仅部分内容可读取", "failed": "该文件无法解析"}.get(status, "该文件无法解析")
        if methods:
            note += f"（{','.join(str(item) for item in methods)}）"
        rows["completeness_rows"].append({
            "index": str(index), "source_check_item": name, "available": availability, "source_note": note,
        })
    counts = Counter(scalarish(item.get("status")) or "failed" for item in entries)
    rows["quality_rows"] = [{
        "index": "1", "check": "材料读取情况",
        "result": f"共{len(entries)}项，完整{counts['complete']}项，部分{counts['partial']}项，无法解析{counts['failed']}项",
    }]
    return {"scalars": scalars, "rows": rows, "base_fields": {key: "" for key in BASE_FIELD_KEYS}}


def validate_manifest_coverage(rows: dict[str, Any], manifest: dict[str, Any]) -> Counter[str]:
    entries = manifest_entries(manifest)
    expected = [scalarish(item.get("file_name")) for item in entries]
    evidence = [scalarish(row.get("name")) for row in rows["evidence_rows"]]
    completeness = [scalarish(row.get("source_check_item")) for row in rows["completeness_rows"]]
    if Counter(evidence) != Counter(expected) or Counter(completeness) != Counter(expected):
        raise ReportError(
            "MATERIAL_COVERAGE_INCOMPLETE", "证据清单或材料完整性表未覆盖全部材料",
            {
                "expected": len(expected), "evidence_rows": len(evidence), "completeness_rows": len(completeness),
                "missing_evidence": sorted((Counter(expected) - Counter(evidence)).elements())[:10],
                "missing_completeness": sorted((Counter(expected) - Counter(completeness)).elements())[:10],
            },
        )
    if not rows["quality_rows"]:
        raise ReportError("MATERIAL_COVERAGE_INCOMPLETE", "AI 输出质量自检表缺少材料读取情况")
    return Counter(scalarish(item.get("status")) or "failed" for item in entries)


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
            or item.get("status") not in {"complete", "partial"}
            or not isinstance(producer, dict)
            or producer.get("agent_name") != "纠纷材料视觉核验员"
            or producer.get("model") != "Doubao-Seed-2.1-turbo"
        ):
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务身份、哈希或状态不正确", {"task_id": task_id})
        if task_id in task_map:
            raise ReportError("VISION_EVIDENCE_INVALID", "视觉证据任务 ID 重复", {"task_id": task_id})
        fields = item.get("critical_fields")
        regions = item.get("uncertain_regions")
        if (
            not isinstance(fields, list)
            or any(not isinstance(field, dict) or field.get("status") != "clear" for field in fields)
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
    return {"vision_expected": expected, "vision_received": received, "vision_ocr_disagreements": summary.get("ocr_disagreements", 0)}


def require_source_sections(scalars: dict[str, Any], rows: dict[str, Any], corpus_key: str) -> None:
    missing: list[str] = []

    def has(*phrases: str) -> bool:
        return any(source_key(phrase) in corpus_key for phrase in phrases)

    def scalar_required(name: str) -> None:
        if not scalar_text(scalars.get(name), f"scalars.{name}"):
            missing.append(f"scalars.{name}")

    def row_required(name: str) -> None:
        if not rows.get(name):
            missing.append(f"rows.{name}")

    if has("原告", "被告", "申请人", "被申请人") and has("起诉状", "仲裁申请书", "裁决书", "判决书"):
        scalar_required("our_name")
        scalar_required("opponent_name")
    if has("仲裁请求", "诉讼请求", "请求为"):
        row_required("request_rows")
    if has("双方对下列要素事实存在争议", "本院认为", "本委认定及理由"):
        row_required("focus_rows")
        row_required("legal_basis_rows")
    if has("开庭时间", "公开开庭审理"):
        row_required("procedure_rows")
    if has("现裁决如下", "判决如下"):
        scalar_required("judgment_number")
        scalar_required("case_status")
        if not scalar_text(scalars.get("judgment_summary"), "scalars.judgment_summary") and not scalar_text(scalars.get("judgment_orders"), "scalars.judgment_orders"):
            missing.append("scalars.judgment_summary|judgment_orders")
        scalar_required("outcome")
    if missing:
        raise ReportError("FACT_COVERAGE_INCOMPLETE", "材料明确包含的核心内容未进入报告", {"missing": missing})


def validate_fact_evidence(
    facts: dict[str, Any], template: str, schema: dict[str, Any], source_corpus: str,
    manifest: dict[str, Any], vision_evidence: dict[str, Any], vision_tasks: dict[str, Any],
    vision_tasks_sha256: str,
) -> dict[str, Any]:
    scalars, rows, base_fields = validate_fact_shape(facts, template, schema)
    counts = validate_manifest_coverage(rows, manifest)
    vision_summary = validate_vision_evidence(vision_evidence, source_corpus, vision_tasks, vision_tasks_sha256)
    evidence_corpus = "\n".join(
        line for line in source_corpus.splitlines()
        if not line.startswith(("=== ", "--- "))
    )
    if len(source_key(evidence_corpus)) < 20:
        raise ReportError("SOURCE_CORPUS_EMPTY", "附件没有产生可用正文或 OCR 文本")
    corpus_numeric = re.sub(r"[\s,，]", "", evidence_corpus)
    literals = set().union(*(numeric_literals(value) for value in fact_values(facts, schema, evidentiary_only=True)))
    unsupported_numbers = sorted(literal for literal in literals if not re.search(rf"(?<!\d){re.escape(literal)}(?!\d)", corpus_numeric))
    if unsupported_numbers:
        raise ReportError(
            "FACT_NUMERIC_UNSUPPORTED", "结构化事实含有未在材料文本或 OCR 文本中出现的数字",
            {"values": unsupported_numbers[:20], "total": len(unsupported_numbers)},
        )
    corpus_key = source_key(evidence_corpus)
    unsupported_scalars: list[str] = []
    for name in sorted(EXACT_SOURCE_SCALARS):
        value = scalar_text(scalars.get(name), f"scalars.{name}")
        if value and source_key(value) not in corpus_key:
            unsupported_scalars.append(name)
    if unsupported_scalars:
        raise ReportError("FACT_LITERAL_UNSUPPORTED", "姓名、机构、案号或身份字段无原文支持", {"fields": unsupported_scalars})
    require_source_sections(scalars, rows, corpus_key)
    case_type = scalar_text(scalars.get("case_type"), "scalars.case_type")
    case_status = scalar_text(scalars.get("case_status"), "scalars.case_status")
    base_case_type = scalar_text(base_fields.get("case_type"), "base_fields.case_type")
    base_case_status = scalar_text(base_fields.get("case_status"), "base_fields.case_status")
    if case_type not in {"", "诉讼", "仲裁"} or base_case_type not in {"", "诉讼", "仲裁"}:
        raise ReportError("BASE_FIELD_INVALID", "案件类型只能为诉讼或仲裁")
    allowed_status = {"", "待立案", "审理中", "已结案", "已归档"}
    if case_status not in allowed_status or base_case_status not in allowed_status:
        raise ReportError("BASE_FIELD_INVALID", "案件状态不是 Base 现有选项")
    if case_type and base_case_type != case_type:
        raise ReportError("BASE_FIELD_MISMATCH", "报告案件类型与 Base 回写值不一致")
    if case_status and base_case_status != case_status:
        raise ReportError("BASE_FIELD_MISMATCH", "报告案件状态与 Base 回写值不一致")
    if (scalar_text(scalars.get("our_name"), "scalars.our_name") or scalar_text(scalars.get("opponent_name"), "scalars.opponent_name")) and not scalar_text(base_fields.get("case_name"), "base_fields.case_name"):
        raise ReportError("BASE_FIELD_MISSING", "已识别当事人但未生成案件名称")
    return {
        "status": "valid", "numeric_literal_count": len(literals), "material_count": sum(counts.values()),
        "complete_materials": counts["complete"], "partial_materials": counts["partial"], "failed_materials": counts["failed"],
        **vision_summary,
    }


def validate_report(source: str, template: str, schema: dict[str, Any], facts: dict[str, Any] | None = None) -> dict[str, Any]:
    if UNRESOLVED_PATTERN.search(source) or ROW_PATTERN.search(source):
        raise ReportError("TEMPLATE_MARKER_REMAINS", "报告仍有模板标记")
    actual = parse_fragment(source)
    template_root = parse_fragment(template)
    compare_structure(actual, template_root, template, schema)
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


def extract_document_content(path: Path) -> str:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError("REPORT_UNAVAILABLE", f"无法读取报告：{path}", {"reason": str(exc)}) from exc
    if not source.lstrip().startswith(("{", "[")):
        return source
    for root in json_values(source):
        for value in walk_values(root):
            if isinstance(value, dict):
                content = value.get("content")
                if isinstance(content, str) and "<title" in content and "<table" in content:
                    return content
    raise ReportError("REPORT_UNAVAILABLE", "JSON 中没有文档 content")


def record_field_map(path: Path) -> dict[str, Any]:
    for root in read_response_values(path):
        for value in walk_values(root):
            if not isinstance(value, dict):
                continue
            fields = value.get("fields")
            data = value.get("data")
            if isinstance(fields, list) and fields and all(isinstance(item, str) for item in fields) and isinstance(data, list) and data:
                row = data[0]
                if isinstance(row, list) and len(row) == len(fields):
                    return dict(zip(fields, row))
    raise ReportError("BASE_READBACK_INVALID", "Base 读回中没有完整字段行")


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
    contract = runtime.get("model_contract")
    expected = {
        "main_model": "Deepseek-V4-Pro",
        "vision_agent_name": "纠纷材料视觉核验员",
        "vision_model": "Doubao-Seed-2.1-turbo",
        "vision_result_schema": "vision-evidence/v1",
        "write_policy": "main_agent_only",
    }
    if not isinstance(contract, dict) or any(contract.get(key) != value for key, value in expected.items()):
        raise ReportError("MODEL_CONTRACT_MISMATCH", "运行信封中的主子模型契约不正确")


def build_writeback(
    runtime: dict[str, Any], facts: dict[str, Any], manifest: dict[str, Any], document_token: str,
    report_url: str, schema: dict[str, Any], record_values: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_runtime_model_contract(runtime)
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    mode = scalarish(runtime.get("mode"))
    if not record_id or not dispatch_id or mode not in {"initial", "supplement"}:
        raise ReportError("INVALID_RUNTIME_INPUT", "运行信封缺少 record_id、dispatch_id 或 mode")
    attachment_ids = runtime.get("attachment_ids")
    if not isinstance(attachment_ids, list) or not all(isinstance(item, str) and item for item in attachment_ids):
        raise ReportError("INVALID_RUNTIME_INPUT", "运行信封 attachment_ids 不合法")
    top_items = manifest.get("items")
    expected_downloads = attachment_ids
    if not isinstance(expected_downloads, list) or not isinstance(top_items, list) or len(top_items) != len(expected_downloads):
        raise ReportError("MATERIAL_DOWNLOAD_INCOMPLETE", "下载的顶层附件数与运行信封不一致")
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
        "document_token": document_token,
        "processed_attachment_ids": sorted(set(attachment_ids)),
        "contract_version": schema["schema_version"],
    }
    patch.update({
        "AI分析结果": f"[{title}]({report_url})",
        "材料处理基线": json.dumps(baseline, ensure_ascii=False, separators=(",", ":")),
        "AI处理状态": "已完成",
        "执行日志/失败原因": f"任务 {dispatch_id}：已完成",
    })
    return {"record_id_list": [record_id], "patch": patch}, {"record_id": record_id, "fields": patch}


def build_failure(runtime: dict[str, Any], error_code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    record_id = scalarish(runtime.get("record_id"))
    dispatch_id = scalarish(runtime.get("dispatch_id"))
    mode = scalarish(runtime.get("mode"))
    if not record_id or not dispatch_id or mode not in {"initial", "supplement"} or not error_code:
        raise ReportError("INVALID_RUNTIME_INPUT", "无法构建失败回写")
    patch: dict[str, Any] = {
        "AI处理状态": "分析失败",
        "执行日志/失败原因": f"任务 {dispatch_id}：失败：{error_code}",
    }
    if mode == "initial":
        patch["AI分析结果"] = None
        patch["材料处理基线"] = None
    return {"record_id_list": [record_id], "patch": patch}, {"record_id": record_id, "fields": patch}


def validate_writeback(readback: Path, expectation: dict[str, Any]) -> dict[str, Any]:
    actual = record_field_map(readback)
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


def validate_permission(path: Path, member_id: str) -> dict[str, Any]:
    roots = read_response_values(path)
    strings: list[str] = []
    for root in roots:
        for value in walk_values(root):
            if isinstance(value, (str, int, float, bool)):
                strings.append(str(value))
    if member_id not in strings or "full_access" not in strings:
        raise ReportError(
            "DOC_PERMISSION_GRANT_FAILED", "权限添加响应未返回目标上传人和 full_access",
            {"member_id_found": member_id in strings, "full_access_found": "full_access" in strings},
        )
    return {"status": "valid", "member_id": member_id, "permission": "full_access"}


def atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build, render and verify a dispute report.")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scaffold = subparsers.add_parser("scaffold")
    scaffold.add_argument("--case-number", required=True)
    scaffold.add_argument("--manifest", type=Path, required=True)
    scaffold.add_argument("--output", type=Path, required=True)
    evidence = subparsers.add_parser("validate-facts")
    evidence.add_argument("--facts", type=Path, required=True)
    evidence.add_argument("--source-corpus", type=Path, required=True)
    evidence.add_argument("--manifest", type=Path, required=True)
    evidence.add_argument("--vision-evidence", type=Path, required=True)
    evidence.add_argument("--vision-tasks", type=Path, required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--facts", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--facts", type=Path)
    writeback = subparsers.add_parser("build-writeback")
    writeback.add_argument("--runtime", type=Path, required=True)
    writeback.add_argument("--facts", type=Path, required=True)
    writeback.add_argument("--manifest", type=Path, required=True)
    writeback.add_argument("--vision-evidence", type=Path, required=True)
    writeback.add_argument("--vision-tasks", type=Path, required=True)
    writeback.add_argument("--source-corpus", type=Path, required=True)
    writeback.add_argument("--document-token", required=True)
    writeback.add_argument("--report-url", required=True)
    writeback.add_argument("--record-readback", type=Path)
    writeback.add_argument("--output", type=Path, required=True)
    writeback.add_argument("--expectation", type=Path, required=True)
    failure = subparsers.add_parser("build-failure")
    failure.add_argument("--runtime", type=Path, required=True)
    failure.add_argument("--error-code", required=True)
    failure.add_argument("--output", type=Path, required=True)
    failure.add_argument("--expectation", type=Path, required=True)
    verify_writeback = subparsers.add_parser("validate-writeback")
    verify_writeback.add_argument("--input", type=Path, required=True)
    verify_writeback.add_argument("--expectation", type=Path, required=True)
    permission = subparsers.add_parser("validate-permission")
    permission.add_argument("--input", type=Path, required=True)
    permission.add_argument("--member-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        template, schema = load_contract(args.template, args.schema)
        if args.command == "scaffold":
            manifest = read_json(args.manifest)
            result = build_scaffold(args.case_number.strip(), manifest, template, schema)
            atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2))
            output: dict[str, Any] = {"status": "created", "output": str(args.output.resolve()), "material_count": len(manifest_entries(manifest))}
        elif args.command == "validate-facts":
            try:
                corpus = args.source_corpus.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("SOURCE_CORPUS_UNAVAILABLE", "无法读取材料文本或 OCR 汇总", {"reason": str(exc)}) from exc
            output = validate_fact_evidence(
                read_json(args.facts), template, schema, corpus,
                read_json(args.manifest), read_json(args.vision_evidence), read_json(args.vision_tasks),
                sha256_file(args.vision_tasks),
            )
        elif args.command == "render":
            facts = read_json(args.facts)
            rendered = render_report(facts, template, schema)
            atomic_write(args.output, rendered)
            output = validate_report(rendered, template, schema, facts)
            output.update({"output": str(args.output.resolve()), "bytes": len(rendered.encode("utf-8"))})
        elif args.command == "validate":
            facts = read_json(args.facts) if args.facts else None
            output = validate_report(extract_document_content(args.input), template, schema, facts)
            output["input"] = str(args.input.resolve())
        elif args.command == "build-writeback":
            try:
                corpus = args.source_corpus.read_text(encoding="utf-8")
            except OSError as exc:
                raise ReportError("SOURCE_CORPUS_UNAVAILABLE", "无法读取最终材料语料", {"reason": str(exc)}) from exc
            validate_vision_evidence(
                read_json(args.vision_evidence), corpus, read_json(args.vision_tasks),
                sha256_file(args.vision_tasks),
            )
            record_values = record_field_map(args.record_readback) if args.record_readback else None
            update, expectation = build_writeback(
                read_json(args.runtime), read_json(args.facts), read_json(args.manifest),
                args.document_token.strip(), args.report_url.strip(), schema, record_values,
            )
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "build-failure":
            update, expectation = build_failure(read_json(args.runtime), args.error_code.strip())
            atomic_write(args.output, json.dumps(update, ensure_ascii=False, separators=(",", ":")))
            atomic_write(args.expectation, json.dumps(expectation, ensure_ascii=False, indent=2))
            output = {"status": "created", "output": str(args.output.resolve()), "expectation": str(args.expectation.resolve())}
        elif args.command == "validate-writeback":
            output = validate_writeback(args.input, read_json(args.expectation))
        else:
            output = validate_permission(args.input, args.member_id.strip())
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ReportError as exc:
        print(json.dumps({"status": "invalid", "error_code": exc.code, "message": str(exc), "details": exc.details}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
