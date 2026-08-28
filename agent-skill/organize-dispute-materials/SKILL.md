---
name: organize-dispute-materials
description: 读取小组件指定的一条案件记录，从全部附件提取事实，生成固定格式报告，并经远端读回后两阶段回写同一条 Base 记录。
license: Internal
metadata:
  version: "6.7.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

## 运行契约

只接受一个 `dispute-material-run/v6.7` JSON，且必须满足：

```text
operation = process_target_record
required_skill_version = 6.7.0
component_build = 6.7.0-skill-6.7.0
mode = initial | supplement
main_model = Deepseek-V4-Pro
vision_agent = 纠纷材料视觉核验员 / Doubao-Seed-2.1-turbo / 只读
音频 = Feishu Minutes / 用户身份 / 远端逐字稿读回
唯一业务写入者 = 主智能体
```

将收到的完整 JSON 原样保存为 `runtime.json`，只处理其中的 `record_id`，禁止搜索、推测或切换其他记录。第一条本地命令必须使用 `report_tool.py validate-dispatch` 校验运行信封和当前记录所有权；不得改写信封规避校验。

## 0. 独立工作目录

每次运行使用只属于本 `dispatch_id` 的新目录。目录名取 `dispatch_id` 的 SHA-256 前 16 位，例如 `odm-job-<16hex>`。目录已经存在就失败，禁止复用、清空或读取旧任务目录。后续命令均在该目录执行，`SKILL_ROOT` 指向本 Skill 根目录。

```bash
umask 077
mkdir "$job_dir" && cd "$job_dir"
mkdir -p materials extracted/text extracted/vision-pages extracted/vision-results \
  extracted/audio-files extracted/audio-receipts extracted/audio-transcripts permissions
```

## 1. 读取同一记录并下载全部附件

```bash
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-initial.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-dispatch \
  --runtime runtime.json --record-readback record-initial.json

lark-cli base +record-download-attachment --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --output materials --overwrite \
  --format json > attachment-download.json

python3 "$SKILL_ROOT/scripts/material_tool.py" extract \
  --runtime runtime.json \
  --download-receipt attachment-download.json \
  --input-dir materials \
  --output-dir extracted/text \
  --manifest extracted/material-manifest.json \
  --corpus extracted/source-corpus.txt \
  --vision-dir extracted/vision-pages \
  --vision-tasks extracted/vision-tasks.json \
  --audio-dir extracted/audio-files \
  --audio-tasks extracted/audio-tasks.json
```

提取器必须验证下载回执是用户身份、目标 `record_id`、案件文档字段 `fldOz2CYX4`，并且附件 token、文件路径和字节数与运行信封完全一致。初次和补充处理都重读全部当前附件。OCR 只作为视觉子智能体提示，不能直接进入事实语料；任何 `partial` 或 `failed` 材料都会阻断完成。

## 2. 视觉逐字核验

逐项读取 `extracted/vision-tasks.json`：

1. 调用已经可路由的 `纠纷材料视觉核验员`；
2. 同时传入任务 JSON 和 `image_path` 原图，不能只传 OCR；
3. 子智能体固定为 Doubao-Seed-2.1-turbo，只返回 `vision-evidence/v2`；
4. 子智能体只逐字转录、标注不确定区域，不规范化日期、金额、姓名，不生成报告，不读写飞书；
5. 去掉 Markdown 代码围栏后，将原始 JSON 保存为 `extracted/vision-results/<task_id>.json`，不得补字段或改写；不合约只重试一次。

```bash
python3 "$SKILL_ROOT/scripts/vision_tool.py" reconcile \
  --tasks extracted/vision-tasks.json \
  --results-dir extracted/vision-results \
  --source-corpus extracted/source-corpus.txt \
  --output-corpus extracted/vision-source-corpus.txt \
  --evidence extracted/vision-evidence.json
```

零视觉任务也执行。契约见 `references/vision-contract.md` 与 `references/vision-result-schema.json`。

## 3. 音频逐字稿

