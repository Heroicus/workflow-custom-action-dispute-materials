---
name: organize-dispute-materials
description: 读取小组件指定的一条案件记录，从全部附件提取事实，生成固定格式报告，并经远端读回后两阶段回写同一条 Base 记录。
license: Internal
metadata:
  version: "6.9.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

## 运行契约

只接受一个 `dispute-material-run/v6.7` JSON，且必须满足：

```text
operation = process_target_record
required_skill_version = 6.9.0
component_build = 6.9.0-skill-6.9.0
mode = initial | supplement
main_agent = 纠纷材料整理专员
vision_agent = 纠纷材料视觉核验员 / 只读
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
  extracted/vision-receipts extracted/audio-files extracted/audio-receipts \
  extracted/audio-transcripts permissions
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

python3 "$SKILL_ROOT/scripts/material_tool.py" seal-download \
  --runtime runtime.json \
  --download-receipt attachment-download.json \
  --input-dir materials \
  --output attachment-download-seal.json

python3 "$SKILL_ROOT/scripts/material_tool.py" extract \
  --runtime runtime.json \
  --download-receipt attachment-download.json \
  --download-seal attachment-download-seal.json \
  --input-dir materials \
  --output-dir extracted/text \
  --manifest extracted/material-manifest.json \
  --corpus extracted/source-corpus.txt \
  --vision-dir extracted/vision-pages \
  --vision-tasks extracted/vision-tasks.json \
  --audio-dir extracted/audio-files \
  --audio-tasks extracted/audio-tasks.json
```

下载完成后先生成下载封印。封印将原始回执哈希、用户身份、目标 `record_id`、案件文档字段 `fldOz2CYX4`、附件 token、文件路径、字节数和本地快照 SHA-256 绑定。提取器再次读取原文件，必须与封印完全一致。初次和补充处理都重读全部当前附件。OCR 只作为视觉子智能体提示，不能直接进入事实语料；任何 `partial` 或 `failed` 材料都会阻断完成。

## 2. 视觉逐字核验

视觉任务只覆盖图片附件、扫描页和文档中的有效嵌入图片。具有足够原生文字层且不是整页扫描图的 PDF 页面不再整页重复提交视觉核验；带整页扫描图的文字层页面仍按扫描页核验。`vision_tool.py prepare` 将每项任务绑定到附件下载封印中的唯一 Base 附件，并生成不可改写的调用清单。`transcribe` 以飞书用户身份把每个不可变图片快照上传到 `纠纷材料视觉核验员` 的附件会话，创建全部会话后统一轮询结果。每个会话只带一张真实图片和一项任务。不得用提示词代替图片，不得让视觉智能体重新读取 Base，不得自行生成或改写视觉结果。

```bash
python3 "$SKILL_ROOT/scripts/vision_tool.py" prepare \
  --runtime runtime.json \
  --download-seal attachment-download-seal.json \
  --tasks extracted/vision-tasks.json \
  --image-root extracted/vision-pages \
  --output extracted/vision-invocations.json
```

```bash
python3 "$SKILL_ROOT/scripts/vision_tool.py" transcribe \
  --runtime runtime.json \
  --download-seal attachment-download-seal.json \
  --tasks extracted/vision-tasks.json \
  --image-root extracted/vision-pages \
  --invocations extracted/vision-invocations.json \
  --results-dir extracted/vision-results \
  --receipts-dir extracted/vision-receipts

python3 "$SKILL_ROOT/scripts/vision_tool.py" reconcile \
  --runtime runtime.json \
  --download-seal attachment-download-seal.json \
  --tasks extracted/vision-tasks.json \
  --image-root extracted/vision-pages \
  --results-dir extracted/vision-results \
  --receipts-dir extracted/vision-receipts \
  --source-corpus extracted/source-corpus.txt \
  --output-corpus extracted/vision-source-corpus.txt \
  --evidence extracted/vision-evidence.json
```

`transcribe` 必须实际完成图片附件上传、会话创建和结果读回，并保存三段远端响应和哈希。只在网络或命令明确失败时结束，不做无依据的重复调用。结果不是唯一 `vision-evidence/v3` JSON、任务哈希不一致或存在关键不确定区域时立即阻断报告生成。零视觉任务也执行。契约见 `references/vision-contract.md` 与 `references/vision-result-schema.json`。

## 3. 音频逐字稿

音频只走用户身份的飞书妙记，不走视觉子智能体，不使用本地 Whisper，不用摘要、待办、章节或关键词代替逐字稿。

