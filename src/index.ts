import {
  basekit,
  Component,
  ParamType,
} from "@lark-opdev/block-basekit-server-api";
import {
  BASELINE_FIELD_NAME,
  buildAnalysisPrompt,
  REPORT_CONTRACT_VERSION,
  DispatchMode,
  PRODUCTION_APP_TOKEN,
  TEMPLATE_DOCUMENT_TOKEN,
  PRODUCTION_FIELD_CONTRACT,
  PRODUCTION_TABLE_ID,
} from "./analysis";

const OPEN_API = "https://open.feishu.cn/open-apis";
const ORGANIZER_AGENT_ID = "agent_4kuakyp7zsa2xuc";
const BUILD_ID = "5.3.1-permission-gated-report";
const REQUEST_TIMEOUT_MS = 10_000;
const RUNNING_TTL_MS = 30 * 60 * 1000;
const AILY_CHATS_URL = `${OPEN_API}/aily/v1/agents/${ORGANIZER_AGENT_ID}/chats`;

const FIELD_CASE_NUMBER = PRODUCTION_FIELD_CONTRACT.case_number.name;
const FIELD_ATTACHMENTS = PRODUCTION_FIELD_CONTRACT.attachments.name;
const FIELD_UPLOADER = PRODUCTION_FIELD_CONTRACT.uploader.name;
const FIELD_PROCESSING_STATUS = PRODUCTION_FIELD_CONTRACT.processing_status.name;
const FIELD_ANALYSIS_RESULT = PRODUCTION_FIELD_CONTRACT.analysis_result.name;
const FIELD_EXECUTION_LOG = PRODUCTION_FIELD_CONTRACT.execution_log.name;

basekit.addDomainList(["open.feishu.cn"]);

type UnknownRecord = Record<string, any>;
type RuntimeContext = any;

type Target = {
  recordId: string;
  logId: string;
};

type DispatchState = "accepted" | "already_running" | "already_current" | "review_required" | "failed" | "unknown" | "rejected";

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

type AttachmentBaseline = {
  attachment_id: string;
  size?: number;
};

type MaterialBaseline = {
  version: 3;
  document_token: string;
  template_document_token: string;
  report_contract_version: string;
  processed_attachments: AttachmentBaseline[];
};

type MaterialDecision =
  | { kind: "initial"; newAttachmentIds: string[] }
  | { kind: "supplement"; newAttachmentIds: string[] }
  | { kind: "no_op"; newAttachmentIds: string[] }
  | { kind: "reconcile"; newAttachmentIds: string[] };

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

function uploaderCount(value: unknown): number {
  return Array.isArray(value) ? value.filter((item) => Boolean(scalarText(item))).length : 0;
}

function isHttpsDocxUrl(value: string): boolean {
  return /^https:\/\/[^/]+\/docx\/[A-Za-z0-9_-]+$/.test(value);
}

function parseBaseline(value: unknown): MaterialBaseline | undefined {
  const text = scalarText(value);
  if (!text) return undefined;
  try {
    const parsed = JSON.parse(text);
    if (
      !isRecord(parsed)
      || parsed.version !== 3
      || typeof parsed.document_token !== "string"
      || parsed.template_document_token !== TEMPLATE_DOCUMENT_TOKEN
      || parsed.report_contract_version !== REPORT_CONTRACT_VERSION
    ) return undefined;
    if (!Array.isArray(parsed.processed_attachments)) return undefined;
    const attachments = parsed.processed_attachments
      .filter(isRecord)
      .map((item) => ({ attachment_id: scalarText(item.attachment_id), ...(Number.isFinite(Number(item.size)) ? { size: Number(item.size) } : {}) }))
      .filter((item) => item.attachment_id);
    return {
      version: 3,
      document_token: parsed.document_token,
      template_document_token: TEMPLATE_DOCUMENT_TOKEN,
      report_contract_version: REPORT_CONTRACT_VERSION,
      processed_attachments: attachments,
    };
  } catch {
    return undefined;
  }
}

