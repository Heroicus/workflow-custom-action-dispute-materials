# 纠纷材料整理 6.0.0 发布步骤

1. 应用 `cli_aaf4c426ea785cbd` 必须已开通并发布 `drive:drive`、`docs:permission.member:create`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 导入 `organize-dispute-materials-v6.0.0.zip`；
4. 用 `纠纷材料整理专员员工档案配置-v6.0.0.md` 更新员工档案；
5. 发布小组件 `6.0.0-skill-owned-renderer`；
6. 自动化只映射 `targetRecordId`；
7. 新记录验收：报告由 Skill 内置 XML 一次生成，章节、表格和签字区顺序正确，未知内容为空；
8. 检查 Base 有报告链接且上传人可以打开；
9. 给同一记录增加新附件后再运行：报告 URL 不变，正文按同一 XML 模板完整重写并合并新增事实；
10. `accepted` 只表示会话创建成功，最终以 Base 的报告链接和“已完成”状态为准。
