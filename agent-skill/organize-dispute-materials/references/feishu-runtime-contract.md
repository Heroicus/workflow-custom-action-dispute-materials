# 飞书运行时契约

## 固定目标

```text
app_token = K4nObpF5la8ertskcVccv2LknNh
table_id  = tbllz7nrxSIH8frX
record_id = 运行信封中的 record_id
```

`record_id` 是唯一定位键。小组件同时提供案件编号、全部附件 ID、本次新增附件 ID、上传人 open_id、处理模式和已有报告标识。

## 执行链

```text
读取指定附件
→ 形成来源明确的结构化事实
→ report_tool.py 用固定 XML 模板渲染并本地校验
→ 一次创建或全文重写后进行远端读回校验
→ 为上传人添加报告 full_access
→ 回写同一 Base 记录
```

不复制远程模板，不逐块编辑文档，不扫描云盘、工作区、历史会话或其他 Base 记录。

## 字段契约

| 字段 | 类型 | 访问 |
|---|---|---|
| 案件编号 | 自动编号 | 只读，写入报告 |
| 案件名称 | 文本 | 首次按事实写入或清空 |
| 案件类型 | 单选 | 仅“诉讼”或“仲裁” |
| 立案（收案）日期 | 日期时间 | 按事实写入或清空 |
| 案件状态 | 单选 | 仅已有选项 |
| 案件文档 | 附件 | 只读 |
| 上传人 | 多人员 | 只读，用于报告授权 |
| AI处理状态 | 单选 | 分析中 / 已完成 / 分析失败 |
| AI分析结果 | 文本 | 当前报告 URL |
| 执行日志/失败原因 | 文本 | 任务状态或错误码 |
| 材料处理基线 | 文本 | 报告 token、已处理附件 ID 与合同版本 |

## 文档能力

运行环境必须提供：

```text
附件正文读取与图片 OCR
原生 Docx 创建
同一 Docx 全文重写
完整文档读回
Docx 协作者 full_access 添加
Base 精确读写
```

初次处理将 `report_tool.py` 输出的完整 XML 作为一次创建请求正文。补充处理将同一工具输出的完整 XML 作为一次全文重写请求写入原 document token。禁止手写 XML、使用 append、逐表修改或块移动生成正式报告。

## 成功边界

只有以下事实均已读回确认才算完成：

- 报告通过 `report_tool.py` 的本地与远端读回校验；
- 上传人 full_access 已从协作者列表读回；
- 报告 URL、材料基线和最终状态写回同一 `record_id`。

小组件返回 `accepted` 只表示 Agent 会话创建成功。

## 必需应用权限

```text
drive:drive
docs:permission.member:create
docs:permission.member:retrieve
```
