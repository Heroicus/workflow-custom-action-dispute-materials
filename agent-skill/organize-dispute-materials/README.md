# organize-dispute-materials 3.3.3

生产级飞书案件材料整理 Skill。本包必须接收组件传入的 `RUNTIME_INPUT_JSON`，使用 `table_id + record_id` 精确读取当前生产 Base 记录的 `上传材料`，创建原生飞书云文档，向该记录 `上传人.open_id` 授予并核验 `member_type=openid`、`perm=full_access`，最后回写同一条记录。

## 重要边界

- 这是本地源包，不代表已经导入 Aily、发布或绑定工作流。
- 工作流只映射 `table_id + record_id`；组件精确读取记录后再取得案件编号。
- `record_id` 是唯一定位键；案件编号只做记录字段一致性核验，禁止按案件编号搜索或猜测记录。
- 运行第一步必须调用已授权的 Base 精确读取工具；禁止用 `bash` 搜索文件系统、环境变量、凭据、历史工作区或旧记录。
- 如果 Base 精确读取工具不存在、未授权或不接受 `table_id + record_id`，立即返回 `BASE_CONNECTOR_UNAVAILABLE`，不得长时间重试、创建文档或返回成功链接。
- 组件只提交精确记录引用，不上传附件二进制；Skill 负责当前记录附件正文、原生云文档、权限和同记录终态回写。
- 最终报告只能是原生飞书云文档；`assets/reference-template.docx` 仅作为版式和结构参考，不是最终交付物。
- 当前 Base 字段必须使用 `上传材料`、`上传人`、`案件编号`、`AI分析结果`、`状态` 和 `执行日志/失败原因`。
- 没有权限读回就不得写成功链接；权限失败必须绑定同一 `record_id` 并读回失败状态。

## 运行能力

部署的 Aily Agent 必须提供等价的以下操作：

1. Base 使用生产配置 + `table_id + record_id` 精确读取一条记录；
2. 枚举并读取该记录的附件正文；
3. 创建原生 docx 云文档并读取结构；
4. 添加 docx 协作者并读回成员有效权限；
5. 更新并读回同一 `record_id` 的 Base 记录。

如果连接器只返回表格行、文件名、大小或 file token，而不能读取正文，运行必须失败。没有可执行连接器时，不得用自然语言知识资产或 shell 文件搜索替代。

## 本地验证

本包仅使用 Python 标准库脚本。无需在案件运行时安装 Python 依赖。

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 scripts/validate_template.py assets/reference-template.docx
python3 scripts/validate_delivery.py /tmp/doc-url.txt --allowed-host <configured-host>
```

## 打包

不要直接把桌面目录压缩。使用包内脚本，它会拒绝隐藏文件、符号链接、路径穿越和包外文件：

```bash
python3 scripts/package_skill.py \
  --source . \
  --output /Users/heroicus/Desktop/organize-dispute-materials-v3.3.3.zip
```

ZIP 必须在根目录直接包含 `SKILL.md`、`agents/`、`references/`、`assets/` 和 `scripts/`。

## 发布前人工门禁

本地检查通过不等于线上可用。导入前必须确认目标 Aily Agent 的真实运行日志出现“Base 精确读取 → 枚举附件 → 读取正文”，而不是 `bash` 搜索或 `BASE_CONNECTOR_UNAVAILABLE`；随后还必须验证 `openid/full_access` 权限读回、Base 最终状态，以及目标上传人实际打开、编辑文档和管理协作者。
