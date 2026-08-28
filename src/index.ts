import {
  basekit,
  Component,
  ParamType,
} from "@lark-opdev/block-basekit-server-api";
import {
  BASELINE_FIELD_NAME,
  buildAnalysisPrompt,
  DispatchMode,
  hasMeaningfulFieldValue,
  PRODUCTION_APP_TOKEN,
  PRODUCTION_FIELD_CONTRACT,
  PRODUCTION_TABLE_ID,
  REQUIRED_SKILL_VERSION,
  resolveReportDocxReference,
} from "./analysis";

const OPEN_API = "https://open.feishu.cn/open-apis";
const ORGANIZER_AGENT_ID = "agent_4kuakyp7zsa2xuc";
const BUILD_ID = "6.5.5-skill-6.5.3";
const REQUEST_TIMEOUT_MS = 10_000;
const AILY_CHATS_URL = `${OPEN_API}/aily/v1/agents/${ORGANIZER_AGENT_ID}/chats`;
const RECORD_QUEUES = new Map<string, Promise<void>>();

const FIELD_CASE_NUMBER = PRODUCTION_FIELD_CONTRACT.case_number.name;
const FIELD_ATTACHMENTS = PRODUCTION_FIELD_CONTRACT.attachments.name;
const FIELD_UPLOADER = PRODUCTION_FIELD_CONTRACT.uploader.name;
const FIELD_PROCESSING_STATUS = PRODUCTION_FIELD_CONTRACT.processing_status.name;
const FIELD_ANALYSIS_RESULT = PRODUCTION_FIELD_CONTRACT.analysis_result.name;
const FIELD_EXECUTION_LOG = PRODUCTION_FIELD_CONTRACT.execution_log.name;
const FIELD_MATERIAL_BASELINE = PRODUCTION_FIELD_CONTRACT.material_baseline.name;

basekit.addDomainList(["open.feishu.cn"]);

type UnknownRecord = Record<string, any>;
type RuntimeContext = any;

type Target = {
  recordId: string;
  logId: string;
};

type DispatchState = "accepted" | "already_running" | "already_current" | "failed" | "unknown" | "rejected";

type DispatchResult = {
  accepted: boolean;
  dispatchState: DispatchState;
  stage: string;
  errorCode: string;
  errorMessage: string;
  agentChatId: string;
  dispatchId: string;
  targetRecordId: string;
  mode: string;
  buildId: string;
  logId: string;
};

type MaterialBaseline = {
  documentToken: string;
  processedAttachmentIds: string[];
  contractVersion: string;
};

type MaterialDecision =
  | { kind: "initial"; attachmentIds: string[]; newAttachmentIds: string[]; caseNumber: string; uploaderOpenIds: string[] }
  | { kind: "supplement"; attachmentIds: string[]; newAttachmentIds: string[]; caseNumber: string; uploaderOpenIds: string[]; documentToken: string; reportUrl: string }
  | { kind: "no_op"; attachmentIds: string[]; newAttachmentIds: string[]; caseNumber: string; uploaderOpenIds: string[] };

class DispatchFailure extends Error {
  readonly code: string;
  readonly stage: string;
  readonly definitive: boolean;

  constructor(code: string, stage: string, message: string, definitive = true) {
    super(message);
    this.name = "DispatchFailure";
    this.code = code;
    this.stage = stage;
    this.definitive = definitive;
  }
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalarText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value).trim();
  if (Array.isArray(value)) {
    for (const item of value) {
      const text = scalarText(item);
      if (text) return text;
    }
    return "";
  }
  if (!isRecord(value)) return "";
  for (const key of ["value", "text", "name", "record_id", "recordId", "id"]) {
    const text = scalarText(value[key]);
    if (text) return text;
  }
  return "";
}

function safeMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/Bearer\s+\S+/gi, "Bearer <redacted>")
    .replace(/https?:\/\/\S+/gi, "<url>")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 180);
}

