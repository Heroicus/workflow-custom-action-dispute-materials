#!/usr/bin/env python3
"""Prepare, accept and reconcile evidence from the named visual agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from evidence_contract import (
    MAIN_AGENT_NAME,
    VISION_AGENT_ID,
    VISION_AGENT_NAME,
    VISION_RESULT_SCHEMA,
    VISION_TASK_SCHEMA,
    vision_agent_prompt,
)


PACK_SCHEMA = "vision-evidence-pack/v4"
INVOCATION_SCHEMA = "vision-native-invocation-batch/v1"
RECEIPT_SCHEMA = "vision-native-agent-receipt/v1"
SOURCE_SCHEMA = "vision-base-source/v1"
TRANSPORT = "native_agent_tool"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
RESULT_KEYS = {
    "schema_version", "task_id", "source_sha256", "image_sha256", "producer",
    "status", "verbatim_text", "uncertain_regions",
}
TASK_KEYS = {
    "schema_version", "task_id", "source_file", "source_sha256", "unit", "page",
    "reason", "image_file", "image_sha256", "image_size", "image_transform",
    "ocr_text", "ocr_mean_confidence",
}
SOURCE_KEYS = {
    "schema_version", "app_token", "table_id", "record_id", "attachment_field_id",
    "attachment_field_name", "attachment_id", "attachment_name", "attachment_size",
    "attachment_sha256", "source_locator",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^vis_[0-9a-f]{20}$")
REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
BASE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
TABLE_ID = re.compile(r"^tbl[A-Za-z0-9_-]{1,125}$")
RECORD_ID = re.compile(r"^rec[A-Za-z0-9_-]{1,125}$")


class VisionError(Exception):
    """Stable visual-evidence contract failure."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VisionError("VISION_FILE_UNAVAILABLE", f"无法读取视觉工件：{path.name}", {"reason": str(exc)}) from exc
    return digest.hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def read_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VisionError(code, f"无法读取 JSON：{path.name}", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise VisionError(code, f"JSON 根节点必须是对象：{path.name}")
    return value


def required_text(value: Any, label: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisionError(code, f"{label} 必须是非空字符串")
    return value.strip()


def require_keys(
    value: dict[str, Any], required: set[str], allowed: set[str], label: str, code: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise VisionError(code, f"{label} 字段不符合契约", {"missing": missing, "unknown": unknown})


def owned_file(root: Path, name: str, task_id: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != name:
        raise VisionError("VISION_TASKS_INVALID", "视觉工件名必须是单层相对路径", {"task_id": task_id})
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise VisionError("VISION_TASKS_INVALID", "视觉工件越出任务目录", {"task_id": task_id}) from exc
    return path


def file_fingerprint(path: Path) -> tuple[int, int, int, int, str]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise VisionError("VISION_FILE_UNAVAILABLE", "视觉工件不存在", {"file": path.name}) from exc
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, sha256_file(path)


def verify_image(task: dict[str, Any], image_root: Path) -> tuple[Path, tuple[int, int, int, int, str]]:
    task_id = str(task["task_id"])
    image = owned_file(image_root, str(task["image_file"]), task_id)
    fingerprint = file_fingerprint(image)
    if fingerprint[2] != task["image_size"] or fingerprint[4] != task["image_sha256"]:
        raise VisionError(
            "VISION_SNAPSHOT_INVALID", "视觉工件与不可变快照不一致",
            {"task_id": task_id, "expected_size": task["image_size"], "actual_size": fingerprint[2]},
        )
    if image.suffix.lower() not in IMAGE_SUFFIXES:
        raise VisionError("VISION_SNAPSHOT_INVALID", "视觉工件格式不正确", {"task_id": task_id})
    return image, fingerprint


def validate_image_transform(task: dict[str, Any]) -> None:
    task_id = str(task["task_id"])
    transform = task.get("image_transform")
    keys = {
        "mode", "source_suffix", "source_size_bytes", "source_pixel_width",
        "source_pixel_height", "output_suffix", "output_pixel_width",
        "output_pixel_height", "jpeg_quality",
    }
    if not isinstance(transform, dict):
        raise VisionError("VISION_TASKS_INVALID", "视觉转换记录必须是对象", {"task_id": task_id})
    require_keys(transform, keys, keys, "视觉转换记录", "VISION_TASKS_INVALID")
    dimensions = (
        transform.get("source_pixel_width"), transform.get("source_pixel_height"),
        transform.get("output_pixel_width"), transform.get("output_pixel_height"),
    )
    if (
        transform.get("mode") not in {"passthrough", "transcoded"}
        or type(transform.get("source_size_bytes")) is not int
        or transform["source_size_bytes"] <= 0
        or not all(type(value) is int and value > 0 for value in dimensions)
        or transform.get("output_suffix") != Path(str(task["image_file"])).suffix.lower()
    ):
        raise VisionError("VISION_TASKS_INVALID", "视觉转换记录内容不正确", {"task_id": task_id})
    quality = transform.get("jpeg_quality")
    if transform["mode"] == "passthrough":
        if quality is not None or transform.get("source_suffix") != transform.get("output_suffix"):
            raise VisionError("VISION_TASKS_INVALID", "视觉原样传递记录不正确", {"task_id": task_id})
    elif type(quality) is not int or not 1 <= quality <= 100 or transform.get("output_suffix") != ".jpg":
        raise VisionError("VISION_TASKS_INVALID", "视觉转码记录不正确", {"task_id": task_id})


def load_tasks(path: Path, image_root: Path) -> list[dict[str, Any]]:
    root = read_object(path, "VISION_TASKS_INVALID")
    require_keys(
        root, {"schema_version", "policy", "tasks", "summary"},
        {"schema_version", "policy", "tasks", "summary"}, "视觉任务清单", "VISION_TASKS_INVALID",
    )
    if root.get("schema_version") != VISION_TASK_SCHEMA or not isinstance(root.get("tasks"), list):
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单版本或结构不正确")
    if root.get("policy") != {
        "worker": VISION_AGENT_NAME,
        "agent_id": VISION_AGENT_ID,
        "write_scope": "read_only_evidence",
        "all_visual_units_required": True,
    }:
        raise VisionError("VISION_TASKS_INVALID", "视觉任务工作者契约不正确")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(root["tasks"]):
        if not isinstance(item, dict):
            raise VisionError("VISION_TASKS_INVALID", f"tasks[{index}] 必须是对象")
        require_keys(item, TASK_KEYS, TASK_KEYS, f"tasks[{index}]", "VISION_TASKS_INVALID")
        task_id = required_text(item.get("task_id"), f"tasks[{index}].task_id", "VISION_TASKS_INVALID")
        source_hash = required_text(item.get("source_sha256"), f"tasks[{index}].source_sha256", "VISION_TASKS_INVALID")
        image_hash = required_text(item.get("image_sha256"), f"tasks[{index}].image_sha256", "VISION_TASKS_INVALID")
        if (
            item.get("schema_version") != VISION_TASK_SCHEMA
            or not TASK_ID.fullmatch(task_id)
            or task_id in seen
            or not HEX64.fullmatch(source_hash)
            or not HEX64.fullmatch(image_hash)
            or type(item.get("image_size")) is not int
            or item["image_size"] <= 0
        ):
            raise VisionError("VISION_TASKS_INVALID", "视觉任务身份、哈希或大小不正确", {"task_id": task_id})
        validate_image_transform(item)
        verify_image(item, image_root)
        seen.add(task_id)
        tasks.append(item)
    if root.get("summary") != {"total": len(tasks), "pending": len(tasks)}:
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单统计与任务不一致")
    return tasks


def load_source_bindings(
    runtime_path: Path, seal_path: Path, tasks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    runtime = read_object(runtime_path, "VISION_SOURCE_INVALID")
    seal = read_object(seal_path, "VISION_SOURCE_INVALID")
    app_token = required_text(runtime.get("app_token"), "runtime.app_token", "VISION_SOURCE_INVALID")
    table_id = required_text(runtime.get("table_id"), "runtime.table_id", "VISION_SOURCE_INVALID")
    record_id = required_text(runtime.get("record_id"), "runtime.record_id", "VISION_SOURCE_INVALID")
    runtime_ids = runtime.get("attachment_ids")
    attachments = seal.get("attachments")
    if (
        not BASE_TOKEN.fullmatch(app_token)
        or not TABLE_ID.fullmatch(table_id)
        or not RECORD_ID.fullmatch(record_id)
        or not isinstance(runtime_ids, list)
        or not all(isinstance(item, str) and item.strip() for item in runtime_ids)
        or seal.get("record_id") != record_id
        or not isinstance(attachments, list)
        or not all(isinstance(item, dict) for item in attachments)
    ):
        raise VisionError("VISION_SOURCE_INVALID", "视觉 Base 来源范围无效")
    by_name: dict[str, dict[str, Any]] = {}
    sealed_ids: list[str] = []
    for index, item in enumerate(attachments):
        token = required_text(item.get("file_token"), f"attachments[{index}].file_token", "VISION_SOURCE_INVALID")
        saved_path = required_text(item.get("saved_path"), f"attachments[{index}].saved_path", "VISION_SOURCE_INVALID")
        source_hash = required_text(item.get("sha256"), f"attachments[{index}].sha256", "VISION_SOURCE_INVALID")
        size = item.get("size_bytes")
        name = Path(saved_path).name
        if (
            not REMOTE_ID.fullmatch(token)
            or not HEX64.fullmatch(source_hash)
            or type(size) is not int
            or size <= 0
            or not name
            or name in by_name
            or item.get("record_id") != record_id
        ):
            raise VisionError("VISION_SOURCE_INVALID", "视觉附件封印内容无效", {"attachment": index})
        by_name[name] = {
            "schema_version": SOURCE_SCHEMA,
            "app_token": app_token,
            "table_id": table_id,
            "record_id": record_id,
            "attachment_field_id": required_text(item.get("field_id"), f"attachments[{index}].field_id", "VISION_SOURCE_INVALID"),
            "attachment_field_name": "案件文档",
            "attachment_id": token,
            "attachment_name": name,
            "attachment_size": size,
            "attachment_sha256": source_hash,
        }
        sealed_ids.append(token)
    if sorted(sealed_ids) != sorted(runtime_ids) or len(sealed_ids) != len(set(sealed_ids)):
        raise VisionError("VISION_SOURCE_INVALID", "视觉附件封印与运行信封不一致")
    bindings: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = str(task["task_id"])
        source_file = str(task["source_file"])
        binding = by_name.get(source_file.split("::", 1)[0])
        if binding is None:
            raise VisionError("VISION_SOURCE_INVALID", "视觉任务无法绑定 Base 附件", {"task_id": task_id})
        if "::" not in source_file and task.get("source_sha256") != binding["attachment_sha256"]:
            raise VisionError("VISION_SOURCE_INVALID", "顶层视觉任务与附件哈希不一致", {"task_id": task_id})
        bindings[task_id] = {**binding, "source_locator": source_file}
    return bindings


def validate_result(task: dict[str, Any], value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    task_id = str(task["task_id"])
    require_keys(value, RESULT_KEYS, RESULT_KEYS, f"{task_id}.result", "VISION_RESULT_INVALID")
    producer = value.get("producer")
    regions = value.get("uncertain_regions")
    if (
        value.get("schema_version") != VISION_RESULT_SCHEMA
        or value.get("task_id") != task_id
        or value.get("source_sha256") != task.get("source_sha256")
        or value.get("image_sha256") != task.get("image_sha256")
        or producer != {"agent_name": VISION_AGENT_NAME}
        or value.get("status") not in {"complete", "partial", "failed"}
        or not isinstance(value.get("verbatim_text"), str)
        or not isinstance(regions, list)
    ):
        raise VisionError("VISION_RESULT_INVALID", "视觉结果身份、哈希或状态不正确", {"task_id": task_id})
    unresolved: list[dict[str, Any]] = []
    normalized_regions: list[dict[str, Any]] = []
    for index, region in enumerate(regions):
        if (
            not isinstance(region, dict)
            or not {"description", "critical"}.issubset(region)
            or not set(region).issubset({"description", "critical", "source_ref"})
            or not isinstance(region.get("description"), str)
            or not region["description"].strip()
            or type(region.get("critical")) is not bool
            or ("source_ref" in region and (not isinstance(region["source_ref"], str) or not region["source_ref"].strip()))
        ):
            raise VisionError("VISION_RESULT_INVALID", "视觉不确定区域结构不正确", {"task_id": task_id, "index": index})
        normalized_regions.append(region)
        if region["critical"]:
            unresolved.append({"field_type": "critical_region", "visible_text": region["description"], "status": "unclear"})
    text = value["verbatim_text"]
    if value["status"] != "complete" or not text.strip():
        unresolved.append({"field_type": "task", "visible_text": text or "视觉结果为空", "status": value["status"]})
    return {
        "schema_version": VISION_RESULT_SCHEMA,
        "task_id": task_id,
        "source_file": str(task.get("source_file") or ""),
        "source_sha256": task["source_sha256"],
        "image_sha256": task["image_sha256"],
        "unit": task.get("unit"),
        "page": task.get("page"),
        "status": value["status"],
        "producer": producer,
        "verbatim_text": text,
        "uncertain_regions": normalized_regions,
        "ocr_mean_confidence": task.get("ocr_mean_confidence"),
    }, unresolved


def prepare(
    runtime_path: Path, seal_path: Path, tasks_path: Path, image_root: Path, output: Path,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path, image_root)
    sources = load_source_bindings(runtime_path, seal_path, tasks)
    invocations = []
    for task in tasks:
        task_id = str(task["task_id"])
        source = sources[task_id]
        prompt = vision_agent_prompt(task, source)
        invocations.append({
            "task_id": task_id,
            "agent_name": VISION_AGENT_NAME,
            "transport": TRANSPORT,
            "source_binding": source,
            "source_binding_sha256": sha256_text(canonical(source)),
            "prompt": prompt,
            "prompt_sha256": sha256_text(prompt),
            "response_file": f"{task_id}.json",
        })
    manifest = {
        "schema_version": INVOCATION_SCHEMA,
        "policy": {
            "agent_name": VISION_AGENT_NAME,
            "transport": TRANSPORT,
            "one_task_per_invocation": True,
            "write_scope": "read_only_evidence",
        },
        "invocations": invocations,
        "summary": {"total": len(invocations), "pending": len(invocations)},
    }
    atomic_write(output, json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"status": "prepared", "tasks": len(invocations), "output": str(output.resolve())}


def load_invocations(
    path: Path, tasks: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    root = read_object(path, "VISION_INVOCATION_INVALID")
    expected_policy = {
        "agent_name": VISION_AGENT_NAME,
        "transport": TRANSPORT,
        "one_task_per_invocation": True,
        "write_scope": "read_only_evidence",
    }
    if (
        set(root) != {"schema_version", "policy", "invocations", "summary"}
        or root.get("schema_version") != INVOCATION_SCHEMA
        or root.get("policy") != expected_policy
        or not isinstance(root.get("invocations"), list)
        or root.get("summary") != {"total": len(tasks), "pending": len(tasks)}
    ):
        raise VisionError("VISION_INVOCATION_INVALID", "视觉调用清单结构不正确")
    task_map = {str(task["task_id"]): task for task in tasks}
    invocations: dict[str, dict[str, Any]] = {}
    required = {
        "task_id", "agent_name", "transport", "source_binding", "source_binding_sha256",
        "prompt", "prompt_sha256", "response_file",
    }
    for item in root["invocations"]:
        if not isinstance(item, dict) or set(item) != required:
            raise VisionError("VISION_INVOCATION_INVALID", "视觉调用项结构不正确")
        task_id = str(item.get("task_id") or "")
        task = task_map.get(task_id)
        source = sources.get(task_id)
        if task is None or source is None or task_id in invocations:
            raise VisionError("VISION_INVOCATION_INVALID", "视觉调用项无法绑定任务", {"task_id": task_id})
        prompt = vision_agent_prompt(task, source)
        if item != {
            "task_id": task_id,
            "agent_name": VISION_AGENT_NAME,
            "transport": TRANSPORT,
            "source_binding": source,
            "source_binding_sha256": sha256_text(canonical(source)),
            "prompt": prompt,
            "prompt_sha256": sha256_text(prompt),
            "response_file": f"{task_id}.json",
        }:
            raise VisionError("VISION_INVOCATION_INVALID", "视觉调用项被改写", {"task_id": task_id})
        invocations[task_id] = item
    if set(invocations) != set(task_map):
        raise VisionError("VISION_INVOCATION_INVALID", "视觉调用清单未覆盖全部任务")
    return invocations


def accept(
    runtime_path: Path, seal_path: Path, tasks_path: Path, image_root: Path,
    invocations_path: Path, responses_dir: Path, results_dir: Path, receipts_dir: Path,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path, image_root)
    sources = load_source_bindings(runtime_path, seal_path, tasks)
    invocations = load_invocations(invocations_path, tasks, sources)
    expected_files = {f"{task['task_id']}.json" for task in tasks}
    actual_files = {path.name for path in responses_dir.glob("*.json")}
    if actual_files != expected_files:
        raise VisionError(
            "VISION_RESPONSE_SET_INVALID", "视觉响应文件与任务不一致",
            {"missing": sorted(expected_files - actual_files), "unknown": sorted(actual_files - expected_files)},
        )
    results_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    accepted = 0
    for task in tasks:
        task_id = str(task["task_id"])
        response_path = responses_dir.resolve() / f"{task_id}.json"
        raw = response_path.read_text(encoding="utf-8")
        value = read_object(response_path, "VISION_RESULT_INVALID")
        _, unresolved = validate_result(task, value)
        if unresolved:
            raise VisionError("VISION_RESULT_INCOMPLETE", "视觉智能体未返回完整逐字结果", {"task_id": task_id, "items": unresolved})
        _, before = verify_image(task, image_root)
        invocation = invocations[task_id]
        source = sources[task_id]
        result_path = results_dir.resolve() / f"{task_id}.json"
        shutil.copyfile(response_path, result_path)
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "task_id": task_id,
            "source_sha256": task["source_sha256"],
            "image_sha256": task["image_sha256"],
            "agent_name": VISION_AGENT_NAME,
            "transport": TRANSPORT,
            "source_binding": source,
            "source_binding_sha256": invocation["source_binding_sha256"],
            "prompt_sha256": invocation["prompt_sha256"],
            "response_sha256": sha256_text(raw),
            "result_sha256": sha256_file(result_path),
        }
        _, after = verify_image(task, image_root)
        if before != after:
            result_path.unlink(missing_ok=True)
            raise VisionError("VISION_SNAPSHOT_INVALID", "视觉工件在接收期间发生变化", {"task_id": task_id})
        atomic_write(receipts_dir / f"{task_id}.receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2))
        accepted += 1
    return {"status": "accepted", "expected": len(tasks), "received": accepted}


def receipt_set_sha256(tasks: list[dict[str, Any]], receipts_dir: Path) -> str:
    entries = [
        {
            "task_id": str(task["task_id"]),
            "receipt_sha256": sha256_file(receipts_dir / f"{task['task_id']}.receipt.json"),
        }
        for task in sorted(tasks, key=lambda item: str(item["task_id"]))
    ]
    return sha256_text(canonical(entries))


def validate_receipt(
    task: dict[str, Any], source: dict[str, Any], result_path: Path, receipts_dir: Path,
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    receipt = read_object(receipts_dir / f"{task_id}.receipt.json", "VISION_RECEIPT_INVALID")
    keys = {
        "schema_version", "task_id", "source_sha256", "image_sha256", "agent_name", "transport",
        "source_binding", "source_binding_sha256", "prompt_sha256", "response_sha256", "result_sha256",
    }
    prompt = vision_agent_prompt(task, source)
    if (
        set(receipt) != keys
        or receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("task_id") != task_id
        or receipt.get("source_sha256") != task.get("source_sha256")
        or receipt.get("image_sha256") != task.get("image_sha256")
        or receipt.get("agent_name") != VISION_AGENT_NAME
        or receipt.get("transport") != TRANSPORT
        or receipt.get("source_binding") != source
        or receipt.get("source_binding_sha256") != sha256_text(canonical(source))
        or receipt.get("prompt_sha256") != sha256_text(prompt)
        or receipt.get("response_sha256") != sha256_file(result_path)
        or receipt.get("result_sha256") != sha256_file(result_path)
    ):
        raise VisionError("VISION_RECEIPT_INVALID", "视觉原生调用回执与任务不一致", {"task_id": task_id})
    return receipt


def reconcile(
    runtime_path: Path, seal_path: Path, tasks_path: Path, image_root: Path,
    results_dir: Path, receipts_dir: Path, corpus_path: Path, output_corpus: Path, evidence_path: Path,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path, image_root)
    sources = load_source_bindings(runtime_path, seal_path, tasks)
    try:
        source_corpus = corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VisionError("SOURCE_CORPUS_UNAVAILABLE", "无法读取初始材料语料", {"reason": str(exc)}) from exc
    expected_names = {f"{task['task_id']}.json" for task in tasks}
    actual_names = {path.name for path in results_dir.glob("*.json")}
    if actual_names != expected_names:
        raise VisionError(
            "VISION_RESULT_SET_INVALID", "视觉结果文件与任务不一致",
            {"missing": sorted(expected_names - actual_names), "unknown": sorted(actual_names - expected_names)},
        )
    results: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task["task_id"])
        result_path = results_dir / f"{task_id}.json"
        raw_result = read_object(result_path, "VISION_RESULT_INVALID")
        result, result_unresolved = validate_result(task, raw_result)
        receipt = validate_receipt(task, sources[task_id], result_path, receipts_dir)
        result["collection"] = {
            "agent_name": receipt["agent_name"],
            "transport": receipt["transport"],
            "source_binding": receipt["source_binding"],
            "source_binding_sha256": receipt["source_binding_sha256"],
            "prompt_sha256": receipt["prompt_sha256"],
            "response_sha256": receipt["response_sha256"],
            "result_sha256": receipt["result_sha256"],
        }
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
            "main_writer": MAIN_AGENT_NAME,
            "vision_worker": VISION_AGENT_NAME,
            "single_writer": True,
            "vision_worker_write_scope": "read_only_evidence",
        },
        "tasks": results,
        "unresolved": unresolved,
        "artifacts": {
            "vision_tasks_sha256": sha256_file(tasks_path),
            "vision_receipts_sha256": receipt_set_sha256(tasks, receipts_dir),
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
        raise VisionError("VISION_EVIDENCE_UNRESOLVED", "视觉智能体仍有未核清内容", {"items": unresolved[:20]})
    atomic_write(output_corpus, verified)
    return {
        "status": "valid", **evidence["summary"],
        "output_corpus": str(output_corpus.resolve()), "evidence": str(evidence_path.resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate evidence from the named visual agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--runtime", type=Path, required=True)
    common.add_argument("--download-seal", type=Path, required=True)
    common.add_argument("--tasks", type=Path, required=True)
    common.add_argument("--image-root", type=Path, required=True)
    prepare_parser = subparsers.add_parser("prepare", parents=[common])
    prepare_parser.add_argument("--output", type=Path, required=True)
    accept_parser = subparsers.add_parser("accept", parents=[common])
    accept_parser.add_argument("--invocations", type=Path, required=True)
    accept_parser.add_argument("--responses-dir", type=Path, required=True)
    accept_parser.add_argument("--results-dir", type=Path, required=True)
    accept_parser.add_argument("--receipts-dir", type=Path, required=True)
    reconcile_parser = subparsers.add_parser("reconcile", parents=[common])
    reconcile_parser.add_argument("--results-dir", type=Path, required=True)
    reconcile_parser.add_argument("--receipts-dir", type=Path, required=True)
    reconcile_parser.add_argument("--source-corpus", type=Path, required=True)
    reconcile_parser.add_argument("--output-corpus", type=Path, required=True)
    reconcile_parser.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            output = prepare(args.runtime, args.download_seal, args.tasks, args.image_root, args.output)
        elif args.command == "accept":
            output = accept(
                args.runtime, args.download_seal, args.tasks, args.image_root,
                args.invocations, args.responses_dir, args.results_dir, args.receipts_dir,
            )
        else:
            output = reconcile(
                args.runtime, args.download_seal, args.tasks, args.image_root,
                args.results_dir, args.receipts_dir, args.source_corpus, args.output_corpus, args.evidence,
            )
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except VisionError as exc:
        print(
            json.dumps(
                {"status": "invalid", "error_code": exc.code, "message": str(exc), "details": exc.details},
                ensure_ascii=False, separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
