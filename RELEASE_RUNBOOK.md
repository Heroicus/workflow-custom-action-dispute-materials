# 纠纷材料整理组件 6.2.0 / Skill 6.2.0 发布步骤

1. 应用 `cli_aaf4c426ea785cbd` 必须已开通并发布 `drive:drive` 和 `docs:permission.member:create`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 导入 `organize-dispute-materials-v6.2.0.zip`；
4. 用 `纠纷材料整理专员员工档案配置.md` 更新员工档案；
5. 发布小组件 `6.2.0-complete-report`；
6. 自动化只映射 `targetRecordId`；
7. 新记录验收：材料清单覆盖全部附件及 ZIP 子文件，裁判结果不得遗漏，姓名和案号与原文一致；
8. 检查 Base 的案件名称、类型、立案日期、案件状态、报告链接、基线、AI 状态和执行日志；上传人必须可以打开报告；
9. 旧版本基线首次运行会全量重写；随后给同一记录增加新附件，报告 URL 仍不变；
10. `accepted` 只表示会话创建成功，最终以 Base 的报告链接和“已完成”状态为准。
