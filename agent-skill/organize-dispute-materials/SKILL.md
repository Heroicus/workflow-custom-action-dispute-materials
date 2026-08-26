---
name: organize-dispute-materials
description: 读取指定案件记录附件，用固定模板确定性生成或重写案件报告，并将权限、链接和案件字段写回同一条 Base 记录。
license: Internal
metadata:
  version: "6.1.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

只处理运行信封指定的一条案件记录。`record_id` 是唯一定位键。

## 输入验收

仅接受 `dispute-material-run/v6.1` JSON，且必须满足：

```text
operation = process_target_record
required_skill_version = 6.1.0
app_token、table_id、record_id、dispatch_id、case_number 非空
mode = initial | supplement
```

版本或字段不符时返回 `INVALID_RUNTIME_INPUT`。不得按案件编号、附件名或历史会话搜索记录。

## 执行顺序

### 1. 读取一条记录

使用 `app_token + table_id + record_id` 精确读取当前记录，核对案件编号、案件文档和上传人。

- `initial` 处理 `attachment_ids`；
- `supplement` 只新增读取 `new_attachment_ids`，同时读取原报告作为既有结果；
- 图片和无文本层 PDF 使用 OCR；
- 单个附件失败时继续，其 ID 不进入处理基线；
- 全部附件无法读取时返回 `ATTACHMENT_READ_FAILED`。

案件事实只取自当前记录字段与附件正文。没有依据的字段保持空值。

### 2. 生成结构化事实

创建临时 `case-facts.json`：

```json
{
  "scalars": {
    "case_number": "2026-001",
    "case_type": "仲裁"
  },
  "rows": {
    "timeline_rows": [
      {
        "index": "1",
        "date": "2026-01-01",
        "fact": "材料原文事实",
        "source": "附件名称及页码",
        "note": ""
      }
    ]
  }
}
```

字段定义读取 `references/render-contract.md` 和 `references/render-schema.json`。重复事实合并；日期、金额仅统一展示格式，不改变材料含义或重新计算。

不得自行生成材料没有记载的风险、建议、缺失材料、质证意见、待办、法律条文或胜诉概率。真正互相矛盾的原文才写入 `conflict_rows`；“我方胜诉”和“驳回对方全部请求”等语义一致表述不属于矛盾。

### 3. 确定性渲染

将本次已加载的 `SKILL.md` 所在目录记为 `SKILL_ROOT`。必须执行：

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" render \
  --facts case-facts.json \
  --output report.xml
```

命令非零退出时返回其 `error_code`。禁止手写报告 XML、内联生成 XML、修改模板、复制远程模板或跳过该命令。

### 4. 创建或重写文档

`initial`：

```bash
lark-cli docs +create --as user --content @report.xml --format json > create-result.json
```

`supplement`：

```bash
lark-cli docs +update --as user \
  --doc "$existing_document_token" \
  --command overwrite \
  --content @report.xml \
  --format json > update-result.json
```

补充处理必须保留原 document token 和 URL，不创建第二份报告。禁止 append、逐表编辑或移动文档块。

### 5. 远端读回硬校验

从创建或更新结果取得当前 document token，然后执行：

```bash
lark-cli docs +fetch --as user --doc "$document_token" --format json > report-readback.json
python3 "$SKILL_ROOT/scripts/report_tool.py" validate \
  --input report-readback.json \
  --facts case-facts.json
```

必须以第二条命令零退出为通过。失败时仅允许用同一 `report.xml` 全文重写一次并重新读回；仍失败返回 `REPORT_RENDER_INVALID`。不得把未通过校验的文档链接写成成功结果。

### 6. 上传人权限

对每个 `uploader_open_ids` 执行并检查成功响应：

```bash
lark-cli drive +member-add --as user \
  --token "$document_token" --type docx \
  --member-type openid --member-id "$uploader_open_id" \
  --perm full_access --yes --format json
```

随后读回协作者：

```bash
lark-cli drive +member-list --as user \
  --token "$document_token" --type docx \
  --fields '*' --format json > permission-readback.json
```

确认每个上传人 open_id 为 `full_access`。任一上传人未通过返回 `DOC_PERMISSION_GRANT_FAILED`。

### 7. 回写并读回 Base

业务字段只写材料明确事实：

```text
案件名称
案件类型（诉讼或仲裁）
立案（收案）日期
案件状态（使用表内已有选项）
```

首次处理无事实时清空对应业务字段；补充处理无新事实时保留原值。

成功时一次写回：

```text
AI分析结果 = 报告 URL
材料处理基线 = {"document_token":"...","processed_attachment_ids":[...],"contract_version":"6.1.0"}
AI处理状态 = 已完成
执行日志/失败原因 = 任务 <dispatch_id>：已完成
```

将本次字段写入 `base-update.json`，然后执行：

```bash
lark-cli base +record-batch-update --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --json "$(cat base-update.json)" --format json
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-readback.json
```

失败时写回：

```text
AI处理状态 = 分析失败
执行日志/失败原因 = 任务 <dispatch_id>：失败：<error_code>
```

首次失败清空报告链接和基线；补充失败保留原链接和基线。写回后必须用 `record_id` 读回并逐项比对；读回不一致返回 `BASE_WRITEBACK_VERIFY_FAILED`。

## 返回

只返回：

```json
{
  "status": "completed | failed",
  "record_id": "rec...",
  "dispatch_id": "odm-v61:rec...:...",
  "processing_status": "已完成 | 分析失败",
  "report_url": "https://.../docx/...",
  "error_code": ""
}
```

只有远端报告校验、上传人权限读回、Base 同记录回写读回全部通过，才能返回 `completed`。
