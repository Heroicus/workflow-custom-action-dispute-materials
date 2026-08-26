# 纠纷材料整理 5.4.1 发布步骤

1. 应用 `cli_aaf4c426ea785cbd` 必须已开通并发布 `drive:drive`、`docs:permission.member:create`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 导入 `organize-dispute-materials-v5.4.1.zip`；
4. 用 `纠纷材料整理专员员工档案配置-v5.4.1.md` 更新员工档案；
5. 发布小组件 `5.4.1-balanced-template-fill`；
6. 自动化只映射 `targetRecordId`；
7. 新记录验收：报告是固定模板副本，原有章节、表头、审核区和签字区保留，示例值清空，未知内容为空；
8. 检查 Base 有报告链接且上传人可以打开；
9. 给同一记录增加新附件后再运行：报告 URL 不变，只补充新增事实；
10. `accepted` 只表示会话创建成功，最终以 Base 的报告链接和“已完成”状态为准。
