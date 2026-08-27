---
name: organize-dispute-materials
description: 读取小组件指定的一条案件记录，从全部附件提取事实，生成固定格式报告，并回写同一条 Base 记录。
license: Internal
metadata:
  version: "6.3.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

## 输入

只接受 `dispute-material-run/v6.3` JSON：

```text
operation = process_target_record
required_skill_version = 6.3.0
app_token、table_id、record_id、dispatch_id、case_number 非空
mode = initial | supplement
model_contract = Deepseek-V4-Pro 主写入 + Doubao-Seed-2.1-turbo 只读视觉
```

`record_id` 是唯一定位键。保存完整信封为 `runtime.json`；不搜索其他记录。

## 执行

### 1. 读取记录和全部附件

```bash
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-readback.json

mkdir -p materials extracted/text extracted/vision-pages extracted/vision-results
lark-cli base +record-download-attachment --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --output materials --overwrite \
  --format json > attachment-download.json

python3 "$SKILL_ROOT/scripts/material_tool.py" extract \
  --input-dir materials \
  --output-dir extracted/text \
  --manifest extracted/material-manifest.json \
  --corpus extracted/source-corpus.txt \
  --vision-dir extracted/vision-pages \
  --vision-tasks extracted/vision-tasks.json
```

初次与补充处理都读取当前记录的全部附件，确保重写报告时不丢失旧事实。图片和无文本层 PDF 先以 Tesseract OCR，PDF 页固定按 300 DPI 渲染；ZIP 逐文件展开。所有独立图片、无文字层 PDF 页面和 Office 嵌入图片同时形成 `vision-task/v1` 任务。

### 2. 调用只读视觉子智能体

主智能体固定使用 Deepseek-V4-Pro，并且是唯一事实合并者和业务写入者。逐项读取 `extracted/vision-tasks.json`，对每个任务：

1. 调用已配置的 `纠纷材料视觉核验员` 子智能体；
2. 必须同时传入任务 JSON 和 `image_path` 指向的原始图片，不能只传 OCR 文本；
3. 子智能体必须固定使用 Doubao-Seed-2.1-turbo；
4. 子智能体只逐字转录图片并返回 `vision-evidence/v1` JSON，不得填写事实、生成报告或读写飞书；
5. 把纯 JSON 保存为 `extracted/vision-results/<task_id>.json`。

输出契约见 `references/vision-contract.md` 和 `references/vision-result-schema.json`。全部任务完成后执行：

```bash
python3 "$SKILL_ROOT/scripts/vision_tool.py" reconcile \
  --tasks extracted/vision-tasks.json \
  --results-dir extracted/vision-results \
  --source-corpus extracted/source-corpus.txt \
  --output-corpus extracted/verified-source-corpus.txt \
  --evidence extracted/vision-evidence.json
```

没有视觉任务时也必须运行该命令，它会生成零任务证据包并复制语料。缺少子智能体结果、哈希不一致、模型不符或仍有看不清的关键字段时立即失败；主智能体不得猜测或绕过。

### 3. 填写完整事实脚手架

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" scaffold \
  --case-number "$case_number" \
  --manifest extracted/material-manifest.json \
  --output case-facts.json
```

在脚手架上填写，不重建精简版 JSON：

- 事实只来自 `extracted/verified-source-corpus.txt`；
- 有明确依据就填写，没有依据保持空值；
- 不删除 `evidence_rows`、`completeness_rows`、`quality_rows` 中的材料项；
- 裁决书或判决书已经记载结果时，同时填写第九章、`case_status` 和 `base_fields.case_status`；
- `base_fields` 填写案件名称、案件类型、立案日期、案件状态；无依据字段留空。

验证：

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" validate-facts \
  --facts case-facts.json \
  --source-corpus extracted/verified-source-corpus.txt \
  --manifest extracted/material-manifest.json \
  --vision-evidence extracted/vision-evidence.json \
  --vision-tasks extracted/vision-tasks.json
```

非零退出时修正事实；不放宽或跳过校验。

### 4. 渲染和创建文档

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" render \
  --facts case-facts.json --output report.xml
```

`initial` 创建文档：

```bash
lark-cli docs +create --as user \
  --content @report.xml --format json > document-write.json
```

`supplement` 全文重写原文档：

```bash
lark-cli docs +update --as user \
  --doc "$existing_document_token" --command overwrite \
  --content @report.xml --format json > document-write.json
```

补充处理保留原 document token 和 URL，不创建第二份报告。

### 5. 文档读回

```bash
lark-cli docs +fetch --as user \
  --doc "$document_token" --format json > report-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate \
  --input report-readback.json --facts case-facts.json
```

失败时只允许用同一 `report.xml` 全文重写一次后重新读回。

### 6. 上传人权限

对每个 `uploader_open_ids` 执行：

```bash
lark-cli drive +member-add --as user \
  --token "$document_token" --type docx \
  --member-type openid --member-id "$uploader_open_id" \
  --perm full_access --yes --format json > permission-add.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-permission \
  --input permission-add.json --member-id "$uploader_open_id"
```

当前运行时不提供协作者列表命令，因此以添加接口返回的精确 open_id 和 `full_access` 作为成功门。

### 7. 回写 Base 并读回

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" build-writeback \
  --runtime runtime.json \
  --facts case-facts.json \
  --manifest extracted/material-manifest.json \
  --vision-evidence extracted/vision-evidence.json \
  --vision-tasks extracted/vision-tasks.json \
  --source-corpus extracted/verified-source-corpus.txt \
  --document-token "$document_token" \
  --report-url "$report_url" \
  --record-readback record-readback.json \
  --output base-update.json \
  --expectation base-expectation.json

lark-cli base +record-batch-update --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --json "$(cat base-update.json)" --format json > base-write.json

lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-final-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-writeback \
  --input base-final-readback.json \
  --expectation base-expectation.json
```

## 失败

任一成功门失败时，用原始错误码构建一次失败回写，再读回验证：

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" build-failure \
  --runtime runtime.json --error-code "$error_code" \
  --output base-update.json --expectation base-expectation.json
```

`initial` 失败清空链接和基线；`supplement` 失败保留原链接和基线。

## 返回

只有事实校验、文档读回、上传人权限、Base 八个字段读回全部通过，才返回：

```json
{
  "status": "completed",
  "record_id": "rec...",
  "dispatch_id": "odm-v63:rec...:...",
  "processing_status": "已完成",
  "report_url": "https://.../docx/...",
  "error_code": ""
}
```

否则返回 `status=failed`、`processing_status=分析失败` 和实际 `error_code`。
