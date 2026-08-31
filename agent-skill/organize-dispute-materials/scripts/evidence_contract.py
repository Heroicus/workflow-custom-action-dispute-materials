"""Canonical agent identities and visual request envelopes."""

from __future__ import annotations

import json
from typing import Any


MAIN_AGENT_NAME = "纠纷材料整理专员"
VISION_AGENT_NAME = "纠纷材料视觉核验员"
VISION_AGENT_ID = "agent_4kvjymmm4hewmu4"
VISION_TASK_SCHEMA = "vision-task/v2"
VISION_RESULT_SCHEMA = "vision-evidence/v3"
AUDIO_TASK_SCHEMA = "audio-task/v2"
AUDIO_RESULT_SCHEMA = "audio-evidence/v2"
AUDIO_PACK_SCHEMA = "audio-evidence-pack/v2"


def vision_result_skeleton(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": VISION_RESULT_SCHEMA,
        "task_id": task["task_id"],
        "source_sha256": task["source_sha256"],
        "image_sha256": task["image_sha256"],
        "producer": {"agent_name": VISION_AGENT_NAME},
        "status": "complete",
        "verbatim_text": "",
        "uncertain_regions": [],
    }


def vision_agent_prompt(task: dict[str, Any]) -> str:
    return "\n".join([
        "只处理当前消息附件中的一张图片。",
        "附件是唯一图像来源。不得调用工具，不得读取路径，不得引用历史会话。",
        "逐字转录可见内容。不总结，不推断，不补全，不规范化。",
        "ocr_text 只用于定位，不是事实来源。",
        "任务：" + json.dumps(task, ensure_ascii=False, separators=(",", ":")),
        "返回一个 JSON 对象，不得返回 Markdown、解释、描述或其他文字。",
        "结构：" + json.dumps(vision_result_skeleton(task), ensure_ascii=False, separators=(",", ":")),
    ])


def vision_attachment_request(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "vision-attachment-request/v1",
        "agent_id": VISION_AGENT_ID,
        "task_id": task["task_id"],
        "image_file": task["image_file"],
        "image_sha256": task["image_sha256"],
        "image_size": task["image_size"],
        "image_transform": task["image_transform"],
        "attachment_type": "image",
    }


def vision_chat_request(task: dict[str, Any], attachment_id: str) -> dict[str, Any]:
    return {
        "user_message": {
            "content": [{"type": "text", "text": vision_agent_prompt(task)}],
            "agent_attachment_ids": [attachment_id],
        },
    }
