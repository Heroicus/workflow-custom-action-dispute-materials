#!/usr/bin/env python3
"""Transcribe audio with Feishu Minutes and bind the remote transcript to local evidence."""

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
from typing import Any, Iterator, Sequence


TASK_SCHEMA = "audio-task/v1"
RESULT_SCHEMA = "audio-evidence/v1"
PACK_SCHEMA = "audio-evidence-pack/v1"
EXPECTED_POLICY = {
    "transcriber": "Feishu Minutes",
    "identity": "user",
    "result_schema": RESULT_SCHEMA,
    "write_scope": "transcript_only",
    "all_audio_units_required": True,
}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".wma", ".amr"}
TASK_KEYS = {
    "schema_version", "task_id", "source_file", "source_sha256", "media_path",
    "media_sha256", "media_suffix", "size_bytes",
}
RESULT_KEYS = {
    "schema_version", "task_id", "source_sha256", "media_sha256", "provider",
    "status", "file_token", "minute_token", "minute_url", "transcript_path",
    "transcript_sha256", "transcript_chars", "remote_readback", "reuse_source",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^aud_[0-9a-f]{20}$")
MINUTE_TOKEN = re.compile(r"^[a-z0-9]{8,128}$")
FILE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{4,256}$")


class AudioError(Exception):
    """A stable audio-transcription contract failure."""

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
        temporary = Path(handle.name)
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AudioError("AUDIO_FILE_UNAVAILABLE", f"无法读取音频工件：{path}", {"reason": str(exc)}) from exc
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_object(path: Path, code: str = "AUDIO_JSON_INVALID") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioError(code, f"无法读取 JSON：{path}", {"reason": str(exc)}) from exc
    if not isinstance(value, dict):
        raise AudioError(code, f"JSON 根节点必须是对象：{path}")
    return value


def required_text(value: Any, label: str, code: str = "AUDIO_RESULT_INVALID") -> str:
    if not isinstance(value, str) or not value.strip():
        raise AudioError(code, f"{label} 必须是非空字符串")
    return value.strip()


def require_keys(
    value: dict[str, Any], required: set[str], allowed: set[str], label: str,
    code: str = "AUDIO_RESULT_INVALID",
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise AudioError(code, f"{label} 字段不符合契约", {"missing": missing, "unknown": unknown})


def keyed_values(value: Any, key: str) -> Iterator[Any]:
    if isinstance(value, dict):
        if key in value:
            yield value[key]
        for item in value.values():
            yield from keyed_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from keyed_values(item, key)


def first_text(value: Any, *keys: str) -> str:
    for key in keys:
        for item in keyed_values(value, key):
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def load_tasks(path: Path) -> list[dict[str, Any]]:
    root = read_object(path, "AUDIO_TASKS_INVALID")
    require_keys(
        root, {"schema_version", "policy", "tasks", "summary"},
        {"schema_version", "policy", "tasks", "summary"}, "音频任务清单", "AUDIO_TASKS_INVALID",
    )
    if root.get("schema_version") != TASK_SCHEMA or root.get("policy") != EXPECTED_POLICY:
        raise AudioError("AUDIO_TASKS_INVALID", "音频任务清单版本或转写策略不正确")
    raw_tasks = root.get("tasks")
    if not isinstance(raw_tasks, list):
        raise AudioError("AUDIO_TASKS_INVALID", "音频任务 tasks 必须是数组")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise AudioError("AUDIO_TASKS_INVALID", f"tasks[{index}] 必须是对象")
        require_keys(item, TASK_KEYS, TASK_KEYS, f"tasks[{index}]", "AUDIO_TASKS_INVALID")
        task_id = required_text(item.get("task_id"), f"tasks[{index}].task_id", "AUDIO_TASKS_INVALID")
        source_hash = required_text(item.get("source_sha256"), f"tasks[{index}].source_sha256", "AUDIO_TASKS_INVALID")
        media_hash = required_text(item.get("media_sha256"), f"tasks[{index}].media_sha256", "AUDIO_TASKS_INVALID")
        suffix = required_text(item.get("media_suffix"), f"tasks[{index}].media_suffix", "AUDIO_TASKS_INVALID").lower()
        if item.get("schema_version") != TASK_SCHEMA or not TASK_ID.fullmatch(task_id) or task_id in seen:
            raise AudioError("AUDIO_TASKS_INVALID", "音频任务版本、ID 格式或唯一性不正确", {"task_id": task_id})
        if not HEX64.fullmatch(source_hash) or not HEX64.fullmatch(media_hash) or suffix not in AUDIO_SUFFIXES:
            raise AudioError("AUDIO_TASKS_INVALID", "音频任务哈希或格式不正确", {"task_id": task_id})
        size = item.get("size_bytes")
        if type(size) is not int or size <= 0 or size > 6 * 1024 * 1024 * 1024:
            raise AudioError("AUDIO_LIMIT_INVALID", "音频大小必须在 0 到 6GB 之间", {"task_id": task_id, "size": size})
        media = Path(required_text(item.get("media_path"), f"tasks[{index}].media_path", "AUDIO_TASKS_INVALID"))
        if not media.is_file() or media.stat().st_size != size or sha256_file(media) != media_hash:
            raise AudioError("AUDIO_FILE_CHANGED", "音频文件不存在、大小或哈希不一致", {"task_id": task_id})
        seen.add(task_id)
        tasks.append(item)
    if root.get("summary") != {"total": len(tasks), "pending": len(tasks)}:
        raise AudioError("AUDIO_TASKS_INVALID", "音频任务统计与任务数量不一致")
    return tasks


def parse_json_output(source: str, label: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    cursor = 0
    while cursor < len(source):
        match = re.search(r"[\[{]", source[cursor:])
        if not match:
            break
        start = cursor + match.start()
        try:
            value, end = decoder.raw_decode(source, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        candidates.append(value)
        cursor = end
    for value in reversed(candidates):
        if isinstance(value, dict):
            return value
    raise AudioError("AUDIO_REMOTE_RESPONSE_INVALID", f"{label} 没有返回 JSON 对象")


def remote_message(value: dict[str, Any]) -> str:
    error = value.get("error")
    if isinstance(error, dict):
        return " ".join(str(error.get(key) or "") for key in ("type", "subtype", "code", "message", "hint")).strip()
    return str(value.get("message") or value.get("msg") or value)


def remote_codes(value: Any) -> set[str]:
    """Collect structured and embedded numeric API error codes."""

    codes: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"code", "error_code"} and isinstance(item, (str, int)):
                codes.add(str(item).strip())
            if key == "error" and isinstance(item, str):
                codes.update(re.findall(r"(?<!\d)\d{6,12}(?!\d)", item))
            codes.update(remote_codes(item))
    elif isinstance(value, list):
        for item in value:
            codes.update(remote_codes(item))
    return {code for code in codes if code}


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
        raise AudioError("AUDIO_REMOTE_COMMAND_FAILED", f"{label} 命令执行失败", {"reason": str(exc)}) from exc
    raw = completed.stdout.strip() or completed.stderr.strip()
    value = parse_json_output(raw, label)
    if completed.returncode != 0 or value.get("ok") is not True:
        raise AudioError(
            "AUDIO_REMOTE_OPERATION_FAILED", f"{label} 返回失败",
            {
                "returncode": completed.returncode,
                "message": remote_message(value),
                "remote_codes": sorted(remote_codes(value)),
            },
        )
    if value.get("identity") != "user":
        raise AudioError("AUDIO_IDENTITY_MISMATCH", f"{label} 未使用飞书用户身份")
    return value, raw


def valid_file_token(value: str) -> bool:
    return bool(FILE_TOKEN.fullmatch(value))


def valid_minute_token(value: str) -> bool:
    return bool(MINUTE_TOKEN.fullmatch(value))


def minute_from_url(value: str) -> str:
    match = re.search(r"/minutes/([a-z0-9]{8,128})(?:[/?#]|$)", value)
    return match.group(1) if match else ""


def response_path(receipts_dir: Path, task_id: str, stage: str) -> Path:
    return receipts_dir / "raw" / f"{task_id}.{stage}.json"


def write_remote_response(path: Path, raw: str) -> str:
    atomic_write(path, raw.rstrip() + "\n")
    return sha256_file(path)


def walk_records(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_records(item)


def meaningful_error(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def load_saved_response(path: Path, expected_hash: str, label: str) -> dict[str, Any]:
    if not path.is_file() or sha256_file(path) != expected_hash:
        raise AudioError("AUDIO_REMOTE_READBACK_CHANGED", f"{label}响应不存在或哈希不一致")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AudioError("AUDIO_REMOTE_READBACK_CHANGED", f"无法读取{label}响应", {"reason": str(exc)}) from exc
    response = parse_json_output(raw, label)
    if response.get("ok") is not True or response.get("identity") != "user":
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", f"{label}响应不是用户身份成功结果")
    return response


def safe_transcript_path(value: str, cwd: Path, allowed_root: Path) -> Path | None:
    if not value:
        return None
    candidate = Path(value).expanduser()
    candidate = (cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        candidate.relative_to(allowed_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def locate_transcript(response: dict[str, Any], cwd: Path, output_root: Path) -> Path | None:
    for value in keyed_values(response, "transcript_file"):
        if isinstance(value, str):
            path = safe_transcript_path(value, cwd, output_root)
            if path:
                return path
    return None


def validate_saved_upload_semantics(result: dict[str, Any], receipts_dir: Path) -> None:
    task_id = str(result["task_id"])
    file_token = str(result["file_token"])
    minute_token = str(result["minute_token"])
    minute_url = str(result["minute_url"])
    remote = result["remote_readback"]

    drive = load_saved_response(
        response_path(receipts_dir, task_id, "drive"),
        str(remote["drive_upload_response_sha256"]), "云空间上传",
    )
    drive_tokens = {
        item.strip() for item in keyed_values(drive, "file_token")
        if isinstance(item, str) and item.strip()
    }
    if drive_tokens != {file_token}:
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", "云空间上传响应与回执 file_token 不一致", {"task_id": task_id})

    minute = load_saved_response(
        response_path(receipts_dir, task_id, "minute"),
        str(remote["minute_upload_response_sha256"]), "妙记生成",
    )
    minute_tokens = {
        item.strip() for item in keyed_values(minute, "minute_token")
        if isinstance(item, str) and item.strip()
    }
    if minute_tokens != {minute_token}:
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", "妙记生成响应与回执 minute_token 不一致", {"task_id": task_id})
    response_url = first_text(minute, "minute_url", "url")
    if response_url != minute_url or (minute_url and minute_from_url(minute_url) != minute_token):
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", "妙记生成响应 URL 与回执不一致", {"task_id": task_id})


def validate_saved_remote_semantics(
    result: dict[str, Any], receipts_dir: Path, transcripts_dir: Path, transcript: Path,
) -> None:
    validate_saved_upload_semantics(result, receipts_dir)
    task_id = str(result["task_id"])
    minute_token = str(result["minute_token"])
    remote = result["remote_readback"]
    detail = load_saved_response(
        response_path(receipts_dir, task_id, "detail"),
        str(remote["minute_detail_response_sha256"]), "妙记逐字稿读回",
    )
    minute_items = [
        item for item in walk_records(detail)
        if isinstance(item.get("minute_token"), str) and item["minute_token"].strip()
    ]
    returned_tokens = {str(item["minute_token"]).strip() for item in minute_items}
    if returned_tokens != {minute_token}:
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", "逐字稿响应未唯一绑定目标 minute_token", {"task_id": task_id})
    target_items = [item for item in minute_items if str(item["minute_token"]).strip() == minute_token]
    if any(meaningful_error(item.get("error")) for item in target_items):
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", "逐字稿响应中的目标妙记仍返回错误", {"task_id": task_id})
    cwd = transcripts_dir.resolve().parent
    resolved_transcripts = {
        path.resolve()
        for item in target_items
        for value in keyed_values(item, "transcript_file")
        if isinstance(value, str)
        for path in [safe_transcript_path(value, cwd, transcripts_dir)]
        if path is not None
    }
    if resolved_transcripts != {transcript.resolve()}:
        raise AudioError("AUDIO_REMOTE_READBACK_INVALID", "逐字稿响应路径与回执不一致", {"task_id": task_id})


def retryable_remote_error(error: AudioError) -> bool:
    text = json.dumps(error.details, ensure_ascii=False).lower()
    codes = error.details.get("remote_codes")
    if isinstance(codes, list) and "2091003" in {str(code) for code in codes}:
        return True
    return any(token in text for token in (
        "not ready", "processing", "generating", "transcrib", "rate limit", "too many",
        "temporar", "timeout", " 429 ", "resource not ready", "尚未准备", "暂未完成",
        "处理中", "未生成", "生成中", "转写中", "请求过于频繁",
    ))


def fetch_transcript(
    cli: str, minute_token: str, task_id: str, receipts_dir: Path, transcripts_dir: Path,
    wait_seconds: int, poll_seconds: int, command_timeout: int,
) -> tuple[Path, str]:
    transcripts_dir = transcripts_dir.resolve()
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    cwd = transcripts_dir.parent
    task_output = transcripts_dir / task_id
    task_output.mkdir(parents=True, exist_ok=True)
    relative_output = task_output.relative_to(cwd)
    deadline = time.monotonic() + wait_seconds
    last_error = "逐字稿尚未生成"
    while True:
        try:
            response, raw = run_lark(
                cli,
                [
                    "minutes", "+detail", "--as", "user", "--minute-tokens", minute_token,
                    "--transcript", "--overwrite", "--output-dir", str(relative_output), "--json",
                ],
                cwd, command_timeout, "妙记逐字稿读回",
            )
            returned_tokens = {
                item.strip() for item in keyed_values(response, "minute_token")
                if isinstance(item, str) and item.strip()
            }
            if minute_token not in returned_tokens:
                raise AudioError(
                    "AUDIO_REMOTE_RESPONSE_INVALID", "妙记逐字稿读回未返回目标 minute_token",
                    {"task_id": task_id, "minute_token": minute_token},
                )
            transcript = locate_transcript(response, cwd, task_output)
            if transcript and transcript.stat().st_size > 0:
                return transcript, write_remote_response(response_path(receipts_dir, task_id, "detail"), raw)
            last_error = "妙记接口成功但没有返回非空逐字稿"
        except AudioError as exc:
            if not retryable_remote_error(exc):
                raise
            last_error = str(exc.details.get("message") or exc)
        if time.monotonic() >= deadline:
            raise AudioError(
                "AUDIO_TRANSCRIPTION_TIMEOUT", "等待飞书妙记逐字稿超时",
                {"task_id": task_id, "minute_token": minute_token, "last_error": last_error},
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def load_partial_state(path: Path, task: dict[str, Any]) -> dict[str, str]:
    if not path.is_file():
        return {}
    state = read_object(path, "AUDIO_STATE_INVALID")
    if (
        state.get("schema_version") != "audio-state/v1"
        or state.get("task_id") != task.get("task_id")
        or state.get("media_sha256") != task.get("media_sha256")
    ):
        raise AudioError("AUDIO_STATE_INVALID", "音频重试状态与当前任务不一致", {"task_id": task.get("task_id")})
    file_token = str(state.get("file_token") or "").strip()
    minute_token = str(state.get("minute_token") or "").strip()
    minute_url = str(state.get("minute_url") or "").strip()
    return {
        "file_token": file_token if valid_file_token(file_token) else "",
        "minute_token": minute_token if valid_minute_token(minute_token) else "",
        "minute_url": minute_url,
    }


def write_partial_state(path: Path, task: dict[str, Any], values: dict[str, str]) -> None:
    atomic_write(path, json.dumps({
        "schema_version": "audio-state/v1",
        "task_id": task["task_id"],
        "media_sha256": task["media_sha256"],
        **values,
    }, ensure_ascii=False, indent=2))


def build_receipt(
    task: dict[str, Any], values: dict[str, str], transcript: Path,
    detail_sha256: str, reuse_source: str, drive_sha256: str, upload_sha256: str,
) -> dict[str, Any]:
    transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
    if len(re.sub(r"\s+", "", transcript_text)) < 2:
        raise AudioError("AUDIO_TRANSCRIPT_EMPTY", "飞书妙记逐字稿为空", {"task_id": task["task_id"]})
    return {
        "schema_version": RESULT_SCHEMA,
        "task_id": task["task_id"],
        "source_sha256": task["source_sha256"],
        "media_sha256": task["media_sha256"],
        "provider": {"service": "Feishu Minutes", "identity": "user", "mode": "remote_transcript_readback"},
        "status": "complete",
        "file_token": values.get("file_token", ""),
        "minute_token": values["minute_token"],
        "minute_url": values.get("minute_url", ""),
        "transcript_path": str(transcript.resolve()),
        "transcript_sha256": sha256_file(transcript),
        "transcript_chars": len(re.sub(r"\s+", "", transcript_text)),
        "remote_readback": {
            "drive_upload_response_sha256": drive_sha256,
            "minute_upload_response_sha256": upload_sha256,
            "minute_detail_response_sha256": detail_sha256,
        },
        "reuse_source": reuse_source,
    }


def transcribe(
    tasks_path: Path, receipts_dir: Path, transcripts_dir: Path,
    cli: str, wait_seconds: int, poll_seconds: int, command_timeout: int,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    if not tasks:
        return {
            "status": "complete", "expected": 0, "received": 0,
            "new_uploads": 0, "receipt_refresh": 0, "same_run_reuse": 0,
        }
    executable = shutil.which(cli) if os.sep not in cli else cli
    if not executable:
        raise AudioError("LARK_CLI_UNAVAILABLE", "未找到 lark-cli")
    receipts_dir.mkdir(parents=True, exist_ok=True)
    completed_by_hash: dict[str, dict[str, Any]] = {}
    new_uploads = 0
    receipt_refresh = 0
    same_run_reuse = 0
    for task in tasks:
        task_id = str(task["task_id"])
        media_hash = str(task["media_sha256"])
        receipt_path = receipts_dir / f"{task_id}.receipt.json"
        if media_hash in completed_by_hash:
            shared = completed_by_hash[media_hash]
            shared_task_id = str(shared["task_id"])
            for stage in ("drive", "minute", "detail"):
                source = response_path(receipts_dir, shared_task_id, stage)
                destination = response_path(receipts_dir, task_id, stage)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            receipt = dict(shared)
            receipt.update({
                "task_id": task_id,
                "source_sha256": task["source_sha256"],
                "media_sha256": media_hash,
                "reuse_source": "same_run",
            })
            atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
            validate_receipt(task, receipt, receipts_dir, transcripts_dir)
            same_run_reuse += 1
            continue

        state_path = receipts_dir / f"{task_id}.state.json"
        drive_sha256: str | None = None
        upload_sha256: str | None = None
        if receipt_path.is_file():
            existing = validate_receipt_header(task, read_object(receipt_path))
            validate_saved_upload_semantics(existing, receipts_dir)
            values = {
                "file_token": str(existing["file_token"]),
                "minute_token": str(existing["minute_token"]),
                "minute_url": str(existing["minute_url"]),
            }
            remote = existing["remote_readback"]
            drive_sha256 = str(remote["drive_upload_response_sha256"])
            upload_sha256 = str(remote["minute_upload_response_sha256"])
            reuse_source = "receipt_refresh"
            receipt_refresh += 1
        else:
            values = load_partial_state(state_path, task)
            reuse_source = "retry_state" if values else "new_upload"
        if not values.get("minute_token"):
            if not values.get("file_token"):
                media = Path(str(task["media_path"])).resolve()
                drive_response, drive_raw = run_lark(
                    str(executable), ["drive", "+upload", "--as", "user", "--file", media.name, "--json"],
                    media.parent, command_timeout, "音频上传到云空间",
                )
                file_token = first_text(drive_response, "file_token")
                if not valid_file_token(file_token):
                    raise AudioError("AUDIO_REMOTE_RESPONSE_INVALID", "云空间上传响应缺少 file_token", {"task_id": task_id})
                values["file_token"] = file_token
                drive_sha256 = write_remote_response(response_path(receipts_dir, task_id, "drive"), drive_raw)
                write_partial_state(state_path, task, values)
            minute_response, minute_raw = run_lark(
                str(executable), ["minutes", "+upload", "--as", "user", "--file-token", values["file_token"], "--json"],
                receipts_dir.resolve(), command_timeout, "音频生成飞书妙记",
            )
            minute_url = first_text(minute_response, "minute_url", "url")
            minute_token = first_text(minute_response, "minute_token") or minute_from_url(minute_url)
            if not valid_minute_token(minute_token):
                raise AudioError("AUDIO_REMOTE_RESPONSE_INVALID", "妙记上传响应缺少 minute_token", {"task_id": task_id})
            if minute_url and minute_from_url(minute_url) != minute_token:
                raise AudioError("AUDIO_REMOTE_RESPONSE_INVALID", "妙记上传响应的 URL 与 token 不一致", {"task_id": task_id})
            values.update({"minute_token": minute_token, "minute_url": minute_url})
            upload_sha256 = write_remote_response(response_path(receipts_dir, task_id, "minute"), minute_raw)
            write_partial_state(state_path, task, values)
            new_uploads += 1
        transcript, detail_sha256 = fetch_transcript(
            str(executable), values["minute_token"], task_id, receipts_dir, transcripts_dir,
            wait_seconds, poll_seconds, command_timeout,
        )
        drive_path = response_path(receipts_dir, task_id, "drive")
        minute_path = response_path(receipts_dir, task_id, "minute")
        drive_sha256 = drive_sha256 or (sha256_file(drive_path) if drive_path.is_file() else None)
        upload_sha256 = upload_sha256 or (sha256_file(minute_path) if minute_path.is_file() else None)
        if not values.get("file_token") or drive_sha256 is None or upload_sha256 is None:
            raise AudioError(
                "AUDIO_REMOTE_READBACK_INVALID", "音频任务缺少云盘上传或妙记生成原始响应",
                {"task_id": task_id},
            )
        receipt = build_receipt(
            task, values, transcript, detail_sha256, reuse_source, drive_sha256, upload_sha256,
        )
        atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
        state_path.unlink(missing_ok=True)
        completed_by_hash[media_hash] = validate_receipt(task, receipt, receipts_dir, transcripts_dir)
    return {
        "status": "complete",
        "expected": len(tasks),
        "received": len(tasks),
        "new_uploads": new_uploads,
        "receipt_refresh": receipt_refresh,
        "same_run_reuse": same_run_reuse,
    }


def validate_receipt_header(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    require_keys(result, RESULT_KEYS, RESULT_KEYS, str(task["task_id"]))
    task_id = str(task["task_id"])
    if (
        result.get("schema_version") != RESULT_SCHEMA
        or result.get("task_id") != task_id
        or result.get("source_sha256") != task.get("source_sha256")
        or result.get("media_sha256") != task.get("media_sha256")
        or result.get("status") != "complete"
    ):
        raise AudioError("AUDIO_RESULT_INVALID", "音频结果版本、任务、哈希或状态不正确", {"task_id": task_id})
    provider = result.get("provider")
    if provider != {"service": "Feishu Minutes", "identity": "user", "mode": "remote_transcript_readback"}:
        raise AudioError("AUDIO_PROVIDER_MISMATCH", "音频结果不是飞书妙记用户身份远端读回", {"task_id": task_id})
    file_token = str(result.get("file_token") or "")
    minute_token = str(result.get("minute_token") or "")
    minute_url = str(result.get("minute_url") or "")
    if not valid_file_token(file_token):
        raise AudioError("AUDIO_RESULT_INVALID", "音频结果 file_token 格式不正确", {"task_id": task_id})
    if not valid_minute_token(minute_token):
        raise AudioError("AUDIO_RESULT_INVALID", "音频结果 minute_token 格式不正确", {"task_id": task_id})
    if minute_url and minute_from_url(minute_url) != minute_token:
        raise AudioError("AUDIO_RESULT_INVALID", "妙记 URL 与 minute_token 不一致", {"task_id": task_id})
    remote = result.get("remote_readback")
    if not isinstance(remote, dict) or set(remote) != {
        "drive_upload_response_sha256", "minute_upload_response_sha256", "minute_detail_response_sha256",
    }:
        raise AudioError("AUDIO_RESULT_INVALID", "音频远端响应哈希结构不正确", {"task_id": task_id})
    reuse_source = result.get("reuse_source")
    if reuse_source not in {"new_upload", "same_run", "retry_state", "receipt_refresh"}:
        raise AudioError("AUDIO_RESULT_INVALID", "音频复用来源不正确", {"task_id": task_id})
    for key in (
        "drive_upload_response_sha256", "minute_upload_response_sha256", "minute_detail_response_sha256",
    ):
        value = remote.get(key)
        if not isinstance(value, str) or not HEX64.fullmatch(value):
            raise AudioError("AUDIO_RESULT_INVALID", "音频远端响应哈希格式不正确", {"task_id": task_id, "field": key})
    return result


def validate_receipt(
    task: dict[str, Any], result: dict[str, Any], receipts_dir: Path, transcripts_dir: Path,
) -> dict[str, Any]:
    result = validate_receipt_header(task, result)
    task_id = str(task["task_id"])
    transcript = Path(required_text(result.get("transcript_path"), f"{task_id}.transcript_path")).resolve()
    try:
        transcript.relative_to(transcripts_dir.resolve())
    except ValueError as exc:
        raise AudioError("AUDIO_RESULT_INVALID", "音频逐字稿路径不在本次输出目录", {"task_id": task_id}) from exc
    if not transcript.is_file() or sha256_file(transcript) != result.get("transcript_sha256"):
        raise AudioError("AUDIO_TRANSCRIPT_CHANGED", "音频逐字稿不存在或哈希不一致", {"task_id": task_id})
    transcript_chars = result.get("transcript_chars")
    if type(transcript_chars) is not int or transcript_chars < 2:
        raise AudioError("AUDIO_TRANSCRIPT_EMPTY", "音频逐字稿没有可用正文", {"task_id": task_id})
    validate_saved_remote_semantics(result, receipts_dir, transcripts_dir, transcript)
    return result


def reconcile(
    tasks_path: Path, receipts_dir: Path, transcripts_dir: Path, source_corpus_path: Path,
    output_corpus_path: Path, evidence_path: Path,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    try:
        source_corpus = source_corpus_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AudioError("SOURCE_CORPUS_UNAVAILABLE", "无法读取视觉核验后的材料语料", {"reason": str(exc)}) from exc
    results: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    transcripts: list[str] = []
    for task in tasks:
        task_id = str(task["task_id"])
        path = receipts_dir / f"{task_id}.receipt.json"
        if not path.is_file():
            unresolved.append({"task_id": task_id, "reason": "missing_receipt"})
            continue
        result = validate_receipt(task, read_object(path), receipts_dir, transcripts_dir)
        transcript_path = Path(str(result["transcript_path"]))
        transcript = transcript_path.read_text(encoding="utf-8", errors="replace").strip()
        transcripts.append(f"=== 音频逐字稿[sha256={task['source_sha256']}]：{task['source_file']} ===\n{transcript}")
        results.append({
            "task_id": task_id,
            "source_file": task["source_file"],
            "source_sha256": task["source_sha256"],
            "media_sha256": task["media_sha256"],
            "provider": result["provider"],
            "status": "complete",
            "file_token": result["file_token"],
            "minute_token": result["minute_token"],
            "minute_url": result["minute_url"],
            "transcript_path": result["transcript_path"],
            "transcript_sha256": result["transcript_sha256"],
            "transcript_chars": result["transcript_chars"],
            "remote_readback": result["remote_readback"],
            "reuse_source": result["reuse_source"],
        })
    verified = source_corpus.rstrip() + "\n\n" + "\n\n".join(transcripts) + "\n" if transcripts else source_corpus
    evidence = {
        "schema_version": PACK_SCHEMA,
        "policy": {
            "provider": "Feishu Minutes",
            "identity": "user",
            "remote_transcript_readback": True,
            "main_writer": "Deepseek-V4-Pro",
            "single_writer": True,
        },
        "tasks": results,
        "unresolved": unresolved,
        "artifacts": {
            "audio_tasks_sha256": sha256_file(tasks_path),
            "input_corpus_sha256": sha256_text(source_corpus),
            "verified_corpus_sha256": sha256_text(verified) if not unresolved else None,
        },
        "summary": {
            "expected": len(tasks),
            "received": len(results),
            "complete": len(results),
            "failed": len(unresolved),
            "reused": sum(result["reuse_source"] != "new_upload" for result in results),
        },
    }
    atomic_write(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2))
    if unresolved:
        raise AudioError("AUDIO_EVIDENCE_UNRESOLVED", "音频逐字稿未全部完成远端读回", {"items": unresolved[:20]})
    atomic_write(output_corpus_path, verified)
    return {
        "status": "valid", **evidence["summary"],
        "output_corpus": str(output_corpus_path.resolve()), "evidence": str(evidence_path.resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe and verify dispute audio with Feishu Minutes.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    upload = subparsers.add_parser("transcribe")
    upload.add_argument("--tasks", type=Path, required=True)
    upload.add_argument("--receipts-dir", type=Path, required=True)
    upload.add_argument("--transcripts-dir", type=Path, required=True)
    upload.add_argument("--lark-cli", default="lark-cli")
    upload.add_argument("--wait-seconds", type=int, default=7200)
    upload.add_argument("--poll-seconds", type=int, default=15)
    upload.add_argument("--command-timeout", type=int, default=180)
    verify = subparsers.add_parser("reconcile")
    verify.add_argument("--tasks", type=Path, required=True)
    verify.add_argument("--receipts-dir", type=Path, required=True)
    verify.add_argument("--transcripts-dir", type=Path, required=True)
    verify.add_argument("--source-corpus", type=Path, required=True)
    verify.add_argument("--output-corpus", type=Path, required=True)
    verify.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "transcribe":
            if args.wait_seconds < 1 or args.poll_seconds < 1 or args.command_timeout < 1:
                raise AudioError("AUDIO_ARGUMENT_INVALID", "等待、轮询和命令超时必须为正整数")
            output = transcribe(
                args.tasks, args.receipts_dir, args.transcripts_dir,
                args.lark_cli, args.wait_seconds, args.poll_seconds, args.command_timeout,
            )
        else:
            output = reconcile(
                args.tasks, args.receipts_dir, args.transcripts_dir, args.source_corpus,
                args.output_corpus, args.evidence,
            )
        print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except AudioError as exc:
        print(json.dumps({
            "status": "invalid", "error_code": exc.code, "message": str(exc), "details": exc.details,
        }, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
