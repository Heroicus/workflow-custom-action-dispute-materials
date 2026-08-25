import {
  basekit,
  Component,
  ParamType,
} from "@lark-opdev/block-basekit-server-api";
import { buildAnalysisPrompt } from "./analysis";

/**
 * Dispatches exactly one idempotent handoff to the published agent.
 *
 * The widget does not read or upload attachment bytes. It does, however, lock
 * the exact Base record before dispatching so that the asynchronous agent can
 * never guess a record by case number alone. The agent remains responsible for
 * the long-running document, permission and final-writeback work.
 */
const OPEN_API = "https://open.feishu.cn/open-apis";
const ORGANIZER_AGENT_ID = "agent_4kuakyp7zsa2xuc";
const BUILD_ID = "3.3.3-record-id-only-dispatch";
const API_REQUEST_TIMEOUT_MS = 45_000;

const FIELD_CASE_NUMBER = "案件编号";
const FIELD_MATERIALS = "上传材料";
const FIELD_UPLOADER = "上传人";
const FIELD_STATUS = "状态";
const FIELD_RESULT = "AI分析结果";
const FIELD_FAILURE = "执行日志/失败原因";

basekit.addDomainList([
  "open.feishu.cn",
  "feishu.cn",
  "larkoffice.com",
  "anyclaw-tos.feishucdn.com",
]);

type FetchResponse = {
  ok: boolean;
  status: number;
  statusText: string;
  json: () => Promise<any>;
};

type RuntimeContext = any;
type UnknownRecord = Record<string, any>;

type Target = {
  baseToken: string;
  tableId: string;
  recordId: string;
  logId: string;
};

type RecordSnapshot = {
  record: UnknownRecord;
  fields: UnknownRecord;
};

class DoNotOverwriteFailure extends Error {}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalarText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") {
    return String(value).trim();
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const result = scalarText(item);
      if (result) return result;
    }
    return "";
  }
  if (!isRecord(value)) return "";
  for (const key of [
    "value",
    "text",
    "name",
    "record_id",
    "recordId",
    "id",
    "open_id",
    "openId",
  ]) {
    const result = scalarText(value[key]);
    if (result) return result;
  }
  return "";
}

function fieldValue(fields: UnknownRecord, name: string): unknown {
  return fields[name];
}

function attachmentCount(value: unknown): number {
  if (Array.isArray(value)) return value.length;
  if (isRecord(value) && Array.isArray(value.value)) return value.value.length;
  return 0;
}

function uploaderOpenId(value: unknown): string {
  if (Array.isArray(value)) {
    for (const item of value) {
      const id = uploaderOpenId(item);
      if (id) return id;
    }
    return "";
  }
  if (!isRecord(value)) return scalarText(value);
  return scalarText(value.open_id || value.openId || value.id || value.value);
}

function logId(context: RuntimeContext): string {
  return scalarText(
    context?.app?.logID || context?.app?.logId || context?.logID || context?.logId,
  );
}

function obtainTenantToken(context: RuntimeContext): string {
  const token = scalarText(context?.tenantAccessToken);
  if (!token) {
    throw new Error(
      "缺少租户访问凭证：请确认组件版本已发布，并在工作流中启用应用身份与 Aily 会话调用权限。",
    );
  }
  return token;
}

function obtainBaseToken(context: RuntimeContext): string {
  const token = scalarText(context?.app?.token || context?.app?.baseToken);
  if (!token) throw new Error("缺少当前 Base 标识，无法锁定目标记录。");
  return token;
}

function resolveTableId(formItemParams: UnknownRecord, context: RuntimeContext): string {
  const tableId = scalarText(
    formItemParams?.targetTableId ||
      formItemParams?.targetTableID ||
      context?.app?.trigger?.tableID ||
      context?.app?.trigger?.tableId,
  );
  if (!tableId) throw new Error("缺少当前数据表标识，无法锁定目标记录。");
  return tableId;
}

function resolveRecordId(formItemParams: UnknownRecord, context: RuntimeContext): string {
  const recordId = scalarText(
    formItemParams?.recordId ||
      formItemParams?.recordID ||
      context?.app?.trigger?.recordID ||
      context?.app?.trigger?.recordId,
  );
  if (!recordId) {
    throw new Error(
      "缺少目标记录 ID：请把新增记录/当前触发记录的 record_id 映射到组件，禁止仅凭案件编号猜测记录。",
    );
  }
  return recordId;
}

