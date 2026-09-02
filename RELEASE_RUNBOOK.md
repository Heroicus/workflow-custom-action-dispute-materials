# 纠纷材料工作流投递组件发布验收

版本：7.0.2

## 工作流要求

1. 发布纠纷材料处理工作流。
2. Start 定义 record_id、dispatch_id、trigger_kind 三个文本输入。
3. End 输出 JSON 字符串，且包含与 Start 完全相同的 record_id、dispatch_id、trigger_kind。
4. 应用具备 aily:skill:write 权限。

## 自动化映射

| 触发场景 | targetRecordId | triggerKind |
| --- | --- | --- |
| 新增记录 | 当前记录的 Record ID | record_created |
| 案件文档变更 | 当前记录的 Record ID | case_document_changed |

## 上传与绑定

```bash
npm run build
npm run upload
```

在飞书开发者后台选择新上传的小组件版本，保存多维表格自动化配置后发布应用。

## 验收

一次自动化运行必须返回：

- accepted=true
- dispatchState=success
- workflowStatus=success
- workflowOutput 中的 record_id、dispatch_id、trigger_kind 与本次输入一致

任一项不满足即视为未完成。
