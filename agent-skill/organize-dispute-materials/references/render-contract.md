# 报告数据契约

## 单一格式源

- `report-template.xml`：固定文档结构与样式；
- `render-schema.json`：动态行字段、结构数量和禁用文本；
- `scripts/report_tool.py`：唯一渲染和校验入口。

运行时先由 `report_tool.py scaffold` 生成完整 `case-facts.json`，再填入原文事实。

## `case-facts.json`

```json
{
  "scalars": {
    "case_number": "必填案件编号",
    "document_title": "可空；工具自动生成带案件编号的标题",
    "case_type": "诉讼或仲裁"
  },
  "rows": {
    "request_rows": [],
    "timeline_rows": [],
    "evidence_rows": [],
    "completeness_rows": [],
    "quality_rows": []
  },
  "base_fields": {
    "case_name": "",
    "case_type": "",
    "filing_date": "",
    "case_status": ""
  }
}
```

### 标量

`report-template.xml` 中全部 `{{name}}` 都是可用标量名。脚手架必须保留全部标量；除 `case_number` 外，空值统一渲染为空单元格。金额字段保持材料原值：

```text
principal_*        本金或退款
penalty_*          违约金
interest_*         利息
lawyer_fee_*       律师费
preservation_fee_* 保全或公证费
case_fee_*         诉讼费或仲裁费
total_*            合计
```

`*` 为 `formula / start / end / rate / amount`。费用与成本字段使用 `<name>_budget / actual / note`。

`calculation_detail_rows` 只用于当前请求中明确存在的利息、违约金、资金占用费或逾期费用分段计算。没有这类请求时数组必须为空；填写当期金额时，期间、基数、日利率和天数必须同时有材料支持。已付款、本金余额或请求变更差额不得写入该表。

### 动态行

动态行名与字段顺序只读取 `render-schema.json` 的 `dynamic_rows`。每行必须是对象，只能使用该行定义的键。数组为空时不生成数据行。

## 事实填写

- 当前记录字段和附件正文是唯一事实来源；
- Base 案件编号可直接写入，其他业务内容须有附件依据；
- 证据清单和材料完整性表必须保留脚手架中的全部文件项；
- 原文明确的事实、主张、意见和法律条文可以转写；
- 无依据内容留空，不用解释性占位词；
- 冲突信息保留两份原文，不替材料作结论；
- 不把工具日志、模型过程、内部文件名、视觉/音频处理方式、JSON 或结构检查写进报告。质量自检只写业务结论，不写实现名。

## 硬校验

`report_tool.py` 同时校验：

```text
章节与表格的数量、标题、表头和顺序
案件编号存在于文档标题
完整的 118 个标量、21 类动态行和 4 个 Base 回写字段
姓名、机构、案号和数字均有原文支持
原文明确出现的当事人、请求、争议、程序和裁判结果已进入对应章节
全部结构化事实已写入文档
无残留模板标记和禁用占位词
```

本地 XML 与云文档读回均使用同一校验器。