function decideMaterials(fields: UnknownRecord): MaterialDecision {
  const currentIds = attachmentIds(fieldValue(fields, FIELD_ATTACHMENTS));
  if (!currentIds.length) throw new DispatchFailure("ATTACHMENTS_MISSING", "inspect-materials", "案件文档为空");
  if (!uploaderCount(fieldValue(fields, FIELD_UPLOADER))) {
    throw new DispatchFailure("UPLOADER_MISSING", "inspect-materials", "上传人为空");
  }
  if (!scalarText(fieldValue(fields, FIELD_CASE_NUMBER))) {
    throw new DispatchFailure("CASE_NUMBER_MISSING", "inspect-materials", "案件编号为空");
  }

  const resultUrl = scalarText(fieldValue(fields, FIELD_ANALYSIS_RESULT));
  const baselineRaw = scalarText(fieldValue(fields, BASELINE_FIELD_NAME));
  const baseline = parseBaseline(baselineRaw);
  if (!baseline) {
    return { kind: "initial", newAttachmentIds: currentIds };
  }
  if (!isHttpsDocxUrl(resultUrl) || !resultUrl.includes(`/${baseline.document_token}`)) {
    return { kind: "reconcile", newAttachmentIds: [] };
  }
  const previousIds = new Set(baseline.processed_attachments.map((item) => item.attachment_id));
  const currentSet = new Set(currentIds);
  if ([...previousIds].some((id) => !currentSet.has(id))) {
    return { kind: "reconcile", newAttachmentIds: [] };
  }
  const newAttachmentIds = currentIds.filter((id) => !previousIds.has(id));
  return newAttachmentIds.length
    ? { kind: "supplement", newAttachmentIds }
    : { kind: "no_op", newAttachmentIds: [] };
}

function makeDispatchId(recordId: string): string {
  const stamp = Date.now();
  const nonce = Math.random().toString(36).slice(2, 8);
  return `odm-v3:${recordId}:${stamp}:${nonce}`;
}

function taskLog(dispatchId: string, message: string): string {
  return `任务 ${dispatchId}：${message}`;
}

function dispatchStartedAt(log: string): number | undefined {
  const match = log.match(/任务\s+odm-v3:[A-Za-z0-9_-]+:([0-9]{13}):[A-Za-z0-9]+：/);
  if (!match) return undefined;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : undefined;
}

function schemaName(field: UnknownRecord): string {
  return scalarText(field.field_name || field.fieldName || field.name);
}

function schemaId(field: UnknownRecord): string {
  return scalarText(field.field_id || field.fieldId || field.id);
}

function validateSchema(fields: UnknownRecord[]): void {
  const byName = new Map(fields.map((field) => [schemaName(field), field]));
  for (const field of Object.values(PRODUCTION_FIELD_CONTRACT)) {
    const actual = byName.get(field.name);
    if (!actual) throw new DispatchFailure("FIELD_CONTRACT_MISMATCH", "validate-schema", `缺少字段：${field.name}`);
    if (field.id !== "dynamic" && schemaId(actual) !== field.id) {
      throw new DispatchFailure("FIELD_CONTRACT_MISMATCH", "validate-schema", `字段 ID 不匹配：${field.name}`);
    }
  }
}

async function writeAndReadProcessing(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
  mode: DispatchMode,
): Promise<void> {
  await updateRecord(context, token, target, {
    [FIELD_PROCESSING_STATUS]: "分析中",
    [FIELD_EXECUTION_LOG]: taskLog(dispatchId, "处理中"),
    ...(mode === "initial" ? { [FIELD_ANALYSIS_RESULT]: "" } : {}),
  }, "mark-processing");
  const readback = await getRecord(context, token, target);
  if (scalarText(fieldValue(readback, FIELD_PROCESSING_STATUS)) !== "分析中") {
    throw new DispatchFailure("BASE_READBACK_FAILED", "mark-processing", "分析中状态读回不一致");
  }
  if (!scalarText(fieldValue(readback, FIELD_EXECUTION_LOG)).includes(dispatchId)) {
    throw new DispatchFailure("BASE_READBACK_FAILED", "mark-processing", "任务标识读回不一致");
  }
}

async function currentAttemptOwnsRecord(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
): Promise<boolean> {
  const fields = await getRecord(context, token, target);
  return scalarText(fieldValue(fields, FIELD_PROCESSING_STATUS)) === "分析中"
    && scalarText(fieldValue(fields, FIELD_EXECUTION_LOG)).includes(dispatchId);
}

async function writeFailureIfOwned(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
  code: string,
): Promise<boolean> {
  if (!(await currentAttemptOwnsRecord(context, token, target, dispatchId))) return false;
  await updateRecord(context, token, target, {
    [FIELD_PROCESSING_STATUS]: "分析失败",
    [FIELD_EXECUTION_LOG]: taskLog(dispatchId, `失败：${code}`),
  }, "mark-dispatch-failure");
  const readback = await getRecord(context, token, target);
  return scalarText(fieldValue(readback, FIELD_PROCESSING_STATUS)) === "分析失败"
    && scalarText(fieldValue(readback, FIELD_EXECUTION_LOG)).includes(code);
}

async function writeReview(
  context: RuntimeContext,
  token: string,
  target: Target,
  message: string,
): Promise<void> {
  await updateRecord(context, token, target, {
    [FIELD_PROCESSING_STATUS]: "待法务审核",
    [FIELD_EXECUTION_LOG]: message,
  }, "mark-review-required");
  const readback = await getRecord(context, token, target);
  if (scalarText(fieldValue(readback, FIELD_PROCESSING_STATUS)) !== "待法务审核") {
    throw new DispatchFailure("BASE_READBACK_FAILED", "mark-review-required", "待法务审核状态读回不一致");
  }
}

