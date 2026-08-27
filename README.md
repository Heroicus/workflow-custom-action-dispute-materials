# 纠纷材料整理工作流组件

当前待发布版本：小组件 `6.2.0` / Skill `6.2.0`
文档架构：全附件提取 + 完整事实脚手架 + 固定 XML + 实际回写校验

```text
多维表格 → 小组件 → 纠纷材料整理智能体 + Skill
```

小组件只接收 `targetRecordId`，负责精确定位并投递完整运行信封。Skill 负责附件提取、事实生成、文档创建、权限和同记录回写。小组件的 `accepted` 不代表报告已完成。

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
  "contract_version": "6.2.0"
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
  --output output/organize-dispute-materials-v6.2.0.zip --json
unzip -t output/organize-dispute-materials-v6.2.0.zip
```
