# 纠纷材料整理 5.4.0 发布步骤

1. 应用 `cli_aaf4c426ea785cbd` 必须已开通并发布 `drive:drive`、`docs:permission.member:create`；
2. 保留案件表隐藏文本字段 `材料处理基线`；
3. 导入 `organize-dispute-materials-v5.4.0.zip`；
4. 用 `纠纷材料整理专员员工档案配置-v5.4.0.md` 更新员工档案；
5. 发布小组件 `5.4.0-v5-simple-report`；
6. 自动化只映射 `targetRecordId`；
7. 用一条新记录验收：报告是正式模板副本、未知内容为空、Base 有报告链接、上传人可打开；
8. 给同一记录增加一个新附件后再运行：报告 URL 不变，只补充新增材料；
9. `accepted` 只表示会话创建成功，最终以 Base 的报告链接和“已完成”状态为准。
