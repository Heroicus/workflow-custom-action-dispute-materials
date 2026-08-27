# 纠纷材料整理组件 6.1.2 / Skill 6.1.1 发布步骤

1. 应用 `cli_aaf4c426ea785cbd` 必须已开通并发布 `drive:drive`、`docs:permission.member:create`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 导入 `organize-dispute-materials-v6.1.1.zip`；
4. 用 `纠纷材料整理专员员工档案配置.md` 更新员工档案；
5. 发布小组件 `6.1.2-evidence-gated-renderer`；
6. 自动化只映射 `targetRecordId`；
7. 新记录验收：报告通过 `report_tool.py` 固定渲染与远端读回，章节、表格和签字区顺序正确，未知内容为空；
8. 检查 Base 有报告链接且上传人可以打开；
9. 旧版本基线首次运行会自动保留报告 URL 并全量重写；随后给同一记录增加新附件，报告 URL 仍不变并合并新增事实；数字事实必须先通过 `validate-facts` 的材料/OCR 证据门；
10. `accepted` 只表示会话创建成功，最终以 Base 的报告链接和“已完成”状态为准。