```bash
python3 "$SKILL_ROOT/scripts/audio_tool.py" transcribe \
  --tasks extracted/audio-tasks.json \
  --media-root extracted/audio-files \
  --receipts-dir extracted/audio-receipts \
  --transcripts-dir extracted/audio-transcripts

python3 "$SKILL_ROOT/scripts/audio_tool.py" reconcile \
  --tasks extracted/audio-tasks.json \
  --media-root extracted/audio-files \
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
- `quality_rows` 已由脚手架按材料清单确定性生成，禁止纠纷材料整理专员改写；
- `base_fields.case_name` 必须是材料原文支持的正式案件名称，且在双方主体已知时同时包含双方名称；`filing_date` 必须与 `scalars.filing_date` 同日，类型和状态必须与报告标量一致。案件名称和立案日期同样必须在 `source_refs` 中绑定材料 SHA-256 与逐字引文；不得按字符串公式拼接或自由发挥。
- 对每个非空且不是“未载明/不适用/待核”的实质事实，在顶层 `source_refs` 中以完整字段路径登记一个或多个 `{"source_sha256":"…","quote":"材料原文逐字引文"}`；引文必须出现在该哈希对应的语料分段，事实值也必须由这些指定来源共同支持。来源引用只用于校验，不渲染进报告。

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" validate-facts \
  --facts case-facts.json \
  --source-corpus extracted/verified-source-corpus.txt \
  --vision-corpus extracted/vision-source-corpus.txt \
  --manifest extracted/material-manifest.json \
  --vision-evidence extracted/vision-evidence.json \
  --vision-tasks extracted/vision-tasks.json \
  --vision-receipts-dir extracted/vision-receipts \
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

补充处理必须保留原 document token 和 URL。若快照、token、URL、案件编号、revision 或强基线哈希不一致，覆盖前立即失败。`6.7.0/6.7.1` 必须校验完整记录、build、Skill、revision 和哈希绑定；`6.5.x` 弱基线还必须以 Base 展示标题、远端标题和案件编号证明同源。

## 6. 候选报告远端读回

写后必须全文读回。远端节点、文本、章节、表格、列宽向量和链接属性必须与本地 `report.xml` 一致：

```bash
lark-cli docs +fetch --as user \
  --doc "$document_token" --detail full --format json > report-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" validate \
  --input report-readback.json \
  --document-token "$document_token" \
  --expected-report report.xml \
  --facts case-facts.json > report-validation.json
```

不得把 create/update 成功当成报告成功。权限在 Base 阶段写入完成后处理，使已验证候选先获得同记录持久绑定；阶段失败时不创建新的孤儿报告。

## 7. 单调事务提交

事务只允许以下状态前进：

```text
处理中 → 结果已写入，待最终校验 → 已完成
                                  ↘ 分析失败，保留已验证报告绑定
```

任何响应超时或读回失败都先读取 Base、报告和权限并分类；禁止“任一失败先回滚”。

### 7.1 阶段写入

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
  --vision-receipts-dir extracted/vision-receipts \
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

Base 可能只读回超链接显示标题；标题只有与同记录基线中的 `document_token/report_url` 同时精确一致才通过。阶段状态仍是“分析中”，不得宣称完成。

阶段写响应不确定时重新读取 Base 并分类：

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" classify-base-state \
  --runtime runtime.json --record-readback base-stage-readback.json \
  --staged-expectation base-stage-expectation.json > base-state.json
```

`staged` 才继续；`processing` 进入第 8 节恢复；其他状态零覆盖。

### 7.2 授权与最终提交前校验

阶段写入后，对每个上传人分别添加权限并远端读回：

```bash
mkdir -p permissions-final
while IFS= read -r uploader_open_id; do
  lark-cli drive +member-add --as user \
    --token "$document_token" --type docx \
    --member-type openid --member-id "$uploader_open_id" \
    --perm full_access --yes --format json > "permissions-final/$uploader_open_id.add.json"
  python3 "$SKILL_ROOT/scripts/report_tool.py" capture-permission \
    --document-token "$document_token" --member-id "$uploader_open_id" \
    --output "permissions-final/$uploader_open_id.json"
done < <(python3 -c 'import json; print(*json.load(open("runtime.json"))["uploader_open_ids"], sep="\\n")')
```

随后重新读取同一条 Base 和报告，不复用阶段前回执：

```bash
lark-cli docs +fetch --as user \
  --doc "$document_token" --detail full --format json > report-final-pre.json
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-final-pre-readback.json

python3 "$SKILL_ROOT/scripts/report_tool.py" build-finalize \
  --runtime runtime.json \
  --record-readback base-final-pre-readback.json \
  --staged-expectation base-stage-expectation.json \
  --report-readback report-final-pre.json \
  --expected-report report.xml \
  --permissions-dir permissions-final \
  --output base-finalize-update.json \
  --expectation base-finalize-expectation.json
```

