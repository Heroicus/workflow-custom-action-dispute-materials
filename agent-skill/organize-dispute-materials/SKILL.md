---
name: organize-dispute-materials
description: 读取指定案件记录附件，使用 Skill 内置固定 XML 模板一次性生成或重写案件报告，并将链接和案件字段写回同一条 Base 记录。
license: Internal
metadata:
  version: "6.0.0"
  tier: STANDARD
  category: legal-automation
---

# 纠纷材料整理

只处理运行信封指定的一条案件记录。最终结果是同一条 Base 记录中的报告 URL 和完成状态。

## 输入

只接受 `dispute-material-run/v6` JSON。必须包含：

```text
app_token
table_id
record_id
dispatch_id
mode = initial | supplement
case_number
attachment_ids
new_attachment_ids
uploader_open_ids
existing_document_token
existing_report_url
required_skill_version = 6.0.0
```

`record_id` 是唯一定位键。不得按案件编号、附件名称、内容或历史会话搜索记录。

## 固定边界

- 事实只来自当前 `record_id` 的 Base 字段和附件正文；
- 报告格式只来自 `references/report-template.xml`；
- 字段和动态行只按 `references/render-contract.md` 渲染；
- 不复制远程云模板，不读取其他报告作为案件事实；
- 不逐块修改、移动、删除或追加云文档表格；
- 没有材料依据的值输出为空单元格；
- 权限、报告 URL 和同记录回写完成后才能标记“已完成”。

## 1. 精确读取

使用 `app_token + table_id + record_id` 读取一条记录，并核对案件编号、附件和上传人。

- `initial`：读取 `attachment_ids`；
- `supplement`：读取 `new_attachment_ids`，并读取当前报告已有正文作为既有整理结果；
- 支持 PDF、扫描 PDF、图片、Word、Docx、Excel、表格和飞书文档；
- 图片及无文本层 PDF 使用 OCR；
- 单个附件失败时继续处理其他附件；全部附件均无法读取时返回 `ATTACHMENT_READ_FAILED`；
- 部分失败只写执行日志，失败附件不进入已处理附件基线。

## 2. 整理事实

先形成结构化案件数据，再渲染文档。每项值必须能指向当前附件中的原文位置。

允许：

- 合并重复事实；
- 在不改变含义的前提下压缩长句；
- 统一明确日期和金额的展示格式；
- 把新增材料事实合并到既有整理结果。

不允许：

- 根据缺少附件推导“没有、未提供、需补充”；
- 自行生成风险、建议、质证意见、待办、证据瑕疵或胜诉概率；
- 自行引用材料中没有出现的法律条文；
- 自行改变或重新计算材料明确记载的金额；
- 在冲突信息中选择一个结果冒充确定事实。

## 3. 一次性渲染

完整读取 `references/report-template.xml` 和 `references/render-contract.md`。

按照渲染契约：

1. 替换全部标量标记；
2. 生成全部动态表格行；
3. 空值替换为空字符串，空数组不生成数据行；
4. 对案件文本执行 XML 转义；
5. 确认最终 XML 没有模板标记和占位词。

`initial`：使用当前已授权的飞书原生文档创建能力，将完整 XML 作为一次创建请求的正文。

`supplement`：将既有事实与新增事实合并后，使用当前已授权的飞书原生文档全文重写能力，将完整 XML 一次写入 `existing_document_token`。必须保持原 document token 和 `existing_report_url`，不得创建第二份报告。

禁止使用 `append`、逐表修改或块移动拼装正式报告。

## 4. 文档读回

创建或重写后读取完整文档并验证：

- 标题包含当前案件编号；
- 十五个一级章节、二十二个二级章节和三十四张表格顺序正确；
- 反诉、事实争议、证据瑕疵、保全、类案等空表仍位于所属标题之后；
- 签字区位于文档末尾；
- 不存在 `{{...}}`、行标记、`未载明`、`人工复核` 或内部执行内容；
- 材料明确事实已经出现在对应章节。

结构或顺序错误时，只允许用同一完整 XML 重写一次。第二次仍错误返回 `REPORT_RENDER_INVALID`，不得通过移动单个表格修补。

## 5. 报告权限

对 `uploader_open_ids` 中每个上传人添加报告协作者：

```text
resource = 报告 document_token
type = docx
member_type = openid
perm = full_access
```

授权失败返回 `DOC_PERMISSION_GRANT_FAILED`，不得标记完成。不得扫描云盘或协作者列表。

## 6. 回写 Base

首次处理时，以下业务字段只写材料明确事实；无事实时使用字段类型对应的空值：

```text
案件名称
案件类型（仅诉讼或仲裁）
立案（收案）日期
案件状态（仅表内已有选项）
```

补充处理不清空已有业务字段，只在新增材料提供明确新事实时更新。

材料处理基线：

```json
{
  "document_token": "报告 token",
  "processed_attachment_ids": ["成功写入报告的附件 ID"]
}
```

成功时一次写回并读回确认：

```text
案件业务字段
AI分析结果 = 报告 URL
材料处理基线 = 上述 JSON
AI处理状态 = 已完成
执行日志/失败原因 = 任务 <dispatch_id>：已完成
```

失败时：

```text
AI处理状态 = 分析失败
执行日志/失败原因 = 任务 <dispatch_id>：失败：<错误码>
```

首次失败清空报告链接和基线；补充失败保留原报告链接和原基线。

## 返回

完成 Base 回写后只返回：

```json
{
  "status": "completed | failed",
  "record_id": "rec...",
  "dispatch_id": "odm-v6:rec...:...",
  "processing_status": "已完成 | 分析失败",
  "report_url": "https://.../docx/...",
  "error_code": ""
}
```

没有有效报告 URL 时不得返回 `completed`。不得输出案件正文、附件内容、内部模型或自然语言案情摘要。