async function acquireRecordQueue(recordId: string): Promise<() => void> {
  const previous = RECORD_QUEUES.get(recordId) || Promise.resolve();
  let releaseCurrent!: () => void;
  const current = new Promise<void>((resolve) => { releaseCurrent = resolve; });
  const tail = previous.catch(() => undefined).then(() => current);
  RECORD_QUEUES.set(recordId, tail);
  await previous.catch(() => undefined);
  return () => {
    releaseCurrent();
    if (RECORD_QUEUES.get(recordId) === tail) RECORD_QUEUES.delete(recordId);
  };
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function runtimeLogId(context: RuntimeContext): string {
  return scalarText(context?.app?.logID || context?.app?.logId || context?.logID || context?.logId);
}

function tenantToken(context: RuntimeContext): string {
  const token = scalarText(context?.tenantAccessToken);
  if (!token) throw new DispatchFailure("CONTEXT_MISSING", "validate-runtime", "运行时缺少租户访问凭证");
  return token;
}

function targetRecordId(formItemParams: UnknownRecord): string {
  const recordId = scalarText(formItemParams?.targetRecordId || formItemParams?.targetRecordID);
  if (!recordId) throw new DispatchFailure("TARGET_RECORD_MISSING", "validate-input", "缺少目标案件记录 ID");
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(recordId)) {
    throw new DispatchFailure("TARGET_RECORD_INVALID", "validate-input", "目标案件记录 ID 格式不合法");
  }
  return recordId;
}

function validateRuntimeApp(context: RuntimeContext): void {
  const appToken = scalarText(context?.app?.token || context?.app?.baseToken);
  if (!appToken) throw new DispatchFailure("CONTEXT_MISSING", "validate-runtime", "运行时缺少 Base 标识");
  if (appToken !== PRODUCTION_APP_TOKEN) {
    throw new DispatchFailure("TARGET_NOT_ALLOWED", "validate-runtime", "当前 Base 不在生产目标白名单");
  }
}