音频只走用户身份的飞书妙记，不走视觉子智能体，不使用本地 Whisper，不用摘要、待办、章节或关键词代替逐字稿。

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

`transcribe` 必须实际完成 `drive +upload → minutes +upload → minutes +detail --transcript` 并保存远端回执。零音频任务也执行。契约见 `references/audio-contract.md` 与 `references/audio-result-schema.json`。

## 4. 填写事实脚手架

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" scaffold \
  --case-number "$case_number" \
  --manifest extracted/material-manifest.json \
  --output case-facts.json
```

只能在脚手架上填值：

- 唯一事实来源是 `extracted/verified-source-corpus.txt`；每个用户可见事实必须有原文字面支持；
- 当前程序、前置程序、关联案件、程序记录和证据事实分层，不用历史裁判覆盖当前程序；
- Base 案件编号与法院/仲裁案号分栏；多个主体、案件和请求不得只取第一项；
- 有依据就填；未写明填 `未载明`，不适用填 `不适用`，冲突或无法唯一确认填 `待核` 并登记矛盾；只有签字、负责人和人工期限等操作字段可留空；
- `evidence_rows` 只列正式证据；程序文书、裁判文书、庭审笔录、内部工作清单不得冒充证据；`completeness_rows` 覆盖全部材料；
- 金额只来自当前请求；分段计算有金额时，期间、基数、日利率和天数必须同时有材料支持；
- 身份证号、手机号、银行卡号必须脱敏；正文不得出现模型、工具、文件路径、任务 schema、解析方法、调用参数或运行日志；
- `quality_rows` 六项检查全部给出用户可理解的业务结论；
- `base_fields` 只填写有材料依据的案件名称、类型、立案日期、状态；无依据留空。

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

非零退出时只能修正事实，不放宽、删除或跳过校验。

## 5. 固定模板渲染与追加前快照

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" render \
  --facts case-facts.json --output report.xml

lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-pre-document.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-dispatch \
  --runtime runtime.json --record-readback record-pre-document.json
```

`initial` 创建新文档：

```bash
lark-cli docs +create --as user \
  --content @report.xml --format json > document-write.json
```

`supplement` 必须先读回并快照旧报告，再以旧 revision 乐观覆盖：

```bash
lark-cli docs +fetch --as user \
  --doc "$existing_document_token" --detail full --format json > report-before.json

python3 "$SKILL_ROOT/scripts/report_tool.py" snapshot-existing \
  --runtime runtime.json \
  --record-readback record-pre-document.json \
  --report-readback report-before.json \
  --output report-backup.xml \
  --metadata report-backup.json

old_revision="$(python3 -c 'import json;print(json.load(open("report-backup.json"))["document_revision_id"])')"
lark-cli docs +update --as user \
  --doc "$existing_document_token" --command overwrite \
  --revision-id "$old_revision" --content @report.xml \
  --format json > document-write.json
```

补充处理必须保留原 document token 和 URL。若快照、token、URL、案件编号、revision 或当前 6.7.0 基线哈希不一致，覆盖前立即失败。

## 6. 远端报告与权限读回

写后必须全文读回，且远端章节、表格顺序和全部表格单元格必须与本地 `report.xml` 一致：

```bash
lark-cli docs +fetch --as user \
  --doc "$document_token" --detail full --format json > report-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate \
  --input report-readback.json \
  --document-token "$document_token" \
  --expected-report report.xml \
  --facts case-facts.json > report-validation.json
```

不得把 create/update 成功当成报告成功。对每个上传人分别保存权限读回，禁止相互覆盖：

```bash
lark-cli drive +member-add --as user \
  --token "$document_token" --type docx \
  --member-type openid --member-id "$uploader_open_id" \
  --perm full_access --yes --format json > "permissions/$uploader_open_id.add.json"

python3 "$SKILL_ROOT/scripts/report_tool.py" capture-permission \
  --document-token "$document_token" \
  --member-id "$uploader_open_id" \
  --output "permissions/$uploader_open_id.json"
```

