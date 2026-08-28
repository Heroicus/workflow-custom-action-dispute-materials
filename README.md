# 纠纷材料整理工作流组件

当前待发布版本：小组件 `6.7.0` / Skill `6.7.0`
本版本将视觉层收口为只读逐字证据：豆包只返回原文和不确定区域，DeepSeek 在统一语料上唯一提取业务事实，OCR 只是视觉子智能体的定位提示，不直接进入事实语料。
文档架构：全附件回执绑定 + 豆包只读逐字证据 + 飞书妙记音频逐字稿 + DeepSeek 单写入 + 完整事实脚手架 + 固定 XML + 远程文档/权限读回 + Base 两阶段回写

```text
多维表格 → 小组件 → Deepseek-V4-Pro 主智能体 + Doubao-Seed-2.1-turbo 视觉子智能体 + Feishu Minutes + Skill
```

小组件只接收 `targetRecordId`，负责精确定位并投递完整运行信封。Skill 负责附件提取、事实生成、文档创建、权限和同记录回写。小组件的 `accepted` 不代表报告已完成。

## Base 字段

保留隐藏文本字段：

```text
材料处理基线
```

其内容由脚本确定性生成，核心字段为：

```json
{
  "app_token": "固定 Base token",
  "table_id": "固定表 ID",
  "record_id": "案件记录 ID",
  "case_number": "案件编号",
  "document_token": "报告 token",
  "report_url": "报告 URL",
  "document_revision_id": 1,
  "report_content_sha256": "远程报告正文 SHA-256",
  "processed_attachment_ids": ["已处理附件 ID"],
  "contract_version": "6.7.0",
  "component_build": "6.7.0-skill-6.7.0",
  "skill_version": "6.7.0",
  "source_corpus_sha256": "最终语料 SHA-256",
  "vision_verification": {},
  "audio_verification": {},
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

它用于识别同案新增附件和旧合同迁移，不承担报告质量、覆盖率或审核逻辑。任何非 `6.7.0` 的旧基线都只在报告 URL、文档 token 和案件标题经远端读回确认同源后进入一次全量迁移；无法证明同源时直接失败，不覆盖旧报告。

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
  --output output/organize-dispute-materials-v6.7.0.zip --json
unzip -t output/organize-dispute-materials-v6.7.0.zip
```
