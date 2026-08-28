# 纠纷材料整理组件 6.5.4 / Skill 6.5.2 发布步骤

1. 应用和飞书用户身份必须已开通并发布 `drive:drive`、`drive:file:upload`、`minutes:minutes.upload:write`、`minutes:minutes.basic:read`、`minutes:minutes.artifacts:read`、`docs:permission.member:create` 和 `docs:permission.member:retrieve`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 新建或更新子智能体“纠纷材料视觉核验员”，模型固定 `Doubao-Seed-2.1-turbo`，使用 `视觉核验子智能体员工档案配置.md`，不给予 Base、文档或权限写入工具；
4. 导入 `organize-dispute-materials-v6.5.2.zip`；
5. 主智能体模型固定 `Deepseek-V4-Pro`，添加上述子智能体，并用 `智能体员工档案配置.md` 更新员工档案；
6. 发布小组件 `6.5.4-skill-6.5.2`；
7. 自动化只映射 `targetRecordId`；
8. 新记录验收：运行日志中必须出现 Deepseek-V4-Pro 主模型、Doubao-Seed-2.1-turbo 子模型、全部视觉任务读回、飞书妙记逐字稿读回、`vision-evidence-pack/v1` 和 `audio-evidence-pack/v1` 校验；
9. 音频验收必须使用逐字稿而非妙记 AI 总结；材料清单覆盖全部附件及 ZIP 子文件，裁判结果不得遗漏，姓名、案号、日期和金额与原图或带时间戳逐字稿一致；
10. 检查 Base 的案件名称、类型、立案日期、案件状态、报告链接、基线、AI 状态和执行日志；协作者列表读回必须包含上传人 `full_access`，且上传人可以打开报告；
11. 旧版本基线首次运行会全量重写；随后给同一记录增加新附件，报告 URL 仍不变；
12. `accepted` 只表示会话创建成功，最终以视觉证据、音频证据、报告读回、权限读回和 Base 同记录读回为准；小组件在同一运行实例内按记录串行投递，并精确读回任务锁；记录处于“分析中”时不会自动超时接管，确需恢复孤儿任务时先人工确认旧智能体已停止，再重置该记录状态。