`capture-permission` 直接以用户身份调用协作者列表接口，并把目标 docx token、规范响应哈希和远端响应固化为回执。只有该 token 的协作者列表真实包含对应 open_id 且 `perm=full_access` 才算通过；缺 scope、只看到添加成功、手工拼 JSON 或无法读回都失败。

## 7. Base 两阶段回写

### 7.1 阶段写入

写前重新读取同一记录；构建命令会重新执行事实、材料、远端报告、权限、附件集合和任务所有权校验。

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
  --report-readback report-readback.json \
  --expected-report report.xml \
  --permissions-dir permissions \
  --output base-stage-update.json \
  --expectation base-stage-expectation.json

lark-cli base +record-batch-update --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --json "$(cat base-stage-update.json)" --format json > base-stage-write.json

lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-stage-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-writeback \
  --input base-stage-readback.json --expectation base-stage-expectation.json
```

阶段状态必须仍是 `分析中`，日志为 `结果已写入，待最终校验`；此时不得返回完成。

### 7.2 最终状态

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" build-finalize \
  --runtime runtime.json \
  --record-readback base-stage-readback.json \
  --output base-finalize-update.json \
  --expectation base-finalize-expectation.json

lark-cli base +record-batch-update --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --json "$(cat base-finalize-update.json)" --format json > base-finalize-write.json

lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-final-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-writeback \
  --input base-final-readback.json --expectation base-finalize-expectation.json
```

只有最终读回确认 `已完成` 和当前 `dispatch_id` 的完成日志后，才能返回完成。

## 8. 失败与补充回滚

任一门失败时保留原始错误码。若 `supplement` 已覆盖文档，先使用当前候选 revision 恢复 `report-backup.xml`，再读回验证：

```bash
lark-cli docs +fetch --as user \
  --doc "$existing_document_token" --detail full --format json > report-rollback-pre.json
candidate_revision="$(python3 -c 'import json;print(json.load(open("report-rollback-pre.json"))["data"]["document"]["revision_id"])')"
lark-cli docs +update --as user \
  --doc "$existing_document_token" --command overwrite \
  --revision-id "$candidate_revision" --content @report-backup.xml \
  --format json > report-rollback-write.json
lark-cli docs +fetch --as user \
  --doc "$existing_document_token" --detail full --format json > report-rollback-readback.json
python3 "$SKILL_ROOT/scripts/report_tool.py" verify-snapshot \
  --input report-rollback-readback.json \
  --document-token "$existing_document_token" \
  --snapshot report-backup.xml
```

只有确实生成过 `base-stage-expectation.json` 时，失败构建命令才附加 `--staged-expectation base-stage-expectation.json`，以恢复阶段写入前的业务字段：

```bash
staged_expectation_arg=""
test ! -f base-stage-expectation.json || staged_expectation_arg="--staged-expectation base-stage-expectation.json"

lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-pre-failure.json

python3 "$SKILL_ROOT/scripts/report_tool.py" build-failure \
  --runtime runtime.json --error-code "$error_code" \
  --record-readback record-pre-failure.json \
  ${staged_expectation_arg} \
  --output base-failure-update.json --expectation base-failure-expectation.json

lark-cli base +record-batch-update --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --json "$(cat base-failure-update.json)" --format json > base-failure-write.json
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-failure-readback.json
python3 "$SKILL_ROOT/scripts/report_tool.py" validate-writeback \
  --input base-failure-readback.json --expectation base-failure-expectation.json
```

`initial` 失败清空报告链接和基线；`supplement` 在完成文档和 Base 回滚后标记失败。当前记录已不属于本 `dispatch_id` 时，禁止覆盖新任务。

## 返回

完成返回必须来自最终 Base 读回，不来自本地变量、API 200 或“调用成功”：

```json
{
  "status": "completed",
  "record_id": "rec...",
  "dispatch_id": "odm-v67:rec...:...",
  "processing_status": "已完成",
  "report_url": "https://aixuexi.feishu.cn/docx/...",
  "error_code": ""
}
```

否则返回真实 `status=failed|unknown`、当前处理状态和原始 `error_code`；未经读回验证不得宣称完成。
