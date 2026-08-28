# 飞书运行时契约

## 固定目标

```text
contract  = dispute-material-run/v6.7
app_token = K4nObpF5la8ertskcVccv2LknNh
table_id  = tbllz7nrxSIH8frX
record_id = 运行信封中的 record_id
```

`record_id` 是唯一定位键。小组件同时提供案件编号、全部附件 token、本次新增附件 token、上传人 open_id、模式和已有报告标识；Skill 必须校验完整信封，不能扫描其他记录。

## 执行链

```text
用户身份读取同一记录并校验 dispatch 所有权
→ 下载全部附件，绑定下载回执、record_id、字段 ID、附件 token、路径和字节数
→ OCR 只提供定位提示，Doubao-Seed-2.1-turbo 直接读原图并返回只读逐字证据
→ Feishu Minutes 上传音频并远端读回逐字稿
→ Deepseek-V4-Pro 从统一核验语料唯一生成事实
→ report_tool.py 使用固定 XML 模板渲染并本地校验
→ supplement 先校验并快照旧 token/revision/内容哈希，再按 revision 乐观覆盖
→ 远端读回目标文档 token、revision、章节、表格顺序与全部表格单元格
→ 为每个上传人添加 full_access，并由 report_tool 直接读取目标 docx token 的协作者列表、固化 token 绑定回执
→ Base 阶段回写并读回
→ Base 最终状态回写并读回
```

不复制远程模板，不使用 append，不扫描云盘、工作区、历史会话或其他 Base 记录。

## 模型职责

```text
Deepseek-V4-Pro         主智能体、唯一事实合并者、唯一业务写入者
Doubao-Seed-2.1-turbo   纠纷材料视觉核验员，只读原图并返回 vision-evidence/v2
Feishu Minutes          用户身份远端音频转写，只提供逐字稿及远端回执
```

视觉子智能体和妙记服务不得填写事实脚手架、生成报告或写入 Base。视觉/音频任务必须绑定任务 ID、原文件哈希、媒体哈希和证据包哈希；关键视觉内容未决、逐字稿为空或远端回执不一致时，本轮失败。妙记摘要不得作为事实来源。

## 字段契约

| 字段 | 类型 | 访问 |
|---|---|---|
| 案件编号 | 自动编号 | 只读，写入报告 |
| 案件名称 | 文本 | 有材料依据时写入 |
| 案件类型 | 单选 | 仅“诉讼”或“仲裁” |
| 立案（收案）日期 | 日期时间 | 有材料依据时写入 |
| 案件状态 | 单选 | 仅已有选项 |
| 案件文档 | 附件 | 只读 |
| 上传人 | 多人员 | 只读，用于授权 |
| AI处理状态 | 单选 | 分析中 / 已完成 / 分析失败 |
| AI分析结果 | 文本 | 当前报告 URL |
| 执行日志/失败原因 | 文本 | 当前任务阶段或错误码 |
| 材料处理基线 | 文本 | Base/记录/案件、报告 token/URL/revision/hash、附件和证据包绑定 |

组件必须分页读取字段定义，核对字段 ID、类型和所需单选项后才允许写状态。

## 文档能力

运行环境必须提供：附件读取和媒体渲染、自定义视觉子智能体、用户身份云盘上传、妙记逐字稿、Docx 创建与 revision 覆盖、完整文档读回、协作者添加与列表读回、Base 精确读写。

初次处理只创建一份报告。补充处理在覆盖前保存旧报告快照；后续任一成功门失败时，先恢复旧文档，再恢复 Base 阶段写入前业务字段并标记失败。

## 成功边界

只有以下结果全部真实读回才算完成：

- 材料清单没有 `partial` 或 `failed`，下载 token 集合与运行信封完全一致；
- 全部视觉和音频任务、证据包、语料哈希一致；
- 本地固定模板以及目标 token 的远端报告均通过校验；
- 每个上传人均在与目标 docx token 精确绑定的各自协作者列表回执中具有 `full_access`；
- 写前 Base 记录仍绑定相同案件编号、附件集合、上传人和 `dispatch_id`；
- 阶段回写读回为 `分析中 / 结果已写入，待最终校验`；
- 最终回写读回为 `已完成 / 任务 <dispatch_id>：已完成`。

小组件 `accepted` 只表示 Agent 会话已创建，不表示报告完成。

## 必需应用权限

```text
drive:drive
drive:file:upload
minutes:minutes.upload:write
minutes:minutes.basic:read
minutes:minutes.artifacts:read
docs:permission.member:create
docs:permission.member:retrieve
```
