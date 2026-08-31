#!/usr/bin/env python3
"""Validate read-only vision-worker evidence and build one verified corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


TASK_SCHEMA = "vision-task/v1"
RESULT_SCHEMA = "vision-evidence/v2"
PACK_SCHEMA = "vision-evidence-pack/v2"
EXPECTED_AGENT = "纠纷材料视觉核验员"
EXPECTED_MODEL = "Doubao-Seed-2.1-turbo"
ALLOWED_STATUS = {"complete", "partial", "failed"}
RESULT_KEYS = {
    "schema_version", "task_id", "source_sha256", "image_sha256", "producer",
    "status", "verbatim_text", "uncertain_regions",
}
TASK_KEYS = {
    "schema_version", "task_id", "source_file", "source_sha256", "unit", "page",
    "reason", "image_path", "image_sha256", "ocr_text", "ocr_mean_confidence",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^vis_[0-9a-f]{20}$")


class VisionError(Exception):
    """A stable vision-evidence contract failure."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VisionError("VISION_RESULT_UNAVAILABLE", f"无法读取视觉结果：{path.name}", {"reason": str(exc)}) from exc
    if source.startswith("```"):
        source = re.sub(r"^```(?:json)?\s*|\s*```$", "", source, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise VisionError("VISION_RESULT_INVALID", f"视觉结果不是 JSON：{path.name}", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise VisionError("VISION_RESULT_INVALID", f"视觉结果根节点必须是对象：{path.name}")
    return value


def required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisionError("VISION_RESULT_INVALID", f"{label} 必须是非空字符串")
    return value.strip()


def require_keys(
    value: dict[str, Any], required: set[str], allowed: set[str], label: str,
    code: str = "VISION_RESULT_INVALID",
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise VisionError(
            code, f"{label} 字段不符合契约",
            {"missing": missing, "unknown": unknown},
        )


def load_tasks(path: Path) -> list[dict[str, Any]]:
    root = read_object(path)
    require_keys(
        root, {"schema_version", "policy", "tasks", "summary"},
        {"schema_version", "policy", "tasks", "summary"}, "视觉任务清单", "VISION_TASKS_INVALID",
    )
    if root.get("schema_version") != TASK_SCHEMA or not isinstance(root.get("tasks"), list):
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单版本或结构不正确")
    policy = root.get("policy")
    if not isinstance(policy, dict) or policy != {
        "worker": EXPECTED_AGENT,
        "required_model": EXPECTED_MODEL,
        "write_scope": "read_only_evidence",
        "all_visual_units_required": True,
    }:
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单工作者契约不正确")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(root["tasks"]):
        if not isinstance(item, dict):
            raise VisionError("VISION_TASKS_INVALID", f"tasks[{index}] 必须是对象")
        require_keys(item, TASK_KEYS, TASK_KEYS, f"tasks[{index}]", "VISION_TASKS_INVALID")
        if item.get("schema_version") != TASK_SCHEMA:
            raise VisionError("VISION_TASKS_INVALID", f"tasks[{index}] 版本不正确")
        task_id = required_text(item.get("task_id"), f"tasks[{index}].task_id")
        if not TASK_ID.fullmatch(task_id):
            raise VisionError("VISION_TASKS_INVALID", "视觉任务 ID 格式不正确", {"task_id": task_id})
        if task_id in seen:
            raise VisionError("VISION_TASKS_INVALID", "视觉任务 ID 重复", {"task_id": task_id})
        seen.add(task_id)
        source_hash = required_text(item.get("source_sha256"), f"tasks[{index}].source_sha256")
        if not HEX64.fullmatch(source_hash):
            raise VisionError("VISION_TASKS_INVALID", "视觉任务来源哈希格式不正确", {"task_id": task_id})
        image = Path(required_text(item.get("image_path"), f"tasks[{index}].image_path"))
        if not image.is_file():
            raise VisionError("VISION_IMAGE_UNAVAILABLE", "视觉任务图片不存在", {"task_id": task_id, "image": str(image)})
        expected_hash = required_text(item.get("image_sha256"), f"tasks[{index}].image_sha256")
        if not HEX64.fullmatch(expected_hash):
            raise VisionError("VISION_TASKS_INVALID", "视觉任务图片哈希格式不正确", {"task_id": task_id})
        actual_hash = sha256_file(image)
        if actual_hash != expected_hash:
            raise VisionError("VISION_IMAGE_CHANGED", "视觉任务图片哈希不一致", {"task_id": task_id})
        tasks.append(item)
    summary = root.get("summary")
    if not isinstance(summary, dict) or summary != {"total": len(tasks), "pending": len(tasks)}:
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单统计与任务不一致")
    return tasks


def validate_result(task: dict[str, Any], path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = read_object(path)
    task_id = str(task["task_id"])
    require_keys(result, RESULT_KEYS, RESULT_KEYS, task_id)
    if result.get("schema_version") != RESULT_SCHEMA or result.get("task_id") != task_id:
        raise VisionError("VISION_RESULT_INVALID", "视觉结果版本或 task_id 不匹配", {"task_id": task_id})
    if result.get("source_sha256") != task.get("source_sha256") or result.get("image_sha256") != task.get("image_sha256"):
        raise VisionError("VISION_RESULT_INVALID", "视觉结果来源哈希不匹配", {"task_id": task_id})
    producer = result.get("producer")
    if not isinstance(producer, dict):
        raise VisionError("VISION_RESULT_INVALID", "视觉结果 producer 必须是对象", {"task_id": task_id})
    require_keys(producer, {"agent_name", "model"}, {"agent_name", "model"}, f"{task_id}.producer")
    if producer.get("agent_name") != EXPECTED_AGENT or producer.get("model") != EXPECTED_MODEL:
        raise VisionError(
            "VISION_WORKER_MISMATCH", "视觉结果不是规定的豆包只读子智能体产物",
            {"task_id": task_id, "producer": producer},
        )
    status = str(result.get("status") or "")
    if status not in ALLOWED_STATUS:
        raise VisionError("VISION_RESULT_INVALID", "视觉任务 status 非法", {"task_id": task_id, "status": status})
    vision_text = str(result.get("verbatim_text") or "").strip()
    if status != "failed" and not vision_text:
        raise VisionError("VISION_RESULT_INVALID", "视觉结果缺少逐字转录", {"task_id": task_id})
    raw_uncertain = result.get("uncertain_regions")
    if not isinstance(raw_uncertain, list) or not all(isinstance(item, dict) for item in raw_uncertain):
        raise VisionError("VISION_RESULT_INVALID", "uncertain_regions 必须是对象数组", {"task_id": task_id})
    uncertain_regions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_uncertain):
        require_keys(item, {"description", "critical"}, {"description", "critical", "source_ref"}, f"{task_id}.uncertain_regions[{index}]")
        description = required_text(item.get("description"), f"{task_id}.uncertain_regions[{index}].description")
        if not isinstance(item.get("critical"), bool):
            raise VisionError("VISION_RESULT_INVALID", "uncertain_regions.critical 必须是布尔值", {"task_id": task_id})
        region = {"description": description, "critical": item["critical"]}
        if "source_ref" in item:
            region["source_ref"] = required_text(item.get("source_ref"), f"{task_id}.uncertain_regions[{index}].source_ref")
        uncertain_regions.append(region)
    unresolved: list[dict[str, Any]] = []
    if status != "complete":
        unresolved.append({"field_type": "task", "visible_text": f"视觉任务状态为 {status}", "status": "unclear"})
    unresolved.extend(
        {"field_type": "uncertain_region", "visible_text": str(item.get("description") or ""), "status": "unclear"}
        for item in uncertain_regions if item["critical"] is True
    )
    normalized_result = {
        "task_id": task_id,
        "source_file": str(task.get("source_file") or ""),
        "source_sha256": task.get("source_sha256"),
        "image_sha256": task.get("image_sha256"),
        "unit": task.get("unit"),
        "page": task.get("page"),
        "status": status,
        "producer": producer,
        "verbatim_text": vision_text,
        "uncertain_regions": uncertain_regions,
        "ocr_mean_confidence": task.get("ocr_mean_confidence"),
    }
    return normalized_result, unresolved


def reconcile(tasks_path: Path, results_dir: Path, corpus_path: Path, output_corpus: Path, evidence_path: Path) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    try:
        source_corpus = corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VisionError("SOURCE_CORPUS_UNAVAILABLE", "无法读取初始材料语料", {"reason": str(exc)}) from exc
    results: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    expected_result_names = {f"{task['task_id']}.json" for task in tasks}
    extra_results = sorted(path.name for path in results_dir.glob("*.json") if path.name not in expected_result_names)
    if extra_results:
        raise VisionError("VISION_RESULT_INVALID", "视觉结果目录含有未知任务结果", {"files": extra_results[:20]})
    for task in tasks:
        task_id = str(task["task_id"])
        path = results_dir / f"{task_id}.json"
        if not path.is_file():
            unresolved.append({"task_id": task_id, "field_type": "task", "visible_text": "缺少视觉结果", "status": "unclear"})
            continue
        result, result_unresolved = validate_result(task, path)
        results.append(result)
        unresolved.extend({"task_id": task_id, **item} for item in result_unresolved)
    additions = [
        f"=== 视觉核验[sha256={item['source_sha256']}]：{item['source_file']} {item['unit']} ===\n{item['verbatim_text']}"
        for item in results if item["verbatim_text"]
    ]
    verified = source_corpus.rstrip() + "\n\n" + "\n\n".join(additions) + "\n" if additions else source_corpus
    evidence = {
        "schema_version": PACK_SCHEMA,
        "policy": {
            "main_writer": "Deepseek-V4-Pro",
            "vision_worker": EXPECTED_MODEL,
            "single_writer": True,
            "vision_worker_write_scope": "read_only_evidence",
        },
        "tasks": results,
        "unresolved": unresolved,
        "artifacts": {
            "vision_tasks_sha256": sha256_file(tasks_path),
            "source_corpus_sha256": sha256_text(source_corpus),
            "verified_corpus_sha256": sha256_text(verified) if not unresolved else None,
        },
        "summary": {
            "expected": len(tasks),
            "received": len(results),
            "complete": sum(item["status"] == "complete" for item in results),
            "partial": sum(item["status"] == "partial" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "unresolved": len(unresolved),
        },
    }
    atomic_write(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2))
    if unresolved:
        raise VisionError("VISION_EVIDENCE_UNRESOLVED", "视觉子智能体仍有未核清的关键内容", {"items": unresolved[:20], "total": len(unresolved)})
    atomic_write(output_corpus, verified)
    return {"status": "valid", **evidence["summary"], "output_corpus": str(output_corpus.resolve()), "evidence": str(evidence_path.resolve())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Doubao vision-worker evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("reconcile")
    validate.add_argument("--tasks", type=Path, required=True)
    validate.add_argument("--results-dir", type=Path, required=True)
    validate.add_argument("--source-corpus", type=Path, required=True)
    validate.add_argument("--output-corpus", type=Path, required=True)
    validate.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = reconcile(args.tasks, args.results_dir, args.source_corpus, args.output_corpus, args.evidence)
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except VisionError as exc:
        print(json.dumps({"status": "invalid", "error_code": exc.code, "message": str(exc), "details": exc.details}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