async function fetchJson(
  context: RuntimeContext,
  token: string,
  url: string,
  options: UnknownRecord,
  stage: string,
): Promise<any> {
  if (typeof context?.fetch !== "function") {
    throw new DispatchFailure("CONTEXT_MISSING", stage, "运行时未提供受控网络请求能力");
  }
  const controller = typeof AbortController === "undefined" ? undefined : new AbortController();
  let timedOut = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const request = context.fetch(url, {
      ...options,
      headers: {
        authorization: `Bearer ${token}`,
        ...(options.headers || {}),
      },
      ...(controller ? { signal: controller.signal } : {}),
    });
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        controller?.abort();
        reject(new DispatchFailure("REQUEST_UNKNOWN", stage, `${stage} 超时`, false));
      }, REQUEST_TIMEOUT_MS);
    });
    const response: any = await Promise.race([request, timeout]);
    let body: any;
    try {
      body = await response.json();
    } catch {
      throw new DispatchFailure("RESPONSE_INVALID", stage, `${stage} 返回不可解析响应`, false);
    }
    const status = Number(response.status || 0);
    if (!response.ok || (body?.code !== undefined && String(body.code) !== "0")) {
      const detail = safeMessage(body?.msg || body?.message || response.statusText || `HTTP ${status}`);
      const definitive = (body?.code !== undefined && String(body.code) !== "0")
        || (status >= 400 && status < 500);
      throw new DispatchFailure(definitive ? "REQUEST_REJECTED" : "REQUEST_UNKNOWN", stage, detail, definitive);
    }
    return body?.data ?? body;
  } catch (error) {
    if (error instanceof DispatchFailure) throw error;
    if (timedOut || (error instanceof Error && error.name === "AbortError")) {
      throw new DispatchFailure("REQUEST_UNKNOWN", stage, `${stage} 超时`, false);
    }
    throw new DispatchFailure("REQUEST_UNKNOWN", stage, safeMessage(error) || `${stage} 请求结果未知`, false);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function recordFields(data: unknown): UnknownRecord {
  if (isRecord(data) && isRecord(data.record) && isRecord(data.record.fields)) return data.record.fields;
  if (isRecord(data) && isRecord(data.fields)) return data.fields;
  return {};
}

async function getRecord(context: RuntimeContext, token: string, target: Target): Promise<UnknownRecord> {
  const data = await fetchJson(
    context,
    token,
    `${OPEN_API}/bitable/v1/apps/${encodeURIComponent(PRODUCTION_APP_TOKEN)}/tables/${encodeURIComponent(PRODUCTION_TABLE_ID)}/records/${encodeURIComponent(target.recordId)}`,
    { method: "GET" },
    "read-target-record",
  );
  const fields = recordFields(data);
  if (!Object.keys(fields).length) {
    throw new DispatchFailure("RECORD_NOT_FOUND", "read-target-record", "目标案件记录未返回字段");
  }
  return fields;
}

async function getTableFields(context: RuntimeContext, token: string): Promise<UnknownRecord[]> {
  const data = await fetchJson(
    context,
    token,
    `${OPEN_API}/bitable/v1/apps/${encodeURIComponent(PRODUCTION_APP_TOKEN)}/tables/${encodeURIComponent(PRODUCTION_TABLE_ID)}/fields?page_size=100`,
    { method: "GET" },
    "read-table-schema",
  );
  const items = Array.isArray(data?.items) ? data.items : Array.isArray(data?.fields) ? data.fields : [];
  if (!items.length) throw new DispatchFailure("SCHEMA_UNAVAILABLE", "read-table-schema", "目标数据表未返回字段定义");
  return items.filter(isRecord);
}

async function updateRecord(
  context: RuntimeContext,
  token: string,
  target: Target,
  fields: UnknownRecord,
  stage: string,
): Promise<void> {
  await fetchJson(
    context,
    token,
    `${OPEN_API}/bitable/v1/apps/${encodeURIComponent(PRODUCTION_APP_TOKEN)}/tables/${encodeURIComponent(PRODUCTION_TABLE_ID)}/records/${encodeURIComponent(target.recordId)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fields }),
    },
    stage,
  );
}

function fieldValue(fields: UnknownRecord, name: string): unknown {
  return fields[name];
}

function attachmentId(value: unknown): string {
  if (!isRecord(value)) return "";
  for (const key of ["file_token", "fileToken", "token", "attachment_id", "attachmentId", "id"]) {
    const text = scalarText(value[key]);
    if (text) return text;
  }
  return "";
}

function attachmentIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map(attachmentId).filter(Boolean))].sort();
}

function uploaderOpenIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const ids = value.flatMap((item) => {
    if (!isRecord(item)) return [];
    for (const key of ["open_id", "openId", "member_id", "memberId", "id"]) {
      const id = scalarText(item[key]);
      if (/^ou_[A-Za-z0-9_-]+$/.test(id)) return [id];
    }
    return [];
  });
  return [...new Set(ids)].sort();
}

function isDocumentToken(value: string): boolean {
  return /^[A-Za-z0-9_-]{8,128}$/.test(value);
}

function parseBaseline(value: unknown): MaterialBaseline | undefined {
  const text = scalarText(value);
  if (!text) return undefined;
  try {
    const parsed = JSON.parse(text);
    if (!isRecord(parsed)) return undefined;
    const documentToken = scalarText(parsed.document_token);
    if (!isDocumentToken(documentToken)) return undefined;
    const rawIds = Array.isArray(parsed.processed_attachment_ids)
      ? parsed.processed_attachment_ids.map(scalarText)
      : Array.isArray(parsed.processed_attachments)
        ? parsed.processed_attachments.map((item: unknown) => isRecord(item) ? scalarText(item.attachment_id) : "")
        : [];
    const processedAttachmentIds = [...new Set(rawIds.filter(Boolean))].sort();
    if (!processedAttachmentIds.length) return undefined;
    const contractVersion = scalarText(parsed.contract_version);
    return { documentToken, processedAttachmentIds, contractVersion };
  } catch {
    return undefined;
  }
}

function decideMaterials(fields: UnknownRecord): MaterialDecision {
  const attachmentIdList = attachmentIds(fieldValue(fields, FIELD_ATTACHMENTS));
  if (!attachmentIdList.length) throw new DispatchFailure("ATTACHMENTS_MISSING", "inspect-materials", "案件文档为空");
  const uploaderIdList = uploaderOpenIds(fieldValue(fields, FIELD_UPLOADER));
  if (!uploaderIdList.length) {
    throw new DispatchFailure("UPLOADER_OPEN_ID_MISSING", "inspect-materials", "上传人缺少可授权的 open_id");
  }
  const caseNumber = scalarText(fieldValue(fields, FIELD_CASE_NUMBER));
  if (!caseNumber) throw new DispatchFailure("CASE_NUMBER_MISSING", "inspect-materials", "案件编号为空");

  const reportField = fieldValue(fields, FIELD_ANALYSIS_RESULT);
  const baselineField = fieldValue(fields, BASELINE_FIELD_NAME);
  const baseline = parseBaseline(baselineField);
  const hasReportField = hasMeaningfulFieldValue(reportField);
  if (!hasReportField && !hasMeaningfulFieldValue(baselineField)) {
    return {
      kind: "initial",
      attachmentIds: attachmentIdList,
      newAttachmentIds: attachmentIdList,
      caseNumber,
      uploaderOpenIds: uploaderIdList,
    };
  }
  const report = baseline ? resolveReportDocxReference(reportField, baseline.documentToken) : undefined;
  if (!baseline || !report) {
    throw new DispatchFailure("REPORT_STATE_INVALID", "inspect-materials", "当前报告链接与材料处理基线不一致");
  }
  const reportUrl = report.url;
  const currentSet = new Set(attachmentIdList);
  if (baseline.processedAttachmentIds.some((id) => !currentSet.has(id))) {
    throw new DispatchFailure("ATTACHMENT_SET_CHANGED", "inspect-materials", "已有附件被删除或替换");
  }
  if (baseline.contractVersion !== REQUIRED_SKILL_VERSION) {
    return {
      kind: "supplement",
      attachmentIds: attachmentIdList,
      newAttachmentIds: attachmentIdList,
      caseNumber,
      uploaderOpenIds: uploaderIdList,
      documentToken: baseline.documentToken,
      reportUrl,
    };
  }
  const processed = new Set(baseline.processedAttachmentIds);
  const newAttachmentIds = attachmentIdList.filter((id) => !processed.has(id));
  if (!newAttachmentIds.length) {
    return {
      kind: "no_op",
      attachmentIds: attachmentIdList,
      newAttachmentIds: [],
      caseNumber,
      uploaderOpenIds: uploaderIdList,
    };
  }
  return {
    kind: "supplement",
    attachmentIds: attachmentIdList,
    newAttachmentIds,
    caseNumber,
    uploaderOpenIds: uploaderIdList,
    documentToken: baseline.documentToken,
    reportUrl,
  };
}

function makeDispatchId(recordId: string): string {
  const stamp = Date.now();
  const nonce = Math.random().toString(36).slice(2, 8);
  return `odm-v64:${recordId}:${stamp}:${nonce}`;
}

function taskLog(dispatchId: string, message: string): string {
  return `任务 ${dispatchId}：${message}`;
}

function schemaName(field: UnknownRecord): string {
  return scalarText(field.field_name || field.fieldName || field.name);
}

function schemaId(field: UnknownRecord): string {
  return scalarText(field.field_id || field.fieldId || field.id);
}

function schemaType(field: UnknownRecord): string {
  return scalarText(field.type || field.field_type || field.fieldType);
}

const FIELD_TYPE_ALIASES: Record<string, string[]> = {
  text: ["text", "1"],
  select: ["select", "3"],
  datetime: ["datetime", "5"],
  user: ["user", "11"],
  attachment: ["attachment", "17"],
  auto_number: ["auto_number", "1005"],
};

function validateSchema(fields: UnknownRecord[]): void {
  const byName = new Map(fields.map((field) => [schemaName(field), field]));
  for (const contract of Object.values(PRODUCTION_FIELD_CONTRACT)) {
    const actual = byName.get(contract.name);
    if (!actual) throw new DispatchFailure("FIELD_CONTRACT_MISMATCH", "validate-schema", `缺少字段：${contract.name}`);
    if (schemaId(actual) !== contract.id) {
      throw new DispatchFailure("FIELD_CONTRACT_MISMATCH", "validate-schema", `字段 ID 不匹配：${contract.name}`);
    }
    const actualType = schemaType(actual);
    const allowedTypes = FIELD_TYPE_ALIASES[contract.type] || [contract.type];
    if (actualType && !allowedTypes.includes(actualType)) {
      throw new DispatchFailure("FIELD_CONTRACT_MISMATCH", "validate-schema", `字段类型不匹配：${contract.name}`);
    }
  }
}

async function writeProcessing(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
  mode: DispatchMode,
): Promise<void> {
  await updateRecord(context, token, target, {
    [FIELD_PROCESSING_STATUS]: "分析中",
    [FIELD_EXECUTION_LOG]: taskLog(dispatchId, "处理中"),
    ...(mode === "initial" ? {
      [FIELD_ANALYSIS_RESULT]: "",
      [FIELD_MATERIAL_BASELINE]: "",
    } : {}),
  }, "mark-processing");
  const readback = await getRecord(context, token, target);
  if (scalarText(fieldValue(readback, FIELD_PROCESSING_STATUS)) !== "分析中") {
    throw new DispatchFailure("BASE_READBACK_FAILED", "mark-processing", "分析中状态读回不一致");
  }
  if (scalarText(fieldValue(readback, FIELD_EXECUTION_LOG)) !== taskLog(dispatchId, "处理中")) {
    throw new DispatchFailure("BASE_READBACK_FAILED", "mark-processing", "任务标识读回不一致");
  }
}

async function markFailure(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
  code: string,
  clearReport = false,
): Promise<boolean> {
  await updateRecord(context, token, target, {
    [FIELD_PROCESSING_STATUS]: "分析失败",
    [FIELD_EXECUTION_LOG]: taskLog(dispatchId, `失败：${code}`),
    ...(clearReport ? {
      [FIELD_ANALYSIS_RESULT]: "",
      [FIELD_MATERIAL_BASELINE]: "",
    } : {}),
  }, "mark-failure");
  const readback = await getRecord(context, token, target);
  return scalarText(fieldValue(readback, FIELD_PROCESSING_STATUS)) === "分析失败"
    && scalarText(fieldValue(readback, FIELD_EXECUTION_LOG)).includes(code);
}

async function currentAttemptOwnsRecord(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
): Promise<boolean> {
  const fields = await getRecord(context, token, target);
  return scalarText(fieldValue(fields, FIELD_PROCESSING_STATUS)) === "分析中"
    && scalarText(fieldValue(fields, FIELD_EXECUTION_LOG)) === taskLog(dispatchId, "处理中");
}

async function writeFailureIfOwned(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
  code: string,
  clearReport: boolean,
): Promise<boolean> {
  if (!(await currentAttemptOwnsRecord(context, token, target, dispatchId))) return false;
  return markFailure(context, token, target, dispatchId, code, clearReport);
}

function extractChatId(data: unknown): string {
  if (!isRecord(data)) return "";
  return scalarText(data.agent_chat_id || data.chat_id || data.chat?.id);
}

async function createAgentChat(
  context: RuntimeContext,
  token: string,
  prompt: string,
): Promise<string> {
  const data = await fetchJson(
    context,
    token,
    AILY_CHATS_URL,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ user_message: { content: [{ type: "text", text: prompt }] } }),
    },
    "create-agent-chat",
  );
  const chatId = extractChatId(data);
  if (!chatId) {
    throw new DispatchFailure("AILY_CHAT_RESPONSE_INVALID", "create-agent-chat", "智能体接口未返回会话标识", false);
  }
  return chatId;
}

function resultBase(target?: Partial<Target>) {
  return {
    targetRecordId: target?.recordId || "",
    buildId: BUILD_ID,
    logId: target?.logId || "",
  };
}

function result(
  target: Target | undefined,
  dispatchId: string,
  values: Omit<DispatchResult, "targetRecordId" | "buildId" | "logId" | "dispatchId">,
): DispatchResult {
  return { ...values, dispatchId, ...resultBase(target) };
}

basekit.addAction({
  description: "读取当前案件附件，核验图片并转写音频，使用 Skill 固定模板生成报告并回写同一记录。",
  actionText: "提交案件材料分析任务",
  permission: { type: 2 },
  useTenantAccessToken: true,
  formItems: [
    {
      itemId: "targetRecordId",
      label: "目标案件记录 ID",
      component: Component.Input,
      required: true,
      paramType: ParamType.String,
      componentProps: { placeholder: "映射当前案件记录或第 1 步新增案件记录的 Record ID" },
    },
  ],
  resultType: {
    type: ParamType.Object,
    properties: {
      accepted: { type: ParamType.Boolean, label: "任务已接收" },
      dispatchState: { type: ParamType.String, label: "投递状态" },
      stage: { type: ParamType.String, label: "执行阶段" },
      errorCode: { type: ParamType.String, label: "错误代码" },
      errorMessage: { type: ParamType.String, label: "错误信息" },
      agentChatId: { type: ParamType.String, label: "智能体会话 ID" },
      dispatchId: { type: ParamType.String, label: "任务 ID" },
      targetRecordId: { type: ParamType.String, label: "目标案件记录 ID" },
      mode: { type: ParamType.String, label: "处理模式" },
      buildId: { type: ParamType.String, label: "组件构建标识" },
      logId: { type: ParamType.String, label: "运行日志 ID" },
    },
  },
  execute: async (formItemParams: UnknownRecord, context: RuntimeContext) => {
    let target: Target | undefined;
    let token = "";
    let dispatchId = "";
    let mode: DispatchMode | "" = "";
    let releaseQueue: (() => void) | undefined;
    try {
      validateRuntimeApp(context);
      const recordId = targetRecordId(formItemParams);
      releaseQueue = await acquireRecordQueue(recordId);
      target = { recordId, logId: runtimeLogId(context) };
      token = tenantToken(context);
      validateSchema(await getTableFields(context, token));
      const fields = await getRecord(context, token, target);
      const status = scalarText(fieldValue(fields, FIELD_PROCESSING_STATUS));
      if (status === "分析中") {
        return result(target, "", {
          accepted: true,
          dispatchState: "already_running",
          stage: "inspect-materials",
          errorCode: "",
          errorMessage: "",
          agentChatId: "",
          mode: "",
        });
      }

      let decision: MaterialDecision;
      try {
        decision = decideMaterials(fields);
      } catch (error) {
        const failure = error instanceof DispatchFailure
          ? error
          : new DispatchFailure("MATERIAL_PREFLIGHT_FAILED", "inspect-materials", safeMessage(error));
        dispatchId = makeDispatchId(target.recordId);
        const written = await markFailure(context, token, target, dispatchId, failure.code, false);
        return result(target, dispatchId, {
          accepted: false,
          dispatchState: "failed",
          stage: failure.stage,
          errorCode: written ? failure.code : "BASE_READBACK_FAILED",
          errorMessage: written ? safeMessage(failure) : "失败状态读回不一致",
          agentChatId: "",
          mode: "",
        });
      }

      if (decision.kind === "no_op") {
        return result(target, "", {
          accepted: true,
          dispatchState: "already_current",
          stage: "inspect-materials",
          errorCode: "",
          errorMessage: "",
          agentChatId: "",
          mode: "no_op",
        });
      }

      mode = decision.kind;
      dispatchId = makeDispatchId(target.recordId);
      await writeProcessing(context, token, target, dispatchId, mode);
      const prompt = buildAnalysisPrompt({
        targetRecordId: target.recordId,
        dispatchId,
        mode,
        caseNumber: decision.caseNumber,
        attachmentIds: decision.attachmentIds,
        newAttachmentIds: decision.newAttachmentIds,
        uploaderOpenIds: decision.uploaderOpenIds,
        existingDocumentToken: decision.kind === "supplement" ? decision.documentToken : "",
        existingReportUrl: decision.kind === "supplement" ? decision.reportUrl : "",
        componentBuild: BUILD_ID,
      });

      try {
        await sleep(500);
        if (!(await currentAttemptOwnsRecord(context, token, target, dispatchId))) {
          throw new DispatchFailure("BASE_STATE_CONFLICT", "create-agent-chat", "任务锁已被其他运行接管");
        }
        const chatId = await createAgentChat(context, token, prompt);
        return result(target, dispatchId, {
          accepted: true,
          dispatchState: "accepted",
          stage: "create-agent-chat",
          errorCode: "",
          errorMessage: "",
          agentChatId: chatId,
          mode,
        });
      } catch (error) {
        const failure = error instanceof DispatchFailure
          ? error
          : new DispatchFailure("AILY_CHAT_UNKNOWN", "create-agent-chat", safeMessage(error), false);
        if (failure.definitive) {
          const written = await writeFailureIfOwned(context, token, target, dispatchId, failure.code, mode === "initial");
          return result(target, dispatchId, {
            accepted: false,
            dispatchState: "failed",
            stage: failure.stage,
            errorCode: written ? failure.code : "BASE_STATE_CONFLICT",
            errorMessage: written ? safeMessage(failure) : "任务状态已被其他运行接管",
            agentChatId: "",
            mode,
          });
        }
        return result(target, dispatchId, {
          accepted: false,
          dispatchState: "unknown",
          stage: failure.stage,
          errorCode: failure.code,
          errorMessage: safeMessage(failure),
          agentChatId: "",
          mode,
        });
      }
    } catch (error) {
      const failure = error instanceof DispatchFailure
        ? error
        : new DispatchFailure("COMPONENT_INTERNAL_ERROR", "dispatch", safeMessage(error));
      if (target && token && failure.definitive && dispatchId) {
        try {
          await writeFailureIfOwned(context, token, target, dispatchId, failure.code, mode === "initial");
        } catch {}
      }
      return result(target, dispatchId, {
        accepted: false,
        dispatchState: failure.definitive ? "failed" : "unknown",
        stage: failure.stage,
        errorCode: failure.code,
        errorMessage: safeMessage(failure),
        agentChatId: "",
        mode,
      });
    } finally {
      releaseQueue?.();
    }
  },
});

export default basekit;
