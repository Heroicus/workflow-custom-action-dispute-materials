# 纠纷材料整理工作流组件

当前版本：`5.3.1`

```text
多维表格 → 小组件 → 纠纷材料整理智能体 + Skill
```

小组件只接收明确的 `targetRecordId`，将同一案件记录置为“分析中”并投递一次任务。智能体复制飞书原生正式模板，按当前附件事实完成全表填写、读取核验，并回写报告链接、业务字段和终态。

## Base 前置字段

在 `诉讼仲裁案件信息汇总表` 保留一个隐藏长文本字段：

```text
材料处理基线
```

它保存已处理附件标识、报告文档 token 和报告契约版本，用于同案补充材料。旧版基线会自动触发一次完整重整，并在启动时清空旧报告链接。

## 自动化

直接案件记录流程只映射：

```text
targetRecordId = 当前触发记录.Record ID
```

若自动化先创建案件记录，则映射：

```text
targetRecordId = 第 1 步新增案件记录.Record ID
```

自动化不写状态、链接、日志或基线。

## 打包

```bash
npm run build
python3 agent-skill/organize-dispute-materials/scripts/package_skill.py \
  --source agent-skill/organize-dispute-materials \
  --output output/organize-dispute-materials-v5.3.1.zip --json
unzip -t output/organize-dispute-materials-v5.3.1.zip
```
