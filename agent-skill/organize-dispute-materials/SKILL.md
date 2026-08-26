---
name: organize-dispute-materials
description: 读取指定案件记录的附件，在固定飞书 Docx 模板副本中整理案件事实，并将报告链接和案件字段写回同一条 Base 记录。
license: Internal
metadata:
  version: "5.4.1"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

处理运行信封指定的一条案件记录。最终交付物是写回该记录的报告链接，不是聊天中的案情摘要。

## 输入

只接受 `dispute-material-run/v5` JSON：

```json
{
  "type": "dispute-material-run/v5",
  "operation": "process_target_record",
  "app_token": "K4nObpF5la8ertskcVccv2LknNh",
  "table_id": "tbllz7nrxSIH8frX",
  "record_id": "rec...",
  "dispatch_id": "odm-v5:rec...:...",
  "mode": "initial | supplement",
  "case_number": "2026-001",
  "attachment_ids": ["附件 ID"],
  "new_attachment_ids": ["本次需读取的附件 ID"],
  "uploader_open_ids": ["ou_..."],
  "existing_document_token": "",
  "existing_report_url": "",
  "template_document_token": "Kk2edGa13oOrh8xuyM5ced3Gnhh",
  "required_skill_version": "5.4.1"
}
```

`record_id` 是唯一定位键。不得用案件编号、附件名称或内容搜索其他记录。

## 执行准则

### 必须满足

- 事实只能来自当前 `record_id` 的 Base 字段和附件正文；
- `initial` 必须使用指定模板的副本，`supplement` 必须编辑原报告；
- 不得覆盖整篇文档、重建全文或删除模板已有章节、表头、审核区和签字区；
- 没有材料依据的内容保持空白，不得保留模板示例值；
- 报告可访问、上传人权限成功、同记录回写成功后，才能标记“已完成”。

### 允许容错

- 单个附件读取失败时继续处理其他附件；
- 表格标签不能精确匹配时，可按所在章节和相邻表头定位；仍不能确认时留空；
- 允许按实际当事人、请求、时间线和证据数量在对应表格追加行；
- 允许在不改变事实含义的前提下压缩重复内容、统一日期和金额格式；
- 文档读回发现非关键格式差异时继续交付，不因标题样式、列宽或空行差异失败。

### 整体失败条件

只有以下情况使任务整体失败：

1. 全部附件均无法读取；
2. 首次处理无法复制模板，或补充处理无法打开原报告；
3. 报告主体被破坏且一次修复仍失败；
4. 权限授予或同记录最终回写失败。

## 执行步骤

### 1. 读取目标记录与附件

用 `app_token + table_id + record_id` 精确读取当前记录，确认案件编号、案件文档和上传人与运行信封一致。

- `initial`：读取 `attachment_ids` 中的全部附件；
- `supplement`：只读取 `new_attachment_ids`；
- 支持 PDF、扫描 PDF、图片、Word、Docx、Excel、表格和飞书文档；图片及无文本层 PDF 使用 OCR；
- 至少一个附件读取成功才继续；
- 部分附件失败时处理已读取事实，只在执行日志记录失败数量，不把失败说明写进报告。

### 2. 准备报告

`initial`：

1. 使用飞书 Drive 复制 `template_document_token` 指定的原生 Docx；
2. 记录副本原有的章节标题、表头、审核区和签字区，作为本次结构基线；
3. 清空可填写值单元格中的模板示例内容，保留标签、表头和固定说明；
4. 案件编号使用运行信封的 `case_number`。

`supplement`：

1. 使用 `existing_document_token` 打开原报告；
2. 保持 `existing_report_url` 和 document token 不变；
3. 只处理新增附件中的新事实，避免重复写入已有内容。

### 3. 原位填写

按 `references/report-table-map.md` 定位章节和表格，逐个更新值单元格；需要新增条目时，只在对应表格追加行。

- 不得使用全文覆盖、整篇 Markdown 写入或新建相似报告代替模板填充；
- 空章节、空表格、审核区和签字区继续保留；
- 同一事实在材料中重复出现时合并；
- 材料之间存在直接冲突时，分别保留双方记载并写入“十四、矛盾信息登记”；
- 计算参数完整且一致时可以计算；参数冲突时保留原文结果并登记冲突，不自行纠正材料。

以下文字不能作为未知内容的占位符：

```text
未载明
未提供
需补充
材料缺失
核心材料不足
暂无
待确认
（未明确表态）
```

材料明确记载的否定事实可以如实填写，但不能根据“没有看到”推导否定结论。

### 4. 结构读回

完成填写后读取报告结构：

- 编辑前存在的章节标题、固定表头、审核区和签字区应继续存在；
- 允许新增明细行、文本换行、样式和列宽出现非关键差异；
- 如果误删模板主体，先修复一次；首次处理仍无法修复时丢弃损坏副本并重新复制模板一次；
- 重新复制后仍无法保留主体时返回 `TEMPLATE_STRUCTURE_DAMAGED`；
- 不以固定标题数量、表格数量或逐字符相等作为成功条件。

### 5. 报告访问权限

对 `uploader_open_ids` 中每个上传人添加当前报告协作者：

```text
resource = 报告 document_token
type = docx
member_type = openid
perm = full_access
```

授权失败时返回 `DOC_PERMISSION_GRANT_FAILED`，不得标记“已完成”。不扫描云盘或协作者列表。

### 6. 回写 Base

首次处理时，业务字段只写材料明确事实：

| 字段 | 有事实 | 无事实 |
|---|---|---|
| 案件名称 | 写文本 | 清空 |
| 案件类型 | 仅写“诉讼”或“仲裁” | 清空 |
| 立案（收案）日期 | 写真实日期 | 清空 |
| 案件状态 | 仅写表内已有选项 | 清空 |

文本、单选和日期使用对应 Base 字段类型。补充材料时不清空已有业务字段，仅在新增材料提供明确新事实时更新。

材料处理基线只写：

```json
{
  "document_token": "报告 token",
  "processed_attachment_ids": ["已成功写入报告的附件 ID"]
}
```

部分附件失败时，只把成功写入报告的附件 ID 加入基线；补充成功时使用原 ID 与本次成功 ID 的并集。

成功时一次回写：

```text
案件名称 / 案件类型 / 立案（收案）日期 / 案件状态
AI分析结果 = 报告 URL
材料处理基线 = 上述 JSON
AI处理状态 = 已完成
执行日志/失败原因 = 任务 <dispatch_id>：已完成
```

部分附件读取失败时，执行日志追加：

```text
ATTACHMENT_READ_PARTIAL=<数量>
```

失败时：

```text
AI处理状态 = 分析失败
执行日志/失败原因 = 任务 <dispatch_id>：失败：<错误码>
```

首次处理失败时清空 `AI分析结果`。补充材料失败时保留原报告链接和原材料处理基线。

## 返回

完成 Base 回写后，只返回：

```json
{
  "status": "completed | failed",
  "record_id": "rec...",
  "dispatch_id": "odm-v5:rec...:...",
  "processing_status": "已完成 | 分析失败",
  "report_url": "https://.../docx/...",
  "error_code": ""
}
```

没有报告链接时不得返回 `completed`，不得输出案件正文或自然语言案情摘要。
