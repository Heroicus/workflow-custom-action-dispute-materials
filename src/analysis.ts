export const DISPATCH_CONTRACT_TYPE = "dispute-material-run/v6.5";
export const DISPATCH_OPERATION = "process_target_record";
export const REQUIRED_SKILL_VERSION = "6.5.1";

const REPORT_DOCX_ORIGIN = "https://aixuexi.feishu.cn";
const REPORT_DOCX_PATTERN = /https:\/\/aixuexi\.feishu\.cn\/docx\/([A-Za-z0-9_-]{8,128})(?=$|[\s)\]}>,'"?&#])/g;

export type ReportDocxReference = {
  url: string;
  documentToken: string;
};

export function reportDocxReferenceFromToken(documentToken: string): ReportDocxReference | undefined {
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(documentToken)) return undefined;
  return {
    documentToken,
    url: `${REPORT_DOCX_ORIGIN}/docx/${documentToken}`,
  };
}

function collectStrings(value: unknown, output: string[], seen: Set<unknown>): void {
  if (typeof value === "string") {
    output.push(value);
    return;
  }
  if (value === null || typeof value !== "object" || seen.has(value)) return;
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) collectStrings(item, output, seen);
    return;
  }
  for (const item of Object.values(value as Record<string, unknown>)) {
    collectStrings(item, output, seen);
  }
}

export function hasMeaningfulFieldValue(value: unknown): boolean {
  if (typeof value === "string") return Boolean(value.trim());
  if (typeof value === "number" || typeof value === "boolean") return true;
  if (Array.isArray(value)) return value.some(hasMeaningfulFieldValue);
  if (value === null || typeof value !== "object") return false;
  return Object.values(value as Record<string, unknown>).some(hasMeaningfulFieldValue);
}

export function parseReportDocxReference(value: unknown): ReportDocxReference | undefined {
  const strings: string[] = [];
  collectStrings(value, strings, new Set());
  const references = new Map<string, ReportDocxReference>();
  for (const text of strings) {
    REPORT_DOCX_PATTERN.lastIndex = 0;
    for (const match of text.matchAll(REPORT_DOCX_PATTERN)) {
      const documentToken = match[1];
      references.set(documentToken, reportDocxReferenceFromToken(documentToken)!);
    }
  }
  return references.size === 1 ? references.values().next().value : undefined;
}

export function resolveReportDocxReference(
  value: unknown,
  baselineDocumentToken: string,
): ReportDocxReference | undefined {
  if (!hasMeaningfulFieldValue(value)) return undefined;
  const baselineReference = reportDocxReferenceFromToken(baselineDocumentToken);
  if (!baselineReference) return undefined;
  const parsedReference = parseReportDocxReference(value);
  if (parsedReference && parsedReference.documentToken !== baselineDocumentToken) return undefined;
  return parsedReference || baselineReference;
}

export const PRODUCTION_APP_TOKEN = "K4nObpF5la8ertskcVccv2LknNh";
export const PRODUCTION_TABLE_ID = "tbllz7nrxSIH8frX";
export const BASELINE_FIELD_NAME = "材料处理基线";
export const MODEL_CONTRACT = {
  main_model: "Deepseek-V4-Pro",
  vision_agent_name: "纠纷材料视觉核验员",
  vision_model: "Doubao-Seed-2.1-turbo",
  vision_result_schema: "vision-evidence/v1",
  audio_transcription_service: "Feishu Minutes",
  audio_result_schema: "audio-evidence/v1",
  write_policy: "main_agent_only",
} as const;

export const PRODUCTION_FIELD_CONTRACT = {
  case_number: { id: "fldnDqIuar", name: "案件编号", type: "auto_number", access: "read_only" },
  case_name: { id: "fldZ1S4MD3", name: "案件名称", type: "text", access: "read_write" },
  case_type: { id: "fldZCjfhMY", name: "案件类型", type: "select", access: "read_write", options: ["诉讼", "仲裁"] },
  filing_date: { id: "fld9zzBKtm", name: "立案（收案）日期", type: "datetime", access: "read_write" },
  case_status: { id: "fldRlZJrNA", name: "案件状态", type: "select", access: "read_write", options: ["待立案", "审理中", "已结案", "已归档"] },
  attachments: { id: "fldOz2CYX4", name: "案件文档", type: "attachment", access: "read_only" },
  uploader: { id: "fldpXEeboF", name: "上传人", type: "user", access: "read_only" },
  processing_status: {
    id: "fldHeuCxLE",
    name: "AI处理状态",
    type: "select",
    access: "read_write",
    options: ["待处理", "分析中", "已完成", "分析失败"],
  },
  analysis_result: { id: "fldDH6CfUI", name: "AI分析结果", type: "text", access: "read_write" },
  execution_log: { id: "fldeBcCdyM", name: "执行日志/失败原因", type: "text", access: "read_write" },
  material_baseline: { id: "fldeOvHTNp", name: BASELINE_FIELD_NAME, type: "text", access: "read_write" },
} as const;

export type DispatchMode = "initial" | "supplement";

export type AnalysisPromptInput = {
  targetRecordId: string;
  dispatchId: string;
  mode: DispatchMode;
  caseNumber: string;
  attachmentIds: string[];
  newAttachmentIds: string[];
  uploaderOpenIds: string[];
  existingDocumentToken?: string;
  existingReportUrl?: string;
  componentBuild: string;
};

function requiredText(value: unknown, label: string): string {
  const text = String(value || "").trim();
  if (!text) throw new Error(`${label}为空`);
  return text;
}

function unique(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].sort();
}

export function buildAnalysisPrompt(input: AnalysisPromptInput): string {
  return JSON.stringify({
    type: DISPATCH_CONTRACT_TYPE,
    operation: DISPATCH_OPERATION,
    app_token: PRODUCTION_APP_TOKEN,
    table_id: PRODUCTION_TABLE_ID,
    record_id: requiredText(input.targetRecordId, "targetRecordId"),
    dispatch_id: requiredText(input.dispatchId, "dispatchId"),
    mode: input.mode,
    case_number: requiredText(input.caseNumber, "caseNumber"),
    attachment_ids: unique(input.attachmentIds),
    new_attachment_ids: unique(input.newAttachmentIds),
    uploader_open_ids: unique(input.uploaderOpenIds),
    existing_document_token: input.existingDocumentToken || "",
    existing_report_url: input.existingReportUrl || "",
    component_build: requiredText(input.componentBuild, "componentBuild"),
    required_skill_version: REQUIRED_SKILL_VERSION,
    model_contract: MODEL_CONTRACT,
    baseline_field_name: BASELINE_FIELD_NAME,
    field_contract: PRODUCTION_FIELD_CONTRACT,
  });
}
