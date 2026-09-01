"""Canonical agent identities and visual request envelopes."""

from __future__ import annotations

import json
from typing import Any


MAIN_AGENT_NAME = "纠纷材料整理专员"
VISION_AGENT_NAME = "纠纷材料视觉核验员"
VISION_AGENT_ID = "agent_4kvjymmm4hewmu4"
VISION_TASK_SCHEMA = "vision-task/v3"
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


def vision_agent_prompt(task: dict[str, Any], source: dict[str, Any]) -> str:
    return "\n".join([
        "你是纠纷材料视觉核验员。只处理当前消息给出的唯一 Base 记录、唯一附件和唯一视觉单元。",
        "使用飞书用户身份读取指定记录，并只下载 source.attachment_id 指定的附件。不得读取其他记录或附件。",
        "下载后核对附件字节数和 SHA-256。使用文件读取能力打开真实附件，只核验 task.source_file 与 task.unit 指定的页面、图片或嵌入对象。",
        "直接使用视觉能力读取文件。不得安装、运行或调用本地 OCR 库、OCR 命令或 Python 图像识别脚本。",
        "不得写入 Base、云文档、云盘、权限或任何业务状态。不得调用纠纷材料整理专员。不得引用历史会话。",
        "逐字转录可见内容。不总结，不推断，不补全，不规范化。",
        "ocr_text 只用于定位，不是事实来源。",
        "来源：" + json.dumps(source, ensure_ascii=False, separators=(",", ":")),
        "任务：" + json.dumps(task, ensure_ascii=False, separators=(",", ":")),
        "返回一个 JSON 对象，不得返回 Markdown、解释、描述或其他文字。",
        "uncertain_regions 每项只能包含 description、critical 和可选 source_ref。不得使用 text、context、reason 或其他字段。",
        "结构：" + json.dumps(vision_result_skeleton(task), ensure_ascii=False, separators=(",", ":")),
    ])


def vision_chat_request(task: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_message": {
            "content": [{"type": "text", "text": vision_agent_prompt(task, source)}],
        },
    }
