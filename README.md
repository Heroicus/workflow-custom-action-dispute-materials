# 纠纷材料整理工作流组件

当前待发布版本：小组件 `6.7.2` / Skill `6.7.2`
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
  "authorized_uploader_open_ids": ["已读回 full_access 的上传人 open_id"],
  "contract_version": "6.7.2",
  "component_build": "6.7.2-skill-6.7.2",
  "skill_version": "6.7.2",
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

它用于绑定同案附件、上传人、报告 revision 和旧合同迁移，不承担报告质量结论。`6.7.0/6.7.1` 只有完整记录/build/Skill/revision/hash 均一致时才迁移；`6.5.x` 弱基线还必须以基线 token、Base 展示标题、远端标题和案件编号证明同源。完成记录再次触发时仍进入 supplement 全量复核，不以本地基线直接返回 no-op。

事务采用单调提交：候选报告读回通过后先写入同记录阶段绑定，再授权并完成最终提交；最终写后重新读取 Base、报告正文和协作者列表统一验证。失败先分类 `processing/staged/completed/conflict`，禁止无条件回滚或覆盖人工修改。

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
  --output output/organize-dispute-materials-v6.7.2.zip \
  --expected-version 6.7.2 --json
unzip -t output/organize-dispute-materials-v6.7.2.zip
```