async function fetchWithTimeout(
  context: RuntimeContext,
  url: string,
  options: UnknownRecord,
  label: string,
): Promise<FetchResponse> {
  if (typeof context?.fetch !== "function") throw new Error("运行时未提供受控网络请求能力。");
  if (context?.__testNoTimeout) return context.fetch(url, options);
  const timeoutMs = Math.max(
    1_000,
    Number(context?.__testRequestTimeoutMs || API_REQUEST_TIMEOUT_MS),
  );
  const controller =
    typeof AbortController === "undefined" ? undefined : new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const request = context.fetch(url, {
      ...options,
      ...(controller ? { signal: controller.signal } : {}),
    });
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller?.abort();
        reject(new Error(`${label}超时：${Math.ceil(timeoutMs / 1000)} 秒内未收到响应`));
      }, timeoutMs);
    });
    return await Promise.race([request, timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function responseJson(response: FetchResponse, label: string): Promise<any> {
  let body: any;
  try {
    body = await response.json();
  } catch {
    throw new Error(`${label}失败：服务返回了不可解析的响应（HTTP ${response.status}）。`);
  }
  const code = body?.code;
  if (!response.ok || (code !== undefined && String(code) !== "0")) {
    const detail = scalarText(body?.msg || body?.message || response.statusText || response.status);
    throw new Error(`${label}失败：${detail}`);
  }
  return body?.data ?? body;
}

async function apiJson(
  context: RuntimeContext,
  token: string,
  path: string,
  options: UnknownRecord,
  label: string,
): Promise<any> {
  const response = await fetchWithTimeout(
    context,
    `${OPEN_API}${path}`,
    {
      ...options,
      headers: {
        authorization: `Bearer ${token}`,
        ...(options.headers || {}),
      },
    },
    label,
  );
  return responseJson(response, label);
}

async function getRecord(
  context: RuntimeContext,
  token: string,
  target: Pick<Target, "baseToken" | "tableId" | "recordId">,
): Promise<RecordSnapshot> {
  const data = await apiJson(
    context,
    token,
    `/bitable/v1/apps/${encodeURIComponent(target.baseToken)}/tables/${encodeURIComponent(
      target.tableId,
    )}/records/${encodeURIComponent(target.recordId)}`,
    { method: "GET" },
    `读取目标记录 ${target.recordId}`,
  );
  const record = isRecord(data?.record) ? data.record : isRecord(data) ? data : {};
  const fields = isRecord(record.fields) ? record.fields : {};
  if (!Object.keys(fields).length) {
    throw new Error(`目标记录 ${target.recordId} 未返回字段，无法安全继续。`);
  }
  return { record, fields };
}

async function updateRecord(
  context: RuntimeContext,
  token: string,
  target: Pick<Target, "baseToken" | "tableId" | "recordId">,
  fields: UnknownRecord,
): Promise<void> {
  await apiJson(
    context,
    token,
    `/bitable/v1/apps/${encodeURIComponent(target.baseToken)}/tables/${encodeURIComponent(
      target.tableId,
    )}/records/${encodeURIComponent(target.recordId)}`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ fields }),
    },
    `更新目标记录 ${target.recordId}`,
  );
}

function extractChatId(data: any): string {
  return scalarText(data?.agent_chat_id || data?.chat_id || data?.chat?.id);
}

function extractExistingChatId(value: unknown): string {
  const text = scalarText(value);
  const match = text.match(/chatId=([A-Za-z0-9_-]+)/);
  return match?.[1] || "";
}

function safeReason(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw
    .replace(/Bearer\s+\S+/gi, "Bearer <redacted>")
    .replace(/https?:\/\/\S+/gi, "<url>")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 280);
}

async function markFailure(
  context: RuntimeContext,
  token: string,
  target: Target,
  reason: string,
): Promise<void> {
  await updateRecord(context, token, target, {
    [FIELD_RESULT]: null,
    [FIELD_STATUS]: "分析失败",
    [FIELD_FAILURE]: `build=${BUILD_ID}; stage=dispatch; ${safeReason(reason)}`,
  });
  const readback = await getRecord(context, token, target);
  const status = scalarText(fieldValue(readback.fields, FIELD_STATUS));
  if (status !== "分析失败") {
    throw new Error(`失败状态回读不一致：实际为 ${status || "空"}。`);
  }
}

async function markDispatching(
  context: RuntimeContext,
  token: string,
  target: Target,
): Promise<void> {
  await updateRecord(context, token, target, {
    [FIELD_STATUS]: "分析中",
    [FIELD_FAILURE]: `build=${BUILD_ID}; stage=dispatching; record=${target.recordId}`,
  });
  const readback = await getRecord(context, token, target);
  const status = scalarText(fieldValue(readback.fields, FIELD_STATUS));
  if (status !== "分析中") {
    throw new Error(`分析中状态回读不一致：实际为 ${status || "空"}。`);
  }
}

async function startAgentChat(
  context: RuntimeContext,
  token: string,
  target: Target,
  uploaderId: string,
  caseNumber: string,
): Promise<string> {
  const prompt = buildAnalysisPrompt({
    caseNumber,
    recordId: target.recordId,
    tableId: target.tableId,
    uploaderOpenId: uploaderId,
  });
  const data = await apiJson(
    context,
    token,
    `/aily/v1/agents/${ORGANIZER_AGENT_ID}/chats`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        user_message: { content: [{ type: "text", text: prompt }] },
      }),
    },
    `调用纠纷材料整理专员（${BUILD_ID}）`,
  );
  const chatId = extractChatId(data);
  if (!chatId) throw new Error("智能体接口未返回会话标识，任务未被确认接收。");
  return chatId;
}

