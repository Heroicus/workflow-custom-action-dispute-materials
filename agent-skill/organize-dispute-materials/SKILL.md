---
name: organize-dispute-materials
description: 读取小组件指定的一条案件记录，从全部附件提取事实，生成固定格式报告，并回写同一条 Base 记录。
license: Internal
metadata:
  version: "6.5.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

## 输入

只接受 `dispute-material-run/v6.5` JSON：

```text
operation = process_target_record
required_skill_version = 6.5.0
app_token、table_id、record_id、dispatch_id、case_number 非空
mode = initial | supplement
model_contract = Deepseek-V4-Pro 主写入 + Doubao-Seed-2.1-turbo 只读视觉 + Feishu Minutes 音频逐字稿
```

`record_id` 是唯一定位键。保存完整信封为 `runtime.json`；不搜索其他记录。

## 执行

### 1. 读取记录和全部附件

```bash
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-readback.json

mkdir -p materials extracted/text extracted/vision-pages extracted/vision-results \
  extracted/audio-files extracted/audio-receipts extracted/audio-transcripts
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
  --vision-tasks extracted/vision-tasks.json \
  --audio-dir extracted/audio-files \
  --audio-tasks extracted/audio-tasks.json
```

初次与补充处理都读取当前记录的全部附件，确保重写报告时不丢失旧事实。图片和无文本层 PDF 先以 Tesseract OCR，PDF 页固定按 300 DPI 渲染；ZIP 逐文件展开。所有独立图片、无文字层 PDF 页面和 Office 嵌入图片形成 `vision-task/v1`；`wav/mp3/m4a/aac/ogg/wma/amr` 及非空 `.m4a.larkcache` 形成 `audio-task/v1`。

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
  --output-corpus extracted/vision-source-corpus.txt \
  --evidence extracted/vision-evidence.json
```

没有视觉任务时也必须运行该命令，它会生成零任务证据包并复制语料。缺少子智能体结果、哈希不一致、模型不符或仍有看不清的关键字段时立即失败；主智能体不得猜测或绕过。

### 3. 生成并读回音频逐字稿

音频固定走用户身份的飞书妙记，不调用视觉子智能体，不使用本地 Whisper，也不得用妙记 Summary、Todo、Chapter 或 Keyword 代替逐字稿：

```bash
python3 "$SKILL_ROOT/scripts/audio_tool.py" transcribe \
  --tasks extracted/audio-tasks.json \
  --receipts-dir extracted/audio-receipts \
  --transcripts-dir extracted/audio-transcripts

python3 "$SKILL_ROOT/scripts/audio_tool.py" reconcile \
  --tasks extracted/audio-tasks.json \
  --receipts-dir extracted/audio-receipts \
  --transcripts-dir extracted/audio-transcripts \
  --source-corpus extracted/vision-source-corpus.txt \
  --output-corpus extracted/verified-source-corpus.txt \
  --evidence extracted/audio-evidence.json
```

`transcribe` 实际执行 `drive +upload → minutes +upload → minutes +detail --transcript`，并轮询最多 2 小时。只在同一次本地运行中按音频哈希去重；已有本地回执也必须重新远端读回逐字稿。Base 中的 `audio_minutes` 仅用于审计，不作为跨运行可信输入，补充处理会重新上传当前附件，避免被编辑的基线把无关妙记绑定到案件音频。没有音频任务时两个命令也必须执行，它们会生成零任务证据包并逐字节复制视觉语料。

输出契约见 `references/audio-contract.md` 和 `references/audio-result-schema.json`。音频缺失、超过 6GB、权限不足、转写超时、逐字稿为空、远端响应或逐字稿哈希不一致时立即失败。

### 4. 填写完整事实脚手架

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" scaffold \
  --case-number "$case_number" \
  --manifest extracted/material-manifest.json \
  --output case-facts.json
```

在脚手架上填写，不重建精简版 JSON：

