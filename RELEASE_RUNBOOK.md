# 纠纷材料整理 5.3.2 发布步骤

1. 在应用 `cli_aaf4c426ea785cbd` 开通并发布：`drive:drive`、`docs:permission.member:create`、`docs:permission.member:retrieve`；
2. 保留案件表隐藏长文本字段 `材料处理基线`；
3. 导入 `organize-dispute-materials-v5.3.2.zip`；
4. 用 `智能体员工档案配置.md` 更新员工档案；
5. 上传并发布小组件 `5.3.2-uploader-targeted-permission`；
6. 自动化仅映射 `targetRecordId`；
7. 对当前不完整报告所在记录手动执行一次自动化。旧基线会触发完整重整并清空旧链接；
8. 验收 Base 的案件名称、类型、立案日期、案件状态、AI处理状态、报告链接和材料处理基线，并以上传人身份打开报告确认可访问。