async function writePreflightFailure(
  context: RuntimeContext,
  token: string,
  target: Target,
  dispatchId: string,
  code: string,
): Promise<boolean> {
  await updateRecord(context, token, target, {
    [FIELD_PROCESSING_STATUS]: "分析失败",
    [FIELD_EXECUTION_LOG]: taskLog(dispatchId, `失败：${code}`),
  }, "mark-preflight-failure");
  const readback = await getRecord(context, token, target);
  return scalarText(fieldValue(readback, FIELD_PROCESSING_STATUS)) === "分析失败"
    && scalarText(fieldValue(readback, FIELD_EXECUTION_LOG)).includes(code);
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

function resultBase(target?: Partial<Target>): Omit<DispatchResult, "accepted" | "dispatchState" | "stage" | "errorCode" | "errorMessage" | "agentChatId" | "dispatchId" | "mode"> {
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
  description: "锁定目标案件记录，写入分析中并向纠纷材料整理智能体投递一次任务。",
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
    try {
      validateRuntimeApp(context);
      target = { recordId: targetRecordId(formItemParams), logId: runtimeLogId(context) };
      token = tenantToken(context);
      validateSchema(await getTableFields(context, token));
      const fields = await getRecord(context, token, target);
      const status = scalarText(fieldValue(fields, FIELD_PROCESSING_STATUS));
      const currentLog = scalarText(fieldValue(fields, FIELD_EXECUTION_LOG));
      if (status === "分析中") {
        const startedAt = dispatchStartedAt(currentLog);
        if (!startedAt || Date.now() - startedAt < RUNNING_TTL_MS) {
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
      }
      let decision: MaterialDecision;
      try {
        decision = decideMaterials(fields);
      } catch (error) {
        const failure = error instanceof DispatchFailure
          ? error
          : new DispatchFailure("MATERIAL_PREFLIGHT_FAILED", "inspect-materials", safeMessage(error));
        dispatchId = makeDispatchId(target.recordId);
        const written = await writePreflightFailure(context, token, target, dispatchId, failure.code);
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
      if (decision.kind === "reconcile") {
        await writeReview(context, token, target, "材料发生替换、删除或基线异常，需要确认是否全文重整");
        return result(target, "", {
          accepted: false,
          dispatchState: "review_required",
          stage: "inspect-materials",
          errorCode: "MATERIAL_BASELINE_RECONCILE_REQUIRED",
          errorMessage: "材料基线与当前附件不一致",
          agentChatId: "",
          mode: "reconcile",
        });
      }

      dispatchId = makeDispatchId(target.recordId);
      await writeAndReadProcessing(context, token, target, dispatchId, decision.kind as DispatchMode);
      const prompt = buildAnalysisPrompt({
        targetRecordId: target.recordId,
        dispatchId,
        mode: decision.kind as DispatchMode,
        newAttachmentIds: decision.newAttachmentIds,
        componentBuild: BUILD_ID,
      });

      try {
        const chatId = await createAgentChat(context, token, prompt);
        return result(target, dispatchId, {
          accepted: true,
          dispatchState: "accepted",
          stage: "create-agent-chat",
          errorCode: "",
          errorMessage: "",
          agentChatId: chatId,
          mode: decision.kind,
        });
      } catch (error) {
        const failure = error instanceof DispatchFailure
          ? error
          : new DispatchFailure("AILY_CHAT_UNKNOWN", "create-agent-chat", safeMessage(error), false);
        if (failure.definitive) {
          const written = await writeFailureIfOwned(context, token, target, dispatchId, failure.code);
          return result(target, dispatchId, {
            accepted: false,
            dispatchState: "failed",
            stage: failure.stage,
            errorCode: written ? failure.code : "BASE_STATE_CONFLICT",
            errorMessage: written ? safeMessage(failure) : "任务状态已被其他运行接管",
            agentChatId: "",
            mode: decision.kind,
          });
        }
        return result(target, dispatchId, {
          accepted: false,
          dispatchState: "unknown",
          stage: failure.stage,
          errorCode: failure.code,
          errorMessage: safeMessage(failure),
          agentChatId: "",
          mode: decision.kind,
        });
      }
    } catch (error) {
      const failure = error instanceof DispatchFailure
        ? error
        : new DispatchFailure("COMPONENT_INTERNAL_ERROR", "dispatch", safeMessage(error));
      if (target && token && failure.definitive && dispatchId) {
        try {
          await writeFailureIfOwned(context, token, target, dispatchId, failure.code);
        } catch {}
      }
      return result(target, dispatchId, {
        accepted: false,
        dispatchState: failure.definitive ? "failed" : "unknown",
        stage: failure.stage,
        errorCode: failure.code,
        errorMessage: safeMessage(failure),
        agentChatId: "",
        mode: "",
      });
    }
  },
});

export default basekit;