### 7.3 最终写入与写后统一验证

```bash
lark-cli base +record-batch-update --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --json "$(cat base-finalize-update.json)" --format json > base-finalize-write.json

lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > base-final-readback.json
lark-cli docs +fetch --as user \
  --doc "$document_token" --detail full --format json > report-final-readback.json

mkdir -p permissions-post-commit
while IFS= read -r uploader_open_id; do
  python3 "$SKILL_ROOT/scripts/report_tool.py" capture-permission \
    --document-token "$document_token" --member-id "$uploader_open_id" \
    --output "permissions-post-commit/$uploader_open_id.json"
done < <(python3 -c 'import json; print(*json.load(open("runtime.json"))["uploader_open_ids"], sep="\\n")')

python3 "$SKILL_ROOT/scripts/report_tool.py" validate-completion \
  --runtime runtime.json \
  --record-readback base-final-readback.json \
  --final-expectation base-finalize-expectation.json \
  --report-readback report-final-readback.json \
  --expected-report report.xml \
  --permissions-dir permissions-post-commit > completion.json
```

只有 `validate-completion` 返回 `status=completed` 才能返回完成。它同时验证最终 Base、附件和上传人集合、报告 token/revision/正文以及全部上传人的 `full_access`。

## 8. 失败分类与恢复

先读取当前 Base，再按现有期望文件分类：

```bash
staged_arg=""; final_arg=""
test ! -f base-stage-expectation.json || staged_arg="--staged-expectation base-stage-expectation.json"
test ! -f base-finalize-expectation.json || final_arg="--final-expectation base-finalize-expectation.json"
lark-cli base +record-get --as user \
  --base-token "$app_token" --table-id "$table_id" \
  --record-id "$record_id" --format json > record-recovery.json
python3 "$SKILL_ROOT/scripts/report_tool.py" classify-base-state \
  --runtime runtime.json --record-readback record-recovery.json \
  ${staged_arg} ${final_arg} > base-recovery-state.json
```

分类后的唯一动作：

- `completed`：禁止回滚。重新执行第 7.3 节写后验证；若证据已经变化，只把本任务状态标为失败，保留报告和基线供下次同文档修复。
- `staged`：候选报告和 Base 绑定已经提交，禁止回滚业务字段或文档；标为失败并保留同一报告，下一次以 supplement 全量复核。
- `processing`：Base 尚未提交候选。先分类文档；正文仍是任务前精确 revision/hash 时可直接失败，正文是本次已验证候选时必须补做阶段写入后再失败，禁止制造 Base 基线与文档 revision 不一致的“伪回滚”。
- `failed`：停止，不重复写入。
- 分类冲突：返回 `unknown`，不得写 Base 或覆盖文档。

`supplement + processing` 的文档分类：

```bash
lark-cli docs +fetch --as user \
  --doc "$existing_document_token" --detail full --format json > report-recovery-pre.json
python3 "$SKILL_ROOT/scripts/report_tool.py" classify-document-state \
  --input report-recovery-pre.json \
  --document-token "$existing_document_token" \
  --original-report report-backup.xml \
  --original-metadata report-backup.json \
  --candidate-report report.xml > report-recovery-state.json
```

- `original`：revision、哈希和正文都仍等于任务前快照，不做文档操作。
- `candidate`：重新执行报告校验和第 7.1 节阶段写入，把当前 candidate revision/hash 持久绑定到同记录，再用 `--staged-expectation` 标记失败；不得把旧正文覆盖回去。
- 冲突：零覆盖并返回 `REPORT_ROLLBACK_CONFLICT`。

`initial + processing` 若已经创建并验证候选文档但尚未阶段写入，同样补做第 7.1 节阶段绑定后再标记失败，使重试始终复用同一报告。create 响应未知且无法证明 token 时不猜测、不创建删除指令，返回 `unknown`。

完成上述分类动作后才构建失败回写：

```bash
python3 "$SKILL_ROOT/scripts/report_tool.py" build-failure \
  --runtime runtime.json --error-code "$error_code" \
  --record-readback record-recovery.json \
  ${staged_arg} ${final_arg} \
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

`build-failure` 使用稳定 v6.7 恢复契约，可以为 6.7.0/6.7.1 滚动发布版本错配回写失败，但仍严格绑定固定 Base、表、字段、记录和原 dispatch；版本不匹配不会再造成永久“分析中”。

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
