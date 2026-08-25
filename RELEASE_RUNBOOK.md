# 3.3.3 发布与端到端验收

## 发布前门槛

在组件仓库运行：

```bash
npm test
npm run build
```

必须同时通过：组件编译、记录级 Base 预检、`table_id + record_id` 消息契约、单次智能体会话创建、无附件二进制上传、错误诊断和 Skill/ZIP 契约测试。

在 Skill 根目录运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 scripts/validate_template.py assets/reference-template.docx
python3 scripts/package_skill.py --source . --output /tmp/organize-dispute-materials-v3.3.3.zip
```

## 线上绑定顺序

组件上传、开放平台发布、Base 工作流绑定、目标 Agent Skill 导入/发布和 Agent 连接器授权是五个独立状态，必须分别读回确认。

1. 导入并发布 Skill `3.3.3` 到固定 Agent `agent_4kuakyp7zsa2xuc`；确认 `agents/openai.yaml` 与 `SKILL.md` 同包、无旧版“只按案件编号查找”提示。
2. 在目标 Agent 中确认真实可执行能力：
   - Base 使用生产配置 + `table_id + record_id` 精确读取；
   - 当前记录附件枚举和正文读取；
   - 原生云文档创建和结构读回；
   - `openid/full_access` 添加和权限读回；
   - 同一记录更新和最终读回。
3. 若 Base 精确连接器不存在或未授权，运行必须快速返回 `BASE_CONNECTOR_UNAVAILABLE`，不得进入 `bash` 文件搜索循环。
4. 上传并发布组件 `3.3.3`；确认线上日志出现：

   ```text
   build=3.3.3-record-id-only-dispatch
   ```

5. 编辑并保存 Base 工作流，显式映射：
   - `recordId` ← 当前记录 `record_id`；
   - `targetTableId` ← 当前记录 `table_id`。
6. 触发条件只在案件编号、上传材料、上传人都已就绪后触发。

## 唯一有效的线上验收

使用一条新的隔离记录，禁止复用旧失败记录。验收日志必须按顺序出现：

```text
组件读取同一 table_id + record_id
→ 同记录状态=分析中并读回
→ 只创建一个 Aily chat
→ Agent 调用 Base 精确读取同一 record_id
→ 枚举并读取当前 上传材料 正文
→ 创建原生飞书云文档并读回结构
→ 对 上传人.open_id 添加 member_type=openid, perm=full_access
→ 读回权限确认为 full_access
→ 同一记录写入裸 /docx/ URL
→ 读回 URL、状态、完成时间、报告版本和失败字段
```

最终必须同时满足：

```text
状态=已完成 或 待人工审核
AI分析结果=唯一可点击原生云文档 URL
完成时间=非空
报告版本=非空
执行日志/失败原因为空
历史附件字段为空
```

最后由目标上传人真实打开文档，编辑正文，并打开协作者/分享设置验证可管理协作者。任何一步失败都不通过。

## 回滚

若线上失败，先保留运行日志和同记录诊断，不创建重复记录或重复文档。组件切回上一个已验证版本；Skill/连接器问题单独修正并重新发布。不得把本地测试、上传回执或 HTTP 成功当作端到端通过。
