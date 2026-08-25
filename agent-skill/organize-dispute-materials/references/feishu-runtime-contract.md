# 飞书运行时契约

本文件把 `SKILL.md` 中的自然语言要求转换为连接器必须提供的语义接口。Aily 工具名称可以不同，但不能缺少任何动作或返回字段。

## 1. 当前生产 Base

| 项目 | 值 |
| --- | --- |
| 目标表 | 诉讼仲裁案件信息汇总表 |
| 定位键 | `table_id + record_id`；其中 `record_id` 是唯一权威记录键 |
| 业务编号 | 案件编号；从精确记录读取，不参与定位或搜索 |
| 附件字段 | 上传材料 |
| 权限接收人字段 | 上传人 |
| 链接字段 | AI分析结果 |
| 状态字段 | 状态 |
| 失败字段 | 执行日志/失败原因 |
| 完成时间 | 文档与 Base 写回完成后填写 |
| 报告版本 | 与云文档模板版本一致 |
| 案件名称 | 文档标题来源 |
| 待办事项 | 仅写需人工处理的事项 |
| 最后更新时间 | 最终回读前更新（若可写） |

状态允许值：`待分析`、`分析中`、`待人工审核`、`已完成`、`分析失败`。

## 2. 必须的连接器动作

### 2.1 `base_record_get_exact`

输入：当前生产 Base 配置、`table_id`、`record_id`。

返回：恰好由该 `record_id` 指定的一条记录及当前字段快照，并核验消息中的 `case_number` 与记录的 `案件编号` 一致。

约束：不得调用按案件编号查找记录的接口，不得使用模糊搜索、首条命中、文件名猜测或历史索引替代精确读取。若运行时没有可执行的精确读取能力，必须立即返回 `BASE_CONNECTOR_UNAVAILABLE`，禁止进入 bash 搜索或长时间重试。

### 2.2 `base_attachment_list`

输入：目标 `record_id`。

返回每个附件的：`file_token`、`name`、`mime_type`、`size`、当前字段归属。

约束：不得把其他记录、知识库命中或历史报告加入清单。

### 2.3 `base_attachment_read`

输入：目标 `record_id` 和一个 `file_token`。

返回：非空正文或平台可解析的文件内容，并保留页码、图片位置或时间戳。

只有元数据、预览 URL 或文件名不算正文读取成功。每个文件失败必须有来源级错误。

### 2.4 `native_doc_create`

输入：标题、结构化正文、模板版本。

返回：`document_token`、HTTPS `url`、创建身份。

约束：类型必须为原生 `docx`；不得把本地 Word 文件上传或导入成最终报告；URL 不得在返回前直接写入 Base。

### 2.5 `native_doc_structure_read`

输入：`document_token` 或 URL。

返回：可读的 block/章节树。

必须验证：十五章、三十四张正式表、固定表头、来源定位和末页人工签字区；发现结构缺失先修复或失败，不交付不完整文档。

### 2.6 `permission_member_add`

输入必须等价于：

```json
{
  "resource_type": "docx",
  "resource_token": "<document-token>",
  "member_id": "<上传人.open_id>",
  "member_type": "openid",
  "perm": "full_access"
}
```

参考 CLI：

```text
lark-cli drive +member-add --token <url> --type docx --member-id <open_id> --member-type openid --perm full_access --yes
```

`--yes` 只表示已通过执行环境的高风险写入门禁；它不替代 API 返回校验。

### 2.7 `permission_member_read`

输入：资源 token、成员类型、成员 ID。

返回：成员有效权限。必须能明确证明该成员为 `full_access`。

以下情况失败：返回空、只读、编辑、继承来源不明、成员 ID 不匹配、批量部分成功、读回权限不足。

### 2.8 `base_record_update_and_readback`

更新顺序：

1. 记录锁定后先写 `状态=分析中` 并读回；
2. 成功完成文档和权限门禁后写裸 URL；
3. 读回 URL 并校验；
4. 再写最终状态、时间、版本和日志；
5. 再读回整条记录验证。

技术失败时：清空 `AI分析结果`，写 `状态=分析失败`，写精确错误到 `执行日志/失败原因`，然后再次读回。

## 3. URL 校验

URL 必须满足：

- scheme 为 `https`；
- hostname 来自部署配置的允许列表；
- path 为 `/docx/<token>`；
- token 只允许字母、数字、下划线和连字符；
- 无 query、fragment、用户名、密码、端口、空白和第二个 URL。

不得在 Skill 中写死某个租户域名。线上环境通过允许列表配置实际 Feishu/Lark host。

## 4. 身份规则

只使用目标记录 `上传人` 的 `open_id`。不得使用：

- 触发消息发送者的 open_id；
- `材料上传修改人` 的临时更新身份；
- 附件元数据的 owner_id；
- 历史报告的协作者；
- 邮箱模糊匹配替代 open_id。

如果 `上传人` 为空或包含多个无法确定的用户，必须失败。

## 5. 幂等和重试

幂等键：

```text
record_id + sorted(current file_token/hash set) + template_version
```

同一幂等键不得创建第二份报告。网络错误可有限重试，但不能改变目标记录、材料集合、权限接收人或模板版本。已创建但未完成授权的文档保留最小审计信息，清理需走单独的人工治理流程。
