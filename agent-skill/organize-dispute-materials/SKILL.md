---
name: organize-dispute-materials
description: 读取指定案件记录的附件，填写固定飞书 Docx 模板，并将报告链接和案件字段写回同一条 Base 记录。
license: Internal
metadata:
  version: "5.4.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

处理运行信封指定的一条案件记录。交付物是写入同一条 Base 记录的报告链接，不是聊天中的案情摘要。

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
  "required_skill_version": "5.4.0"
}
```

`record_id` 是唯一定位键。案件编号和附件名称不能用于搜索其他记录。

## 执行

### 1. 读取目标记录与附件

先用 `app_token + table_id + record_id` 精确读取当前 Base 记录，确认案件编号、案件文档和上传人与运行信封一致。只读取该记录的附件正文。

- `initial`：读取 `attachment_ids` 中的全部附件；
- `supplement`：只读取 `new_attachment_ids`；
- 读取 PDF、扫描 PDF、图片、Word、Docx、Excel、表格和飞书文档；图片及无文本层 PDF 使用 OCR；
- 至少一个附件读取成功才继续；全部失败时写回 `ATTACHMENT_READ_FAILED`；
- 部分附件失败时继续处理已读取事实，只在执行日志记录失败文件数量，不把失败说明写进报告。

### 2. 生成或补充报告

`initial`：

1. 用飞书 Drive 复制 `template_document_token` 指定的原生 Docx；
2. 保留模板标题、章节、表头、审核区和签字区；
3. 清除可填写单元格中的示例值和占位文字；
4. 按 `references/report-table-map.md` 把材料明确记载的事实写入对应位置；
5. 案件编号使用运行信封的 `case_number`；
6. 没有事实的位置保持空白。

`supplement`：

1. 使用 `existing_document_token` 打开同一份报告；
2. 只把新增附件中的新事实写入对应既有章节或表格；
3. 需要增加条目时，在对应表格追加行；
4. 保持 `existing_report_url` 不变，不复制第二份报告。

禁止写入以下占位或推测内容：

```text
未载明
需补充
材料缺失
核心材料不足
风险提示
法律建议
待法务审核
AI 输出质量自检
```

### 3. 报告访问权限

对 `uploader_open_ids` 中每个上传人添加当前报告协作者：

```text
resource = 报告 document_token
type = docx
member_type = openid
perm = full_access
```

授权失败时写回 `DOC_PERMISSION_GRANT_FAILED`，不得写“已完成”。不扫描云盘或协作者列表。

### 4. 回写 Base

首次处理时，四个业务字段只写材料明确事实：

| 字段 | 有事实 | 无事实 |
|---|---|---|
| 案件名称 | 写文本 | 清空 |
| 案件类型 | 仅写“诉讼”或“仲裁” | 清空 |
| 立案（收案）日期 | 写真实日期 | 清空 |
| 案件状态 | 仅写表内已有选项 | 清空 |

文本、单选和日期必须使用其真实 Base 字段类型写入；不能用一个空字符串清空所有字段。

补充材料时，不清空已有业务字段；新增附件提供明确新事实时才更新。

材料处理基线只写：

```json
{
  "document_token": "报告 token",
  "processed_attachment_ids": ["已成功写入报告的附件 ID"]
}
```

部分附件读取失败时，只把成功写入报告的附件 ID 加入基线；失败附件保留为下一次运行的新增附件。补充成功时，基线使用原 ID 与本次成功 ID 的并集。

成功时一次回写：

```text
案件名称 / 案件类型 / 立案（收案）日期 / 案件状态
AI分析结果 = 报告 URL
材料处理基线 = 上述 JSON
AI处理状态 = 已完成
执行日志/失败原因 = 任务 <dispatch_id>：已完成
```

部分附件读取失败时，执行日志改为：

```text
任务 <dispatch_id>：已完成：ATTACHMENT_READ_PARTIAL=<数量>
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
