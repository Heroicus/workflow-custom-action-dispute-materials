#!/usr/bin/env python3
"""Collect and reconcile source-bound visual evidence through one Aily agent."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from evidence_contract import (
    MAIN_AGENT_NAME,
    VISION_AGENT_ID as AGENT_ID,
    VISION_AGENT_NAME as EXPECTED_AGENT,
    VISION_RESULT_SCHEMA as RESULT_SCHEMA,
    VISION_TASK_SCHEMA as TASK_SCHEMA,
    vision_attachment_request as attachment_request,
    vision_chat_request as chat_request,
)

PACK_SCHEMA = "vision-evidence-pack/v3"
STATE_SCHEMA = "vision-collection-state/v2"
RECEIPT_SCHEMA = "vision-collection-receipt/v2"
AILY_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
AILY_IMAGE_MAX_BYTES = 5 * 1024 * 1024
ALLOWED_STATUS = {"complete", "partial", "failed"}
RESULT_KEYS = {
    "schema_version", "task_id", "source_sha256", "image_sha256", "producer",
    "status", "verbatim_text", "uncertain_regions",
}
TASK_KEYS = {
    "schema_version", "task_id", "source_file", "source_sha256", "unit", "page",
    "reason", "image_file", "image_sha256", "image_size", "image_transform",
    "ocr_text", "ocr_mean_confidence",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^vis_[0-9a-f]{20}$")
REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{8,256}$")


class VisionError(Exception):
    """A stable visual-evidence contract failure."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise VisionError("VISION_FILE_UNAVAILABLE", f"无法读取视觉工件：{path.name}", {"reason": str(exc)}) from exc
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_object(path: Path, code: str = "VISION_RESULT_INVALID") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VisionError(code, f"无法读取 JSON：{path.name}", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise VisionError(code, f"JSON 根节点必须是对象：{path.name}")
    return value


def required_text(value: Any, label: str, code: str = "VISION_RESULT_INVALID") -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisionError(code, f"{label} 必须是非空字符串")
    return value.strip()


def require_keys(
    value: dict[str, Any], required: set[str], allowed: set[str], label: str,
    code: str = "VISION_RESULT_INVALID",
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
    if image.suffix.lower() not in AILY_IMAGE_SUFFIXES or fingerprint[2] > AILY_IMAGE_MAX_BYTES:
        raise VisionError(
            "VISION_ATTACHMENT_UNSUPPORTED", "视觉工件不符合 Aily 图片附件格式或大小限制",
            {"task_id": task_id, "suffix": image.suffix.lower(), "size": fingerprint[2]},
        )
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
    mode = transform.get("mode")
    dimensions = (
        transform.get("source_pixel_width"), transform.get("source_pixel_height"),
        transform.get("output_pixel_width"), transform.get("output_pixel_height"),
    )
    if (
        mode not in {"passthrough", "transcoded"}
        or type(transform.get("source_size_bytes")) is not int
        or transform["source_size_bytes"] <= 0
        or not all(type(value) is int and value > 0 for value in dimensions)
        or transform.get("output_suffix") != Path(str(task["image_file"])).suffix.lower()
    ):
        raise VisionError("VISION_TASKS_INVALID", "视觉转换记录内容不正确", {"task_id": task_id})
    quality = transform.get("jpeg_quality")
    if mode == "passthrough":
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
    if root.get("schema_version") != TASK_SCHEMA or not isinstance(root.get("tasks"), list):
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单版本或结构不正确")
    if root.get("policy") != {
        "worker": EXPECTED_AGENT,
        "agent_id": AGENT_ID,
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
        image_size = item.get("image_size")
        if item.get("schema_version") != TASK_SCHEMA or not TASK_ID.fullmatch(task_id) or task_id in seen:
            raise VisionError("VISION_TASKS_INVALID", "视觉任务版本、ID 格式或唯一性不正确", {"task_id": task_id})
        if not HEX64.fullmatch(source_hash) or not HEX64.fullmatch(image_hash):
            raise VisionError("VISION_TASKS_INVALID", "视觉任务哈希格式不正确", {"task_id": task_id})
        if type(image_size) is not int or image_size <= 0:
            raise VisionError("VISION_TASKS_INVALID", "视觉工件大小不正确", {"task_id": task_id})
        validate_image_transform(item)
        seen.add(task_id)
        verify_image(item, image_root)
        tasks.append(item)
    if root.get("summary") != {"total": len(tasks), "pending": len(tasks)}:
        raise VisionError("VISION_TASKS_INVALID", "视觉任务清单统计与任务不一致")
    return tasks


def parse_cli_response(source: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", f"{label}未返回单一 JSON 对象", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", f"{label}返回值不是对象")
    return value


def run_lark(cli: str, args: list[str], cwd: Path, timeout: int, label: str) -> tuple[dict[str, Any], str]:
    environment = dict(os.environ)
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    try:
        completed = subprocess.run(
            [cli, *args], cwd=str(cwd), env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VisionError("VISION_REMOTE_COMMAND_FAILED", f"{label}命令执行失败", {"reason": str(exc)}) from exc
    raw = completed.stdout.strip() or completed.stderr.strip()
    response = parse_cli_response(raw, label)
    if completed.returncode != 0 or response.get("ok") is not True:
        raise VisionError(
            "VISION_REMOTE_OPERATION_FAILED", f"{label}返回失败",
            {"returncode": completed.returncode, "message": str(response.get("message") or response.get("msg") or response)},
        )
    if response.get("identity") != "user":
        raise VisionError("VISION_IDENTITY_MISMATCH", f"{label}未使用飞书用户身份")
    return response, raw


def response_data(response: dict[str, Any], label: str) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", f"{label}缺少 data 对象")
    return data


def write_raw(path: Path, raw: str) -> str:
    atomic_write(path, raw.rstrip() + "\n")
    return sha256_file(path)


def state_path(receipts_dir: Path, task_id: str) -> Path:
    return receipts_dir / f"{task_id}.state.json"


def receipt_path(receipts_dir: Path, task_id: str) -> Path:
    return receipts_dir / f"{task_id}.receipt.json"


def raw_path(receipts_dir: Path, task_id: str, attempt: int, stage: str) -> Path:
    return receipts_dir / "raw" / f"{task_id}.attempt-{attempt:04d}.{stage}.json"


def base_state(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA,
        "task_id": task["task_id"],
        "source_sha256": task["source_sha256"],
        "image_sha256": task["image_sha256"],
        "attempt": 1,
        "phase": "new",
        "agent_attachment_id": "",
        "agent_chat_id": "",
        "attachment_request_sha256": "",
        "attachment_response_sha256": "",
        "chat_request_sha256": "",
        "chat_create_response_sha256": "",
        "chat_read_response_sha256": "",
        "error_code": "",
        "error_message": "",
    }


def load_state(path: Path, task: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return base_state(task)
    state = read_object(path, "VISION_STATE_INVALID")
    expected_keys = set(base_state(task))
    require_keys(state, expected_keys, expected_keys, "视觉收集状态", "VISION_STATE_INVALID")
    if any(state.get(key) != task.get(key) for key in ("task_id", "source_sha256", "image_sha256")):
        raise VisionError("VISION_STATE_INVALID", "视觉收集状态与任务不一致")
    if (
        state.get("schema_version") != STATE_SCHEMA
        or type(state.get("attempt")) is not int
        or state["attempt"] < 1
        or state.get("phase") not in {"new", "uploaded", "chat_created", "retryable_failed", "validated_complete"}
    ):
        raise VisionError("VISION_STATE_INVALID", "视觉收集状态版本或阶段不正确")
    return state


def next_attempt(task: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    state = base_state(task)
    state["attempt"] = int(previous["attempt"]) + 1
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))


@contextmanager
def task_lock(receipts_dir: Path, task_id: str) -> Iterator[None]:
    lock_path = receipts_dir.resolve() / f"{task_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def extract_chat_text(response: dict[str, Any], task_id: str) -> tuple[str, str]:
    data = response_data(response, "视觉会话读回")
    status = required_text(data.get("status"), "data.status", "VISION_REMOTE_RESPONSE_INVALID")
    if status != "Completed":
        return status, ""
    content = data.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", "已完成的视觉会话必须只有一个文本结果", {"task_id": task_id})
    item = content[0]
    if item.get("type") != "text" or not isinstance(item.get("text"), str):
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", "视觉会话结果不是文本", {"task_id": task_id})
    return status, item["text"]


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
    require_keys(producer, {"agent_name"}, {"agent_name"}, f"{task_id}.producer")
    if producer != {"agent_name": EXPECTED_AGENT}:
        raise VisionError("VISION_WORKER_MISMATCH", "视觉结果不是指定子智能体产物", {"task_id": task_id})
    status = str(result.get("status") or "")
    if status not in ALLOWED_STATUS:
        raise VisionError("VISION_RESULT_INVALID", "视觉任务 status 非法", {"task_id": task_id, "status": status})
    raw_vision_text = result.get("verbatim_text")
    if not isinstance(raw_vision_text, str):
        raise VisionError("VISION_RESULT_INVALID", "verbatim_text 必须是字符串", {"task_id": task_id})
    vision_text = raw_vision_text.strip()
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
        {"field_type": "uncertain_region", "visible_text": str(item["description"]), "status": "unclear"}
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


def walk_values(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)


def first_remote_id(response: dict[str, Any], key: str, label: str) -> str:
    values = [
        item[key].strip() for item in walk_values(response)
        if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip()
    ]
    if len(set(values)) != 1 or not values or not REMOTE_ID.fullmatch(values[0]):
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", f"{label}未返回唯一 {key}")
    return values[0]


def validated_raw(path: Path, expected_hash: str, label: str) -> str:
    if not HEX64.fullmatch(expected_hash) or not path.is_file() or sha256_file(path) != expected_hash:
        raise VisionError("VISION_RECEIPT_INVALID", f"{label}缺失或哈希不一致")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VisionError("VISION_RECEIPT_INVALID", f"无法读取{label}", {"reason": str(exc)}) from exc


def validated_remote_response(path: Path, expected_hash: str, label: str) -> dict[str, Any]:
    response = parse_cli_response(validated_raw(path, expected_hash, label), label)
    if response.get("ok") is not True or response.get("identity") != "user":
        raise VisionError("VISION_RECEIPT_INVALID", f"{label}不是用户身份成功结果")
    return response


def validated_object(path: Path, expected_hash: str, label: str) -> dict[str, Any]:
    raw = validated_raw(path, expected_hash, label)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionError("VISION_RECEIPT_INVALID", f"{label}不是 JSON 对象", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise VisionError("VISION_RECEIPT_INVALID", f"{label}根节点不是对象")
    return value


def validate_collection_receipt(task: dict[str, Any], receipts_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task["task_id"])
    receipt = read_object(receipt_path(receipts_dir, task_id), "VISION_RECEIPT_INVALID")
    keys = {
        "schema_version", "task_id", "source_sha256", "image_sha256", "agent_id", "attempt",
        "agent_attachment_id", "agent_chat_id", "attachment_request_sha256",
        "attachment_response_sha256", "chat_request_sha256", "chat_create_response_sha256",
        "chat_read_response_sha256",
    }
    require_keys(receipt, keys, keys, "视觉收集回执", "VISION_RECEIPT_INVALID")
    attempt = receipt.get("attempt")
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("agent_id") != AGENT_ID
        or type(attempt) is not int
        or attempt < 1
        or any(receipt.get(key) != task.get(key) for key in ("task_id", "source_sha256", "image_sha256"))
    ):
        raise VisionError("VISION_RECEIPT_INVALID", "视觉收集回执与任务不一致", {"task_id": task_id})
    if not REMOTE_ID.fullmatch(str(receipt.get("agent_attachment_id") or "")) or not REMOTE_ID.fullmatch(str(receipt.get("agent_chat_id") or "")):
        raise VisionError("VISION_RECEIPT_INVALID", "视觉收集回执远程 ID 不正确", {"task_id": task_id})
    attachment_id = str(receipt["agent_attachment_id"])
    chat_id = str(receipt["agent_chat_id"])
    saved_attachment_request = validated_object(
        raw_path(receipts_dir, task_id, attempt, "attachment-request"),
        str(receipt["attachment_request_sha256"]), "视觉附件请求",
    )
    if saved_attachment_request != attachment_request(task):
        raise VisionError("VISION_RECEIPT_INVALID", "视觉附件请求与任务不一致", {"task_id": task_id})
    attachment_response = validated_remote_response(
        raw_path(receipts_dir, task_id, attempt, "attachment"),
        str(receipt["attachment_response_sha256"]), "视觉附件响应",
    )
    if first_remote_id(attachment_response, "agent_attachment_id", "视觉附件响应") != attachment_id:
        raise VisionError("VISION_RECEIPT_INVALID", "视觉附件 ID 与回执不一致", {"task_id": task_id})
    saved_chat_request = validated_object(
        raw_path(receipts_dir, task_id, attempt, "chat-request"),
        str(receipt["chat_request_sha256"]), "视觉会话请求",
    )
    if saved_chat_request != chat_request(task, attachment_id):
        raise VisionError("VISION_RECEIPT_INVALID", "视觉会话请求未唯一绑定附件和任务", {"task_id": task_id})
    chat_response = validated_remote_response(
        raw_path(receipts_dir, task_id, attempt, "chat-create"),
        str(receipt["chat_create_response_sha256"]), "视觉会话创建响应",
    )
    if first_remote_id(chat_response, "agent_chat_id", "视觉会话创建响应") != chat_id:
        raise VisionError("VISION_RECEIPT_INVALID", "视觉会话 ID 与回执不一致", {"task_id": task_id})
    read_response = validated_remote_response(
        raw_path(receipts_dir, task_id, attempt, "chat-read"),
        str(receipt["chat_read_response_sha256"]), "视觉会话读回响应",
    )
    status, text = extract_chat_text(read_response, task_id)
    if status != "Completed":
        raise VisionError("VISION_RECEIPT_INVALID", "视觉会话读回未完成", {"task_id": task_id})
    try:
        remote_result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionError("VISION_RECEIPT_INVALID", "视觉会话读回不是单一 JSON 对象", {"task_id": task_id}) from exc
    if remote_result != result:
        raise VisionError("VISION_RECEIPT_INVALID", "视觉会话读回与本地结果不一致", {"task_id": task_id})
    return receipt


def receipt_set_sha256(tasks: list[dict[str, Any]], receipts_dir: Path) -> str:
    entries = [
        {
            "task_id": str(task["task_id"]),
            "receipt_sha256": sha256_file(receipt_path(receipts_dir, str(task["task_id"]))),
        }
        for task in sorted(tasks, key=lambda item: str(item["task_id"]))
    ]
    return sha256_text(json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def mark_retryable_failure(path: Path, state: dict[str, Any], error: VisionError) -> None:
    state.update({"phase": "retryable_failed", "error_code": error.code, "error_message": str(error)})
    save_state(path, state)


def recover_completed_state(
    task: dict[str, Any], state: dict[str, Any], receipt: dict[str, Any], path: Path,
) -> None:
    state.update({
        "schema_version": STATE_SCHEMA,
        "task_id": task["task_id"],
        "source_sha256": task["source_sha256"],
        "image_sha256": task["image_sha256"],
        "attempt": receipt["attempt"],
        "phase": "validated_complete",
        "agent_attachment_id": receipt["agent_attachment_id"],
        "agent_chat_id": receipt["agent_chat_id"],
        "attachment_request_sha256": receipt["attachment_request_sha256"],
        "attachment_response_sha256": receipt["attachment_response_sha256"],
        "chat_request_sha256": receipt["chat_request_sha256"],
        "chat_create_response_sha256": receipt["chat_create_response_sha256"],
        "chat_read_response_sha256": receipt["chat_read_response_sha256"],
        "error_code": "",
        "error_message": "",
    })
    save_state(path, state)


def collect_task_once(
    task: dict[str, Any], image_root: Path, results_dir: Path, receipts_dir: Path,
    executable: str, wait_seconds: int, command_timeout: int, poll_seconds: int,
    overall_deadline: float,
) -> bool:
    task_id = str(task["task_id"])
    result_path = results_dir / f"{task_id}.json"
    current_state_path = state_path(receipts_dir, task_id)
    state = load_state(current_state_path, task)
    if result_path.is_file():
        normalized, unresolved = validate_result(task, result_path)
        if unresolved or normalized["status"] != "complete":
            raise VisionError("VISION_RESULT_INCOMPLETE", "已缓存视觉结果不是完整结果", {"task_id": task_id})
        receipt = validate_collection_receipt(task, receipts_dir, read_object(result_path))
        if state["phase"] != "validated_complete":
            recover_completed_state(task, state, receipt, current_state_path)
        return True
    if state["phase"] == "validated_complete":
        raise VisionError("VISION_STATE_INVALID", "已验证视觉状态缺少结果文件", {"task_id": task_id})
    if state["phase"] == "retryable_failed":
        state = next_attempt(task, state)
        save_state(current_state_path, state)

    if time.monotonic() >= overall_deadline:
        raise VisionError("VISION_TOTAL_TIMEOUT", "视觉任务总等待时间已用尽", {"task_id": task_id})
    image, before = verify_image(task, image_root)
    attempt = int(state["attempt"])
    if state["phase"] == "new":
        attachment_request_path = raw_path(receipts_dir, task_id, attempt, "attachment-request")
        atomic_write(attachment_request_path, json.dumps(attachment_request(task), ensure_ascii=False, indent=2))
        attachment_request_hash = sha256_file(attachment_request_path)
        response, raw = run_lark(
            executable,
            [
                "api", "POST", f"/open-apis/aily/v1/agents/{AGENT_ID}/attachments",
                "--as", "user", "--file", image.name, "--data", '{"type":"image"}', "--json",
            ],
            image.parent, command_timeout, "视觉附件上传",
        )
        attachment_hash = write_raw(raw_path(receipts_dir, task_id, attempt, "attachment"), raw)
        _, after = verify_image(task, image_root)
        if before != after:
            raise VisionError("VISION_SNAPSHOT_INVALID", "视觉工件在上传期间发生变化", {"task_id": task_id})
        state.update({
            "phase": "uploaded",
            "agent_attachment_id": first_remote_id(response, "agent_attachment_id", "视觉附件上传"),
            "attachment_request_sha256": attachment_request_hash,
            "attachment_response_sha256": attachment_hash,
        })
        save_state(current_state_path, state)
    if state["phase"] == "uploaded":
        request = chat_request(task, str(state["agent_attachment_id"]))
        chat_request_path = raw_path(receipts_dir, task_id, attempt, "chat-request")
        atomic_write(chat_request_path, json.dumps(request, ensure_ascii=False, indent=2))
        chat_request_hash = sha256_file(chat_request_path)
        response, raw = run_lark(
            executable,
            [
                "api", "POST", f"/open-apis/aily/v1/agents/{AGENT_ID}/chats",
                "--as", "user", "--data", json.dumps(request, ensure_ascii=False, separators=(",", ":")), "--json",
            ],
            receipts_dir.resolve(), command_timeout, "视觉会话创建",
        )
        chat_hash = write_raw(raw_path(receipts_dir, task_id, attempt, "chat-create"), raw)
        state.update({
            "phase": "chat_created",
            "agent_chat_id": first_remote_id(response, "agent_chat_id", "视觉会话创建"),
            "chat_request_sha256": chat_request_hash,
            "chat_create_response_sha256": chat_hash,
        })
        save_state(current_state_path, state)

    deadline = min(time.monotonic() + wait_seconds, overall_deadline)
    while True:
        response, raw = run_lark(
            executable,
            [
                "api", "GET", f"/open-apis/aily/v1/agents/{AGENT_ID}/chats/{state['agent_chat_id']}",
                "--as", "user", "--json",
            ],
            receipts_dir.resolve(), command_timeout, "视觉会话读回",
        )
        status, text = extract_chat_text(response, task_id)
        if status == "Completed":
            read_hash = write_raw(raw_path(receipts_dir, task_id, attempt, "chat-read"), raw)
            state["chat_read_response_sha256"] = read_hash
            save_state(current_state_path, state)
            break
        if status not in {"Pending", "Running"}:
            read_hash = write_raw(raw_path(receipts_dir, task_id, attempt, "chat-read"), raw)
            state["chat_read_response_sha256"] = read_hash
            error = VisionError(
                "VISION_REMOTE_OPERATION_FAILED", "视觉会话未正常完成",
                {"task_id": task_id, "status": status},
            )
            mark_retryable_failure(current_state_path, state, error)
            raise error
        if time.monotonic() >= deadline:
            raise VisionError("VISION_REMOTE_TIMEOUT", "视觉会话读回超时", {"task_id": task_id})
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    candidate_path = results_dir / f".{task_id}.candidate.json"
    try:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisionError(
                "VISION_RESULT_INVALID", "视觉子智能体未返回单一 JSON 对象",
                {"task_id": task_id, "reason": str(exc)},
            ) from exc
        if not isinstance(parsed, dict):
            raise VisionError("VISION_RESULT_INVALID", "视觉子智能体结果根节点不是对象", {"task_id": task_id})
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "task_id": task_id,
            "source_sha256": task["source_sha256"],
            "image_sha256": task["image_sha256"],
            "agent_id": AGENT_ID,
            "attempt": attempt,
            "agent_attachment_id": state["agent_attachment_id"],
            "agent_chat_id": state["agent_chat_id"],
            "attachment_request_sha256": state["attachment_request_sha256"],
            "attachment_response_sha256": state["attachment_response_sha256"],
            "chat_request_sha256": state["chat_request_sha256"],
            "chat_create_response_sha256": state["chat_create_response_sha256"],
            "chat_read_response_sha256": state["chat_read_response_sha256"],
        }
        atomic_write(receipt_path(receipts_dir, task_id), json.dumps(receipt, ensure_ascii=False, indent=2))
        atomic_write(candidate_path, json.dumps(parsed, ensure_ascii=False, indent=2))
        normalized, unresolved = validate_result(task, candidate_path)
        if unresolved or normalized["status"] != "complete":
            raise VisionError(
                "VISION_RESULT_INCOMPLETE", "视觉子智能体未返回完整可用的逐字结果",
                {"task_id": task_id, "status": normalized["status"], "unresolved": unresolved},
            )
        validate_collection_receipt(task, receipts_dir, parsed)
    except VisionError as exc:
        candidate_path.unlink(missing_ok=True)
        mark_retryable_failure(current_state_path, state, exc)
        raise
    candidate_path.replace(result_path)
    recover_completed_state(task, state, receipt, current_state_path)
    return False


def collect(
    tasks_path: Path, image_root: Path, results_dir: Path, receipts_dir: Path,
    cli: str, wait_seconds: int, poll_seconds: int, command_timeout: int,
    total_wait_seconds: int, max_attempts: int, workers: int,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path, image_root)
    executable = shutil.which(cli) if os.sep not in cli else cli
    if tasks and not executable:
        raise VisionError("LARK_CLI_UNAVAILABLE", "未找到 lark-cli")
    results_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    overall_deadline = time.monotonic() + total_wait_seconds
    retryable_codes = {
        "VISION_RESULT_INVALID", "VISION_RESULT_INCOMPLETE", "VISION_REMOTE_OPERATION_FAILED",
    }
    def process(task: dict[str, Any]) -> int:
        task_id = str(task["task_id"])
        with task_lock(receipts_dir, task_id):
            failures = 0
            while True:
                try:
                    was_cached = collect_task_once(
                        task, image_root, results_dir, receipts_dir, str(executable),
                        wait_seconds, command_timeout, poll_seconds, overall_deadline,
                    )
                    return int(was_cached)
                except VisionError as exc:
                    if exc.code not in retryable_codes:
                        raise
                    failures += 1
                    if failures >= max_attempts or time.monotonic() >= overall_deadline:
                        raise VisionError(
                            "VISION_RETRY_EXHAUSTED", "视觉子智能体未在限定尝试内返回有效结果",
                            {"task_id": task_id, "attempts": failures, "cause": exc.code},
                        ) from exc
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(tasks)))) as executor:
        cached = sum(executor.map(process, tasks))
    return {"status": "complete", "expected": len(tasks), "received": len(tasks), "cached": cached}


def reconcile(
    tasks_path: Path, image_root: Path, results_dir: Path, receipts_dir: Path,
    corpus_path: Path, output_corpus: Path, evidence_path: Path,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path, image_root)
    try:
        source_corpus = corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VisionError("SOURCE_CORPUS_UNAVAILABLE", "无法读取初始材料语料", {"reason": str(exc)}) from exc
    results: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    expected_names = {f"{task['task_id']}.json" for task in tasks}
    extra_results = sorted(path.name for path in results_dir.glob("*.json") if path.name not in expected_names)
    if extra_results:
        raise VisionError("VISION_RESULT_INVALID", "视觉结果目录含未知任务结果", {"files": extra_results[:20]})
    for task in tasks:
        task_id = str(task["task_id"])
        path = results_dir / f"{task_id}.json"
        if not path.is_file():
            unresolved.append({"task_id": task_id, "field_type": "task", "visible_text": "缺少视觉结果", "status": "unclear"})
            continue
        state = load_state(state_path(receipts_dir, task_id), task)
        if state["phase"] != "validated_complete":
            raise VisionError("VISION_STATE_INVALID", "视觉结果未处于已验证完成状态", {"task_id": task_id})
        raw_result = read_object(path)
        validate_collection_receipt(task, receipts_dir, raw_result)
        result, result_unresolved = validate_result(task, path)
        receipt = read_object(receipt_path(receipts_dir, task_id), "VISION_RECEIPT_INVALID")
        result["collection"] = {
            "agent_id": receipt["agent_id"],
            "attempt": receipt["attempt"],
            "agent_attachment_id": receipt["agent_attachment_id"],
            "agent_chat_id": receipt["agent_chat_id"],
            "attachment_request_sha256": receipt["attachment_request_sha256"],
            "attachment_response_sha256": receipt["attachment_response_sha256"],
            "chat_request_sha256": receipt["chat_request_sha256"],
            "chat_create_response_sha256": receipt["chat_create_response_sha256"],
            "chat_read_response_sha256": receipt["chat_read_response_sha256"],
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
            "vision_worker": EXPECTED_AGENT,
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
        raise VisionError("VISION_EVIDENCE_UNRESOLVED", "视觉子智能体仍有未核清的关键内容", {"items": unresolved[:20], "total": len(unresolved)})
    atomic_write(output_corpus, verified)
    return {
        "status": "valid", **evidence["summary"],
        "output_corpus": str(output_corpus.resolve()), "evidence": str(evidence_path.resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and validate visual-agent evidence.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--tasks", type=Path, required=True)
    collect_parser.add_argument("--image-root", type=Path, required=True)
    collect_parser.add_argument("--results-dir", type=Path, required=True)
    collect_parser.add_argument("--receipts-dir", type=Path, required=True)
    collect_parser.add_argument("--lark-cli", default="lark-cli")
    collect_parser.add_argument("--wait-seconds", type=int, default=900)
    collect_parser.add_argument("--poll-seconds", type=int, default=5)
    collect_parser.add_argument("--command-timeout", type=int, default=180)
    collect_parser.add_argument("--total-wait-seconds", type=int, default=3000)
    collect_parser.add_argument("--max-attempts", type=int, default=3)
    collect_parser.add_argument("--workers", type=int, default=3)
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--tasks", type=Path, required=True)
    reconcile_parser.add_argument("--image-root", type=Path, required=True)
    reconcile_parser.add_argument("--results-dir", type=Path, required=True)
    reconcile_parser.add_argument("--receipts-dir", type=Path, required=True)
    reconcile_parser.add_argument("--source-corpus", type=Path, required=True)
    reconcile_parser.add_argument("--output-corpus", type=Path, required=True)
    reconcile_parser.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            if (
                args.wait_seconds < 1 or args.poll_seconds < 1 or args.command_timeout < 1
                or args.total_wait_seconds < 1 or args.max_attempts < 1 or not 1 <= args.workers <= 4
            ):
                raise VisionError("VISION_ARGUMENT_INVALID", "等待、轮询和命令超时必须为正整数")
            output = collect(
                args.tasks, args.image_root, args.results_dir, args.receipts_dir,
                args.lark_cli, args.wait_seconds, args.poll_seconds, args.command_timeout,
                args.total_wait_seconds, args.max_attempts, args.workers,
            )
        else:
            output = reconcile(
                args.tasks, args.image_root, args.results_dir, args.receipts_dir,
                args.source_corpus, args.output_corpus, args.evidence,
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