basekit.addAction({
  description:
    "锁定当前案件记录后提交一次智能体任务；由智能体完成云文档、full_access 权限和最终 Base 回写。",
  actionText: "提交案件材料分析任务",
  permission: { type: 2 },
  useTenantAccessToken: true,
  formItems: [
    {
      itemId: "recordId",
      label: "目标记录 ID（必填）",
      component: Component.Input,
      required: true,
      paramType: ParamType.String,
      componentProps: { placeholder: "显式映射当前触发记录的 record_id" },
    },
    {
      itemId: "targetTableId",
      label: "目标数据表 ID（必填）",
      component: Component.Input,
      required: true,
      paramType: ParamType.String,
      componentProps: { placeholder: "显式映射当前触发记录所属的数据表 ID" },
    },
  ],
  resultType: {
    type: ParamType.Object,
    properties: {
      accepted: { type: ParamType.Boolean, label: "任务已接收" },
      dispatchState: { type: ParamType.String, label: "投递状态" },
      agentChatId: { type: ParamType.String, label: "智能体会话 ID" },
      recordId: { type: ParamType.String, label: "目标记录 ID" },
      buildId: { type: ParamType.String, label: "组件构建标识" },
      logId: { type: ParamType.String, label: "运行日志 ID" },
    },
  },
  execute: async (formItemParams: UnknownRecord, context: RuntimeContext) => {
    let target: Target | undefined;
    let token = "";
    let stage = "input";
    const currentLogId = logId(context);
    try {
      token = obtainTenantToken(context);
      const baseToken = obtainBaseToken(context);
      const tableId = resolveTableId(formItemParams, context);
      const recordId = resolveRecordId(formItemParams, context);
      target = { baseToken, tableId, recordId, logId: currentLogId };

      stage = "record-preflight";
      const snapshot = await getRecord(context, token, target);
      const caseNumber = scalarText(fieldValue(snapshot.fields, FIELD_CASE_NUMBER));
      if (!caseNumber) throw new Error(`目标记录的“${FIELD_CASE_NUMBER}”为空，无法提交分析任务。`);
      if (attachmentCount(fieldValue(snapshot.fields, FIELD_MATERIALS)) < 1) {
        throw new Error(`目标记录的“${FIELD_MATERIALS}”为空，材料尚未就绪。`);
      }
      const uploaderId = uploaderOpenId(fieldValue(snapshot.fields, FIELD_UPLOADER));
      if (!uploaderId) throw new Error(`目标记录的“${FIELD_UPLOADER}”为空，无法交接文档权限。`);

      const status = scalarText(fieldValue(snapshot.fields, FIELD_STATUS));
      const previousLog = scalarText(fieldValue(snapshot.fields, FIELD_FAILURE));
      const previousChatId = extractExistingChatId(previousLog);
      if (status === "分析中") {
        if (previousChatId) {
          return {
            accepted: true,
            dispatchState: "already_in_progress",
            agentChatId: previousChatId,
            recordId,
            buildId: BUILD_ID,
            logId: currentLogId,
          };
        }
        throw new DoNotOverwriteFailure("目标记录已有未确认的分析中任务，禁止重复创建智能体会话。");
      }
      if (status === "已完成" && scalarText(fieldValue(snapshot.fields, FIELD_RESULT))) {
        return {
          accepted: true,
          dispatchState: "already_complete",
          agentChatId: previousChatId,
          recordId,
          buildId: BUILD_ID,
          logId: currentLogId,
        };
      }

      stage = "mark-dispatching";
      await markDispatching(context, token, target);
      stage = "create-agent-chat";
      const chatId = await startAgentChat(context, token, target, uploaderId, caseNumber);
      stage = "record-accepted";
      await updateRecord(context, token, target, {
        [FIELD_STATUS]: "分析中",
        [FIELD_FAILURE]: `build=${BUILD_ID}; stage=accepted; record=${recordId}; chatId=${chatId}`,
      });
      const finalReadback = await getRecord(context, token, target);
      if (scalarText(fieldValue(finalReadback.fields, FIELD_STATUS)) !== "分析中") {
        throw new Error("智能体已接收，但分析中状态最终回读失败。");
      }
      return {
        accepted: true,
        dispatchState: "accepted",
        agentChatId: chatId,
        recordId,
        buildId: BUILD_ID,
        logId: currentLogId,
      };
    } catch (error) {
      if (error instanceof DoNotOverwriteFailure) {
        throw new Error(`[${BUILD_ID}][${stage}] ${safeReason(error)}`);
      }
      const reason = `[${BUILD_ID}][${stage}] ${safeReason(error)}${currentLogId ? `; log=${currentLogId}` : ""}`;
      if (target && token) {
        try {
          await markFailure(context, token, target, reason);
        } catch (writeError) {
          throw new Error(`${reason}; 失败状态回写也失败：${safeReason(writeError)}`);
        }
      }
      throw new Error(reason);
    }
  },
});

export default basekit;
