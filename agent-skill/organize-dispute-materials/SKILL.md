---
name: organize-dispute-materials
description: 复制正式报告模板，按当前案件材料填充全表并回写同一条 Base 记录。
license: Internal
metadata:
  version: "5.3.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

只处理运行信封指定的一条案件记录和该记录当前的案件文档附件。

```text
app_token = K4nObpF5la8ertskcVccv2LknNh
table_id  = tbllz7nrxSIH8frX
record_id = 运行信封中的 record_id
```

## 输入

接收一个 `dispute-material-run/v3` JSON：

```json
{
  "type": "dispute-material-run/v3",
  "operation": "process_target_record",
  "record_id": "rec...",
  "dispatch_id": "odm-v3:rec...:...",
  "mode": "initial | supplement",
  "new_attachment_ids": [],
  "template_document_token": "...",
  "template_document_url": "https://.../docx/...",
  "required_skill_version": "5.3.0"
}
```

使用 `references/report-table-map.md` 填写模板。

## 首次处理

1. 用飞书原生 Drive 的复制文件能力复制 `template_document_token` 指向的 Docx；
2. 读取当前记录全部案件文档附件，填写该副本；
3. 保留模板的标题、章节、表格、列、审核区和签核区；
4. 只把附件和同一条 Base 记录中已有的明确事实写入对应栏目；材料没有记载的字段填 `未载明`；
5. 写完后读取该副本，按 `references/report-table-map.md` 的覆盖项核验；
6. 核验通过才交付该副本为当前案件唯一报告。

模板副本创建失败，写入：

```text
AI处理状态 = 分析失败
执行日志/失败原因 = 任务 <dispatch_id>：失败：TEMPLATE_COPY_UNAVAILABLE
```

覆盖核验失败，写入：

```text
AI处理状态 = 分析失败
AI分析结果 = 空
执行日志/失败原因 = 任务 <dispatch_id>：失败：REPORT_COVERAGE_INCOMPLETE
```

## 补充材料

只读取 `new_attachment_ids`，只向材料处理基线指定的同一份报告追加“补充材料”，不得创建第二份报告。追加后重新读取该报告并按同一映射核验新增材料对应内容。

## 回写

完成报告后：

1. 读取同一份报告，确认案件编号、附件事实和表格覆盖均已写入；
2. 为当前记录全部上传人确认 `full_access`；
3. 首次处理时按明确材料事实回写案件名称、案件类型、立案（收案）日期和案件状态；
4. 回写单个报告链接和材料处理基线：

```json
{
  "version": 3,
  "document_token": "报告 docx token",
  "template_document_token": "运行信封中的模板 token",
  "report_contract_version": "dispute-report/5.3.0",
  "processed_attachments": [{"attachment_id": "稳定附件标识", "size": 0}]
}
```

5. 回写最终 `AI处理状态`，再读取同一条 Base 记录确认。

文档、权限、链接、覆盖核验或最终 Base 读回失败时，清空 `AI分析结果`，将同一记录写为 `分析失败`，日志使用：

```text
任务 <dispatch_id>：失败：<错误代码>
```

只在最终 Base 读回成功后返回：

```json
{
  "status": "completed | review_required | failed",
  "record_id": "rec...",
  "dispatch_id": "...",
  "processing_status": "已完成 | 待法务审核 | 分析失败",
  "report_url": "https://.../docx/...",
  "writeback_verified": true
}
```