- 事实只来自 `extracted/verified-source-corpus.txt`；
- 先按“当前程序 → 前置程序 → 证据事实”建立程序链。存在民事起诉状和法院送达材料时，当前案件类型是诉讼；历史仲裁裁决只能放入关联案件、程序记录和裁判历史，不得覆盖当前诉讼；
- Base 案件编号与法院/仲裁案号分栏，禁止混用；一条记录包含多个关联案件或多个我方主体时，必须在当事人、关联案件、程序记录和矛盾登记中全部展开，不得只取第一个；
- 核心事实有明确依据就填写；材料未写明填 `未载明`，字段不适用填 `不适用`，来源冲突或字迹无法唯一确认填 `待核` 并同步写入矛盾登记。只有整理人、审核人、负责人、期限等人工操作字段保留真空白；
- `evidence_rows` 只填写正式证据项，不得把送达地址确认书、起诉状、申请书、答辩书、证据材料清单、裁判文书、庭审笔录或内部工作底稿当作证据；`completeness_rows` 必须覆盖全部材料；
- 请求金额只来自当前请求事项。协议补偿、已付款、银行流水或裁判认定金额如果不是当前请求，不得写入“本金/应退款项”；
- 身份证号、手机号和银行卡号必须脱敏；报告正文不得出现模型名、工具名、任务 schema、解析 method、调用参数或运行日志；
- 不删除 `completeness_rows` 中的材料项；`quality_rows` 必须完整填写材料读取、事实来源、当前程序、金额、空值和内部过程泄露六项检查；
- 裁决书或判决书已经记载结果时，同时填写第九章、`case_status` 和 `base_fields.case_status`；
- `base_fields` 填写案件名称、案件类型、立案日期、案件状态；无依据字段留空。

验证：

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" validate-facts \
  --facts case-facts.json \
  --source-corpus extracted/verified-source-corpus.txt \
  --vision-corpus extracted/vision-source-corpus.txt \
  --manifest extracted/material-manifest.json \
  --vision-evidence extracted/vision-evidence.json \
  --vision-tasks extracted/vision-tasks.json \
  --audio-evidence extracted/audio-evidence.json \
  --audio-tasks extracted/audio-tasks.json \
  --audio-receipts-dir extracted/audio-receipts \
  --audio-transcripts-dir extracted/audio-transcripts
```

非零退出时修正事实；不放宽或跳过校验。

### 5. 渲染和创建文档

文档写入前重新读取同一记录并确认 `AI处理状态=分析中` 且执行日志仍包含当前 `dispatch_id`；任务已被接管时不得继续：

```bash
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-pre-document.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-dispatch \
  --runtime runtime.json --record-readback record-pre-document.json
```

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

### 6. 文档读回

```bash
lark-cli docs +fetch --as user \
  --doc "$document_token" --format json > report-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate \
  --input report-readback.json --facts case-facts.json
```

失败时只允许用同一 `report.xml` 全文重写一次后重新读回。

### 7. 上传人权限

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

### 8. 回写 Base 并读回

```bash
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-pre-writeback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" build-writeback \
  --runtime runtime.json \
  --facts case-facts.json \
  --manifest extracted/material-manifest.json \
  --vision-evidence extracted/vision-evidence.json \
  --vision-tasks extracted/vision-tasks.json \
  --audio-evidence extracted/audio-evidence.json \
  --audio-tasks extracted/audio-tasks.json \
  --audio-receipts-dir extracted/audio-receipts \
  --audio-transcripts-dir extracted/audio-transcripts \
  --source-corpus extracted/verified-source-corpus.txt \
  --vision-corpus extracted/vision-source-corpus.txt \
  --document-token "$document_token" \
  --report-url "$report_url" \
  --record-readback record-pre-writeback.json \
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
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-pre-failure.json

python3 "$SKILL_ROOT/scripts/report_tool.py" build-failure \
  --runtime runtime.json --error-code "$error_code" \
  --record-readback record-pre-failure.json \
  --output base-update.json --expectation base-expectation.json
```

`initial` 失败清空链接和基线；`supplement` 失败保留原链接和基线。若当前记录已经不再属于本 `dispatch_id`，不得回写失败状态或覆盖新任务。

## 返回

只有事实校验、文档读回、上传人权限、Base 八个字段读回全部通过，才返回：

```json
{
  "status": "completed",
  "record_id": "rec...",
  "dispatch_id": "odm-v64:rec...:...",
  "processing_status": "已完成",
  "report_url": "https://.../docx/...",
  "error_code": ""
}
```

否则返回 `status=failed`、`processing_status=分析失败` 和实际 `error_code`。
