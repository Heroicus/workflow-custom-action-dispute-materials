# 视觉子智能体契约

## 固定角色

```text
名称：纠纷材料视觉核验员
模型：Doubao-Seed-2.1-turbo
权限：只读原始图片，只返回证据 JSON
```

视觉子智能体不得填写 `case-facts.json`，不得生成或修改报告，不得读写 Base、文档或权限。Deepseek-V4-Pro 主智能体是唯一事实合并者和业务写入者。

## 输入

主智能体从 `extracted/vision-tasks.json` 逐项调用子智能体。每次只传一张 `image_path` 指向的原图，以及该任务的完整 JSON。不得只传 Tesseract 文本。

图片包括：独立图片附件、无可用文字层的 PDF 页面，以及 Office 文件中的嵌入图片。PDF 页面固定按 300 DPI 渲染。任务内 `ocr_text` 仅作为对照，不是正确答案。

## 输出

必须只返回符合 `vision-result-schema.json` 的一个 JSON 对象：

- `verbatim_text` 只逐字转录可见内容，不总结、不推断、不补全；
- 姓名、机构、案号、日期、金额、利率、账号、裁判结果等进入 `critical_fields`；
- `normalized_value` 只能做原文直接支持的格式归一化；看不清的关键字段使用 `status=unclear` 并留空规范值，不得猜测；
- 表格按可见行列写入 `tables`，无法确定的单元格保留空值并记入 `uncertain_regions`；
- `source_sha256`、`image_sha256`、`task_id` 必须原样返回；
- `producer.agent_name` 固定为 `纠纷材料视觉核验员`；
- `producer.model` 固定为 `Doubao-Seed-2.1-turbo`。

禁止使用 `field_name`、顶层 `visible_text` 或 `status=inferred_from_context` 等自定义字段和值；顶层正文键必须是 `verbatim_text`，关键字段键必须是 `field_type / visible_text / normalized_value / status / source_ref`。任何上下文推断都不是视觉证据。

## 成功门

主智能体只允许去除代码围栏后，把每项原始结果保存到 `extracted/vision-results/<task_id>.json`，不得改名字段、补字段、转换 schema 或手工重写，再运行 `vision_tool.py reconcile`。证据包会绑定视觉任务文件、原始语料、最终语料、原文件和图片的 SHA-256；缺少结果、来源哈希不一致、关键字段无逐字转录支持、返回包含推断或仍有关键内容不清楚时，整个案件不得返回完成。
