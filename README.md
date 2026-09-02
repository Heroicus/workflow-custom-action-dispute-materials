# 纠纷材料工作流投递组件

组件只负责将一条案件记录投递给已发布的纠纷材料处理工作流。

## 输入映射

| 组件参数 | 来源 |
| --- | --- |
| targetRecordId | 当前触发记录的 Record ID |
| triggerKind | record_created 或 case_document_changed |

组件为每次运行生成 dispatch_id，并调用一次：

POST /open-apis/aily/v1/apps/{app_id}/skills/{skill_id}/start

请求中的 input 仅包含：

```json
{
  "record_id": "当前记录 ID",
  "dispatch_id": "本次任务 ID",
  "trigger_kind": "触发类型"
}
```

## 工作流约定

工作流 Start 接收 record_id、dispatch_id、trigger_kind。

工作流 End 返回 JSON 字符串，并原样返回这三个字段。组件仅在接口 status 为 success 且返回字段完全一致时报告投递成功。

## 边界

- 不读取或更新多维表格记录。
- 不读取、解析或上传附件。
- 不调用智能体会话。
- 不写案件状态、报告链接、执行日志或材料基线。

## 构建与上传

```bash
npm run build
npm run upload
```
