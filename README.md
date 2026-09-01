# 纠纷材料整理工作流组件

当前待发布版本：小组件 `6.9.0` / Skill `6.9.0`

```text
多维表格自动化
→ 小组件向纠纷材料整理专员投递单记录运行信封
→ Skill 下载并封印全部附件
→ 原生文本直接进入逐字语料
→ 有效图片快照通过附件会话交给纠纷材料视觉核验员
→ 音频通过飞书妙记生成并读回逐字稿
→ 纠纷材料整理专员唯一合并事实并生成固定报告
→ 远端报告、权限和同记录 Base 写回统一读回
```

小组件只接收 `targetRecordId`，精确读取一条记录并投递完整 `dispute-material-run/v6.7` 信封。小组件返回 `accepted` 只表示会话已创建。只有 Skill 的 `validate-completion` 返回 `completed` 才表示案件处理完成。

## 发布目标

```text
app_id       = cli_aa1cd1168679dbc3
blockTypeID  = blk_6a966d0b27410bcc81706268
```

`block-basekit-cli` 从仓库父目录的 `app.json` 读取 `app_id`，从本仓库的 `block.json` 读取 `blockTypeID`。两个标识必须属于同一个飞书应用，否则上传会被拒绝。

## 责任边界

- 纠纷材料整理专员是唯一事实合并者、报告生成者和业务写入者。
- 纠纷材料视觉核验员只读取当前消息中的唯一图片附件并返回 `vision-evidence/v3`。
- 飞书妙记只提供音频逐字稿和远端回执。
- OCR 只用于定位视觉区域，不作为事实来源。
- 自动化不写 AI 状态、报告链接、执行日志或材料处理基线。

## Base 字段

案件表必须保留隐藏文本字段 `材料处理基线`。基线由脚本确定性生成，用于绑定 Base、记录、案件编号、报告 token、报告 URL、revision、报告内容哈希、附件集合、上传人权限、Skill 版本和证据包哈希。

当前版本标识：

```json
{
  "contract_version": "6.9.0",
  "component_build": "6.9.0-skill-6.9.0",
  "skill_version": "6.9.0"
}
```

`6.7.0` 至 `6.7.4` 的强基线只有在完整记录、build、Skill、revision 和 hash 全部一致时才允许迁移。`6.5.x` 弱基线还必须由基线 token、Base 展示标题、远端标题和案件编号共同证明同源。已完成记录再次触发时进入 supplement 全量复核，不直接返回 no-op。

## 自动化映射

直接处理当前记录：

```text
targetRecordId = 当前触发记录.Record ID
```

前一步新建记录：

```text
targetRecordId = 第 1 步新增案件记录.Record ID
```

## 构建与打包

```bash
npm run build
python3 agent-skill/organize-dispute-materials/scripts/package_skill.py \
  --source agent-skill/organize-dispute-materials \
  --output output/organize-dispute-materials-v6.9.0.zip \
  --expected-version 6.9.0 --json
unzip -t output/organize-dispute-materials-v6.9.0.zip
```
