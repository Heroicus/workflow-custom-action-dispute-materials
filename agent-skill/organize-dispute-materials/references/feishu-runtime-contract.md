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
→ 下载全部附件，生成下载封印，绑定原始回执哈希、record_id、字段 ID、附件 token、路径、字节数和 SHA-256
→ OCR 只提供定位提示，纠纷材料整理专员通过平台原生智能体调用能力逐项调用纠纷材料视觉核验员
→ 纠纷材料视觉核验员重新下载唯一附件并返回只读逐字证据
→ Feishu Minutes 上传音频并远端读回逐字稿
→ 纠纷材料整理专员从统一核验语料唯一生成事实
→ report_tool.py 使用固定 XML 模板渲染并本地校验
→ supplement 先校验并快照旧 token/revision/内容哈希，再按 revision 乐观覆盖
→ 远端读回目标文档 token、revision、全部可见节点、表格列宽向量和链接属性
→ Base 阶段绑定候选报告并读回
→ 为每个上传人添加 full_access，并读取目标 docx token 的协作者列表、固化 token 绑定回执
→ Base 最终状态回写
→ 写后重新读取 Base、完整报告和全部上传人权限并统一验证
```

不复制远程模板，不使用 append，不扫描云盘、工作区、历史会话或其他 Base 记录。

## 智能体职责

```text
纠纷材料整理专员       唯一事实合并者、唯一报告生成者、唯一业务写入者
纠纷材料视觉核验员     只读指定 Base 记录、附件和视觉单元并返回 vision-evidence/v3
Feishu Minutes          用户身份远端音频转写，只提供逐字稿及远端回执
```

视觉子智能体和妙记服务不得填写事实脚手架、生成报告或写入 Base。视觉/音频任务必须绑定任务 ID、原文件哈希、媒体哈希和证据包哈希；关键视觉内容未决、逐字稿为空或远端回执不一致时，本轮失败。妙记摘要不得作为事实来源。

## 字段契约

| 字段 | 类型 | 访问 |
|---|---|---|
| 案件编号 | 自动编号 | 只读，写入报告 |
| 案件名称 | 文本 | 材料原文支持的正式案件名称；双方主体已知时必须同时包含双方名称，并绑定来源 SHA-256 与逐字引文 |
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

运行环境必须提供：Base 附件读取和媒体渲染、平台原生智能体调用、用户身份云盘上传、妙记逐字稿、Docx 创建与 revision 覆盖、完整文档读回、协作者添加与列表读回、Base 精确读写。

初次处理只创建一份报告。补充处理在覆盖前保存旧报告快照；失败先把 Base 分类为 processing、staged、completed 或 conflict。processing 状态下，文档仍是任务前精确 revision/hash 时直接失败；文档已是本次候选时补做阶段绑定后再失败。staged/completed 均保留已验证报告绑定，只标记失败，禁止回滚业务字段或覆盖人工修改。

## 成功边界

只有以下结果全部真实读回才算完成：

- 材料清单没有 `partial` 或 `failed`，下载 token 集合与运行信封完全一致，原始回执与下载封印哈希已进入材料清单和处理基线；
- 全部视觉和音频任务、证据包、语料哈希一致；
- 本地固定模板以及目标 token 的远端报告均通过校验；
- 每个上传人均在与目标 docx token 精确绑定的各自协作者列表回执中具有 `full_access`；
- 写前 Base 记录仍绑定相同案件编号、附件集合、上传人和 `dispatch_id`；
- 阶段回写读回为 `分析中 / 结果已写入，待最终校验`；
- 最终回写读回为 `已完成 / 任务 <dispatch_id>：已完成`；
- 最终状态写入后再次读取的报告 revision/hash、正文和协作者列表仍与完成期望一致。

小组件 `accepted` 只表示 Agent 会话已创建，不表示报告完成。

## 必需应用权限

```text
base:record:read
docs:document.media:download
drive:drive
drive:file:upload
minutes:minutes.upload:write
minutes:minutes.basic:read
minutes:minutes.artifacts:read
docs:permission.member:create
docs:permission.member:retrieve
```
