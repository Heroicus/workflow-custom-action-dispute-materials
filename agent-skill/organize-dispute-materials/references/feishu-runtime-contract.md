# 飞书运行时契约

## 目标记录

每次只操作：

```text
app_token = K4nObpF5la8ertskcVccv2LknNh
table_id  = tbllz7nrxSIH8frX
record_id = 运行信封中的目标案件记录
```

`record_id` 与 `uploader_open_ids` 由小组件显式传入。`uploader_open_ids` 只允许包含当前记录上传人的 `ou_...` 标识。

## 必需能力

运行时必须能够：

1. 精确读取和更新目标 Base 记录；
2. 读取当前记录的案件文档附件正文；
3. 复制运行信封指定的飞书原生 Docx；
4. 写入并读取复制后的飞书原生 Docx；
5. 为每一个当前上传人创建并读取报告 `full_access` 协作者权限；
6. 对同一条 Base 记录回写状态、链接、日志和材料处理基线。

仅当上传人 `full_access` 创建和读回均成功时，才允许写入报告链接和终态。运行应用必须具备 `drive:drive`、`docs:permission.member:create` 和 `docs:permission.member:retrieve`。

## 状态写入

小组件负责启动状态：

```text
AI处理状态 = 分析中
执行日志/失败原因 = 任务 <dispatch_id>：处理中
```

Agent 只对拥有同一 `dispatch_id` 的运行写入终态：

```text
已完成
待法务审核
分析失败
```

## 可写字段

```text
案件名称
案件类型
立案（收案）日期
案件状态
AI处理状态
AI分析结果
执行日志/失败原因
材料处理基线
```

案件编号、案件文档和上传人只读。

## 材料处理基线

字段名为 `材料处理基线`，类型为隐藏的长文本。内容为：

```json
{
  "version": 3,
  "document_token": "docx token",
  "template_document_token": "运行信封中的模板 token",
  "report_contract_version": "dispute-report/5.3.2",
  "processed_attachments": [
    {"attachment_id": "稳定附件标识", "size": 0}
  ]
}
```
