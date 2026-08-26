# 纠纷材料整理工作流组件

当前发布版本：小组件 `6.1.1` / Skill `6.1.0`
文档架构：固定 XML + 可执行渲染器 + 远端失败闭环

```text
多维表格 → 小组件 → 纠纷材料整理智能体 + Skill
```

小组件只接收 `targetRecordId`，精确读取同一案件记录，判断首次处理或新增附件补充，写入“分析中”后创建 Agent 会话。Agent 读取附件，先生成结构化事实，再调用 Skill 的确定性渲染器一次创建或全文重写报告。远端文档、上传人权限和 Base 回写均读回通过后才完成。

## Base 字段

保留隐藏文本字段：

```text
材料处理基线
```

其内容只有：

```json
{
  "document_token": "报告 token",
  "processed_attachment_ids": ["已处理附件 ID"],
  "contract_version": "6.1.0"
}
```

它用于识别同案新增附件和旧合同迁移，不承担报告质量、覆盖率或审核逻辑。旧基线没有 `contract_version` 时，小组件会保留原报告 URL 并触发一次全量重写。

## 自动化

只映射：

```text
targetRecordId = 当前触发记录.Record ID
```

如果前一步新建记录：

```text
targetRecordId = 第 1 步新增案件记录.Record ID
```

自动化不修改状态、链接、日志或材料处理基线。

## 打包

```bash
npm run build
python3 agent-skill/organize-dispute-materials/scripts/package_skill.py \
  --source agent-skill/organize-dispute-materials \
  --output output/organize-dispute-materials-v6.1.0.zip --json
unzip -t output/organize-dispute-materials-v6.1.0.zip
```
