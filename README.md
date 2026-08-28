# 纠纷材料整理工作流组件

当前待发布版本：小组件 `6.5.2` / Skill `6.5.1`
本补丁让补充材料模式兼容 Base 文本字段中的 Markdown 飞书文档链接，并确保预检失败不会清空既有报告链接或材料处理基线。
文档架构：全附件提取 + Tesseract/豆包视觉双路核验 + 飞书妙记音频逐字稿 + DeepSeek 单写入 + 完整事实脚手架 + 固定 XML + 实际回写校验

```text
多维表格 → 小组件 → Deepseek-V4-Pro 主智能体 + Doubao-Seed-2.1-turbo 视觉子智能体 + Feishu Minutes + Skill
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
  "contract_version": "6.5.1",
  "audio_minutes": {
    "音频 SHA-256": {
      "file_token": "云盘文件 token",
      "minute_token": "妙记 token",
      "minute_url": "妙记 URL",
      "transcript_sha256": "逐字稿 SHA-256"
    }
  }
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
  --output output/organize-dispute-materials-v6.5.1.zip --json
unzip -t output/organize-dispute-materials-v6.5.1.zip
```
