export type AnalysisPromptInput = {
  caseNumber: string;
  recordId: string;
  tableId: string;
  uploaderOpenId?: string;
};

/**
 * Build the one-shot handoff message. The record ID is authoritative; the case
 * number has already been read from that exact record and is only downstream
 * consistency context, never a workflow input or lookup key.
 */
export function buildAnalysisPrompt(input: AnalysisPromptInput): string {
  const caseNumber = String(input?.caseNumber || "").trim();
  const recordId = String(input?.recordId || "").trim();
  const tableId = String(input?.tableId || "").trim();
  const uploaderOpenId = String(input?.uploaderOpenId || "").trim();
  if (!caseNumber) throw new Error("目标记录中的案件编号为空，无法提交智能体分析任务");
  if (!recordId) throw new Error("目标记录 ID 为空，禁止按案件编号猜测记录");
  if (!tableId) throw new Error("目标数据表 ID 为空，无法限定记录范围");
  const uploaderLine = uploaderOpenId ? `上传人 open_id：${uploaderOpenId}` : "";
  const runtimeInput = {
    table_id: tableId,
    record_id: recordId,
    case_number: caseNumber,
    ...(uploaderOpenId ? { uploader_open_id: uploaderOpenId } : {}),
  };
  return [
    "RUNTIME_INPUT_JSON（机器契约，必须原样使用）：",
    JSON.stringify(runtimeInput),
    "只处理上述 table_id + record_id 指向的同一条记录；record_id 是唯一定位键，案件编号只做一致性核验。",
    "第一步必须调用已授权的 Base 精确读取能力；禁止按案件编号搜索、猜测记录，禁止用 bash 搜索文件、环境变量、凭据或历史工作区。",
    "如果运行时没有可执行的 Base 精确读取工具，立即返回 error_code=BASE_CONNECTOR_UNAVAILABLE，不得长时间重试、创建文档或返回成功链接。",
    "随后只读取该记录当前的“上传材料”，完成原生飞书云文档、上传人 full_access 权限读回和同记录最终字段回写。",
    uploaderLine,
  ]
    .filter(Boolean)
    .join("\n");
}
