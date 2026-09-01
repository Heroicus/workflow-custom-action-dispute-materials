# 纠纷材料整理组件 6.7.4 / Skill 6.7.4 发布步骤

1. 应用和飞书用户身份必须已开通并发布 `aily:agent_chat:write`、`aily:agent_chat:read`、`base:record:read`、`docs:document.media:download`、`drive:drive`、`drive:file:upload`、`minutes:minutes.upload:write`、`minutes:minutes.basic:read`、`minutes:minutes.artifacts:read`、`docs:permission.member:create` 和 `docs:permission.member:retrieve`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 更新纠纷材料视觉核验员，使用 `视觉核验子智能体员工档案配置.md`，只保留飞书内置能力并删除反向调用技能，保留可调用通道；
4. 先导入 `organize-dispute-materials-v6.7.4.zip`；6.7.4 Skill 的失败恢复兼容 6.7.0、6.7.1、6.7.2 和 6.7.3 信封；
5. 更新纠纷材料整理专员，只保留飞书内置能力和本 Skill，使用 `智能体员工档案配置.md` 更新员工档案并发布主智能体；
6. 确认主智能体发布完成后再发布小组件 `6.7.4-skill-6.7.4`，不得颠倒滚动发布顺序；
7. 自动化只映射 `targetRecordId`；
8. 新记录验收：运行日志中必须出现纠纷材料整理专员、每项视觉任务的 Base 来源绑定、agent_chat_id、三个会话请求与读回工件哈希和回执集合哈希、纠纷材料视觉核验员生产者身份、飞书妙记逐字稿读回、`vision-evidence-pack/v3` 和 `audio-evidence-pack/v2` 校验；
9. 音频验收必须使用逐字稿而非妙记 AI 总结；材料清单覆盖全部附件及 ZIP 子文件，裁判结果不得遗漏，姓名、案号、日期和金额与原图或带时间戳逐字稿一致；
10. 检查 Base 的案件名称、类型、立案日期、案件状态、报告链接、基线、AI 状态和执行日志；协作者列表读回必须包含上传人 `full_access`，且上传人可以打开报告；
11. `6.7.0`、`6.7.1`、`6.7.2`、`6.7.3` 强基线和 `6.5.x` 弱基线首次运行会全量重写；随后给同一记录增加新附件，报告 URL 必须保持不变；
12. `accepted` 只表示会话创建成功，最终以 `validate-completion` 对 Base、完整报告和协作者权限的写后统一读回为准；精确任务租约一小时内不接管，过期后先把旧 dispatch 标记失败使其失去 Base 写权限，再创建新 dispatch；格式异常的“分析中”记录绝不自动覆盖；
13. 用一条隔离记录完成 initial，再在同记录追加一个附件完成 supplement；两次都保存运行信封、Skill 版本、报告 revision/hash、权限列表和 Base 最终读回。没有这组证据不得标记上线完成。
