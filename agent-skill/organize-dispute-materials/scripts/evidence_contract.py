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
    task_identity = {
        "schema_version": task["schema_version"],
        "task_id": task["task_id"],
        "source_file": task["source_file"],
        "source_sha256": task["source_sha256"],
        "unit": task["unit"],
        "page": task["page"],
        "image_sha256": task["image_sha256"],
    }
    source_identity = {
        "schema_version": source["schema_version"],
        "record_id": source["record_id"],
        "attachment_field_name": source["attachment_field_name"],
        "attachment_id": source["attachment_id"],
        "attachment_name": source["attachment_name"],
        "attachment_sha256": source["attachment_sha256"],
        "source_locator": source["source_locator"],
    }
    return "\n".join([
        "你是纠纷材料视觉核验员。当前消息只包含一个真实图片附件，它就是本次唯一视觉单元。",
        "直接读取当前消息附件。不得重新读取 Base，不得下载其他文件，不得调用任何工具或其他智能体。",
        "按图片从上到下、从左到右逐字抄写实际可见文字。保持原字符，不总结、不解释、不重排、不归类、不补全、不规范化。",
        "原图未出现的标题、标签、括号、单位、说明和占位词不得添加。空白区域直接跳过，严禁写成无、未填写或其他推断值。",
        "无法逐字确认的字符不得猜测。写入 uncertain_regions，并将 status 设为 partial。",
        "逐字符核对姓名、机构、案号、日期、金额、利率、账号、验证码和印章代码。",
        "来源身份：" + json.dumps(source_identity, ensure_ascii=False, separators=(",", ":")),
        "任务身份：" + json.dumps(task_identity, ensure_ascii=False, separators=(",", ":")),
        "返回一个 JSON 对象，不得返回 Markdown、解释、描述或其他文字。",
        "uncertain_regions 每项只能包含 description、critical 和可选 source_ref。不得使用 text、context、reason 或其他字段。",
        "结构：" + json.dumps(vision_result_skeleton(task), ensure_ascii=False, separators=(",", ":")),
    ])
