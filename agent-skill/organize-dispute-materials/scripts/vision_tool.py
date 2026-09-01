#!/usr/bin/env python3
"""Prepare, transcribe and reconcile evidence from the named visual agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
INVOCATION_SCHEMA = "vision-attachment-invocation-batch/v1"
RECEIPT_SCHEMA = "vision-attachment-agent-receipt/v1"
SOURCE_SCHEMA = "vision-base-source/v1"
TRANSPORT = "aily_attachment_chat"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
AILY_IMAGE_MAX_BYTES = 5 * 1024 * 1024
AILY_API_ROOT = f"/open-apis/aily/v1/agents/{VISION_AGENT_ID}"
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


def parse_cli_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionError(
            "VISION_REMOTE_RESPONSE_INVALID", f"{label}未返回 JSON", {"reason": str(exc)},
        ) from exc
    if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("data"), dict):
        raise VisionError("VISION_REMOTE_RESPONSE_INVALID", f"{label}响应结构不正确")
    return value


def run_lark(
    executable: str, arguments: list[str], cwd: Path, timeout: int, label: str,
) -> tuple[dict[str, Any], str]:
    try:
        completed = subprocess.run(
            [executable, *arguments], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VisionError("VISION_TRANSPORT_TIMEOUT", f"{label}超时") from exc
    if completed.returncode != 0:
        raise VisionError(
            "VISION_TRANSPORT_FAILED", f"{label}失败",
            {"returncode": completed.returncode, "stderr": completed.stderr.strip()[-2000:]},
        )
    return parse_cli_object(completed.stdout, label), completed.stdout


def response_path(receipts_dir: Path, task_id: str, stage: str) -> Path:
    return receipts_dir.resolve() / f"{task_id}.{stage}.json"


def write_remote_response(path: Path, raw: str) -> str:
    atomic_write(path, raw)
    return sha256_file(path)


def content_text(result: dict[str, Any], task_id: str) -> str:
    data = result["data"]
    if data.get("status") != "Completed":
        raise VisionError(
            "VISION_REMOTE_RESPONSE_INVALID", "视觉会话未完成",
            {"task_id": task_id, "status": data.get("status")},
        )
    content = data.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
        or not isinstance(content[0].get("text"), str)
        or not content[0]["text"].strip()
    ):
        raise VisionError(
            "VISION_REMOTE_RESPONSE_INVALID", "视觉会话未返回唯一文本结果", {"task_id": task_id},
        )
    return content[0]["text"].strip()


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
            or item["image_size"] > AILY_IMAGE_MAX_BYTES
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
            "agent_id": VISION_AGENT_ID,
            "transport": TRANSPORT,
            "image_file": task["image_file"],
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
            "agent_id": VISION_AGENT_ID,
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
        "agent_id": VISION_AGENT_ID,
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
        "task_id", "agent_name", "agent_id", "transport", "image_file", "source_binding",
        "source_binding_sha256", "prompt", "prompt_sha256", "response_file",
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
            "agent_id": VISION_AGENT_ID,
            "transport": TRANSPORT,
            "image_file": task["image_file"],
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


def transcribe(
    runtime_path: Path, seal_path: Path, tasks_path: Path, image_root: Path,
    invocations_path: Path, results_dir: Path, receipts_dir: Path, lark_cli: str,
    wait_seconds: int, poll_seconds: int, command_timeout: int,
) -> dict[str, Any]:
    if wait_seconds <= 0 or poll_seconds <= 0 or command_timeout <= 0:
        raise VisionError("VISION_TRANSPORT_CONFIG_INVALID", "视觉超时和轮询参数必须大于零")
    tasks = load_tasks(tasks_path, image_root)
    sources = load_source_bindings(runtime_path, seal_path, tasks)
    invocations = load_invocations(invocations_path, tasks, sources)
    results_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    executable = Path(lark_cli).expanduser() if lark_cli else None
    if executable is None or not executable.is_file():
        discovered = shutil.which(lark_cli or "lark-cli")
        executable = Path(discovered) if discovered else None
    if executable is None:
        raise VisionError("VISION_TRANSPORT_UNAVAILABLE", "找不到 lark-cli")

    sessions: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = str(task["task_id"])
        invocation = invocations[task_id]
        image, before = verify_image(task, image_root)
        upload, upload_raw = run_lark(
            str(executable), [
                "api", "POST", f"{AILY_API_ROOT}/attachments", "--as", "user",
                "--data", '{"type":"image"}', "--file", f"file={image.name}", "--format", "json",
            ], image.parent, command_timeout, "视觉附件上传",
        )
        attachment_id = upload["data"].get("agent_attachment_id")
        if not isinstance(attachment_id, str) or not REMOTE_ID.fullmatch(attachment_id):
            raise VisionError(
                "VISION_REMOTE_RESPONSE_INVALID", "视觉附件上传响应缺少附件标识", {"task_id": task_id},
            )
        upload_sha256 = write_remote_response(response_path(receipts_dir, task_id, "upload"), upload_raw)
        chat_body = {
            "stream": False,
            "user_message": {
                "content": [{"type": "text", "text": invocation["prompt"]}],
                "agent_attachment_ids": [attachment_id],
            },
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=receipts_dir, prefix=f".{task_id}.chat.", suffix=".json", delete=False,
        ) as handle:
            json.dump(chat_body, handle, ensure_ascii=False, separators=(",", ":"))
            request_path = Path(handle.name)
        try:
            chat, chat_raw = run_lark(
                str(executable), [
                    "api", "POST", f"{AILY_API_ROOT}/chats", "--as", "user",
                    "--data", f"@{request_path.name}", "--format", "json",
                ], receipts_dir, command_timeout, "视觉会话创建",
            )
        finally:
            request_path.unlink(missing_ok=True)
        agent_chat_id = chat["data"].get("agent_chat_id")
        session_id = chat["data"].get("session_id")
        if (
            not isinstance(agent_chat_id, str)
            or not REMOTE_ID.fullmatch(agent_chat_id)
            or not isinstance(session_id, str)
            or not REMOTE_ID.fullmatch(session_id)
        ):
            raise VisionError(
                "VISION_REMOTE_RESPONSE_INVALID", "视觉会话创建响应缺少会话标识", {"task_id": task_id},
            )
        chat_sha256 = write_remote_response(response_path(receipts_dir, task_id, "chat"), chat_raw)
        _, after = verify_image(task, image_root)
        if before != after:
            raise VisionError("VISION_SNAPSHOT_INVALID", "视觉工件在上传期间发生变化", {"task_id": task_id})
        sessions[task_id] = {
            "task": task,
            "invocation": invocation,
            "source": sources[task_id],
            "fingerprint": before,
            "attachment_id": attachment_id,
            "agent_chat_id": agent_chat_id,
            "session_id": session_id,
            "upload_response_sha256": upload_sha256,
            "chat_response_sha256": chat_sha256,
        }

    deadline = time.monotonic() + wait_seconds
    pending = set(sessions)
    while pending:
        for task_id in sorted(pending):
            session = sessions[task_id]
            result, result_raw = run_lark(
                str(executable), [
                    "api", "GET", f"{AILY_API_ROOT}/chats/{session['agent_chat_id']}",
                    "--as", "user", "--format", "json",
                ], receipts_dir, command_timeout, "视觉结果读回",
            )
            status = result["data"].get("status")
            if status == "Running":
                continue
            if status != "Completed":
                raise VisionError(
                    "VISION_REMOTE_RESPONSE_INVALID", "视觉会话异常结束",
                    {"task_id": task_id, "status": status},
                )
            raw_text = content_text(result, task_id)
            try:
                value = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise VisionError(
                    "VISION_RESULT_INVALID", "视觉智能体结果不是 JSON", {"task_id": task_id, "reason": str(exc)},
                ) from exc
            if not isinstance(value, dict):
                raise VisionError("VISION_RESULT_INVALID", "视觉智能体结果根节点不是对象", {"task_id": task_id})
            _, unresolved = validate_result(session["task"], value)
            if unresolved:
                raise VisionError(
                    "VISION_RESULT_INCOMPLETE", "视觉智能体未返回完整逐字结果",
                    {"task_id": task_id, "items": unresolved},
                )
            result_path = results_dir.resolve() / f"{task_id}.json"
            atomic_write(result_path, raw_text)
            result_response_sha256 = write_remote_response(
                response_path(receipts_dir, task_id, "result"), result_raw,
            )
            _, after = verify_image(session["task"], image_root)
            if session["fingerprint"] != after:
                result_path.unlink(missing_ok=True)
                raise VisionError(
                    "VISION_SNAPSHOT_INVALID", "视觉工件在结果读回期间发生变化", {"task_id": task_id},
                )
            invocation = session["invocation"]
            receipt = {
                "schema_version": RECEIPT_SCHEMA,
                "task_id": task_id,
                "source_sha256": session["task"]["source_sha256"],
                "image_sha256": session["task"]["image_sha256"],
                "agent_name": VISION_AGENT_NAME,
                "agent_id": VISION_AGENT_ID,
                "transport": TRANSPORT,
                "source_binding": session["source"],
                "source_binding_sha256": invocation["source_binding_sha256"],
                "prompt_sha256": invocation["prompt_sha256"],
                "attachment_id": session["attachment_id"],
                "agent_chat_id": session["agent_chat_id"],
                "session_id": session["session_id"],
                "upload_response_sha256": session["upload_response_sha256"],
                "chat_response_sha256": session["chat_response_sha256"],
                "result_response_sha256": result_response_sha256,
                "response_sha256": sha256_text(raw_text),
                "result_sha256": sha256_file(result_path),
            }
            atomic_write(
                receipts_dir / f"{task_id}.receipt.json",
                json.dumps(receipt, ensure_ascii=False, indent=2),
            )
            pending.remove(task_id)
        if pending:
            if time.monotonic() >= deadline:
                raise VisionError(
                    "VISION_RESULT_TIMEOUT", "视觉结果等待超时", {"task_ids": sorted(pending)},
                )
            time.sleep(max(1, poll_seconds))
    return {"status": "complete", "expected": len(tasks), "received": len(tasks)}


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
        "schema_version", "task_id", "source_sha256", "image_sha256", "agent_name", "agent_id",
        "transport", "source_binding", "source_binding_sha256", "prompt_sha256", "attachment_id",
        "agent_chat_id", "session_id", "upload_response_sha256", "chat_response_sha256",
        "result_response_sha256", "response_sha256", "result_sha256",
    }
    prompt = vision_agent_prompt(task, source)
    remote_hashes = {
        stage: sha256_file(response_path(receipts_dir, task_id, stage))
        for stage in ("upload", "chat", "result")
    }
    if (
        set(receipt) != keys
        or receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("task_id") != task_id
        or receipt.get("source_sha256") != task.get("source_sha256")
        or receipt.get("image_sha256") != task.get("image_sha256")
        or receipt.get("agent_name") != VISION_AGENT_NAME
        or receipt.get("agent_id") != VISION_AGENT_ID
        or receipt.get("transport") != TRANSPORT
        or receipt.get("source_binding") != source
        or receipt.get("source_binding_sha256") != sha256_text(canonical(source))
        or receipt.get("prompt_sha256") != sha256_text(prompt)
        or not isinstance(receipt.get("attachment_id"), str)
        or not REMOTE_ID.fullmatch(receipt["attachment_id"])
        or not isinstance(receipt.get("agent_chat_id"), str)
        or not REMOTE_ID.fullmatch(receipt["agent_chat_id"])
        or not isinstance(receipt.get("session_id"), str)
        or not REMOTE_ID.fullmatch(receipt["session_id"])
        or receipt.get("upload_response_sha256") != remote_hashes["upload"]
        or receipt.get("chat_response_sha256") != remote_hashes["chat"]
        or receipt.get("result_response_sha256") != remote_hashes["result"]
        or receipt.get("response_sha256") != sha256_file(result_path)
        or receipt.get("result_sha256") != sha256_file(result_path)
    ):
        raise VisionError("VISION_RECEIPT_INVALID", "视觉附件会话回执与任务不一致", {"task_id": task_id})
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
            "agent_id": receipt["agent_id"],
            "transport": receipt["transport"],
            "source_binding": receipt["source_binding"],
            "source_binding_sha256": receipt["source_binding_sha256"],
            "prompt_sha256": receipt["prompt_sha256"],
            "attachment_id": receipt["attachment_id"],
            "agent_chat_id": receipt["agent_chat_id"],
            "upload_response_sha256": receipt["upload_response_sha256"],
            "chat_response_sha256": receipt["chat_response_sha256"],
            "result_response_sha256": receipt["result_response_sha256"],
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
    transcribe_parser = subparsers.add_parser("transcribe", parents=[common])
    transcribe_parser.add_argument("--invocations", type=Path, required=True)
    transcribe_parser.add_argument("--results-dir", type=Path, required=True)
    transcribe_parser.add_argument("--receipts-dir", type=Path, required=True)
    transcribe_parser.add_argument("--lark-cli", default="lark-cli")
    transcribe_parser.add_argument("--wait-seconds", type=int, default=240)
    transcribe_parser.add_argument("--poll-seconds", type=int, default=3)
    transcribe_parser.add_argument("--command-timeout", type=int, default=120)
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
        elif args.command == "transcribe":
            output = transcribe(
                args.runtime, args.download_seal, args.tasks, args.image_root,
                args.invocations, args.results_dir, args.receipts_dir, args.lark_cli,
                args.wait_seconds, args.poll_seconds, args.command_timeout,
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
