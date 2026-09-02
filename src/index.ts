import {
  basekit,
  Component,
  ParamType,
} from "@lark-opdev/block-basekit-server-api";

const OPEN_API = "https://open.feishu.cn/open-apis";
const AILY_APP_ID = "spring_12dd737859__c";
const WORKFLOW_SKILL_ID = "skill_a4563af760fc";
const WORKFLOW_START_URL = OPEN_API + "/aily/v1/apps/" + AILY_APP_ID + "/skills/" + WORKFLOW_SKILL_ID + "/start";
const BUILD_ID = "7.0.2-workflow-skill";
const TRIGGER_KINDS = new Set(["record_created", "case_document_changed"]);

type UnknownRecord = Record<string, unknown>;

type RuntimeResponse = {
  ok: boolean;
  status?: number;
  statusText?: string;
  json: () => Promise<unknown>;
};

type RuntimeContext = {
  tenantAccessToken?: unknown;
  fetch?: (url: string, options: UnknownRecord) => Promise<RuntimeResponse>;
  app?: { logID?: unknown; logId?: unknown };
  logID?: unknown;
  logId?: unknown;
};

type WorkflowOutput = UnknownRecord & {
  record_id: string;
  dispatch_id: string;
  trigger_kind: string;
};

type DispatchResult = {
  accepted: boolean;
  dispatchState: "success" | "failed" | "unknown";
  stage: string;
  errorCode: string;
  errorMessage: string;
  workflowStatus: string;
  workflowOutput: string;
  dispatchId: string;
  targetRecordId: string;
  triggerKind: string;
  buildId: string;
  logId: string;
};

class DispatchFailure extends Error {
  constructor(
    readonly code: string,
    readonly stage: string,
    message: string,
    readonly unknownOutcome = false,
  ) {
    super(message);
    this.name = "DispatchFailure";
  }
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function scalarText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value).trim() : "";
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
  return scalarText(context.app?.logID || context.app?.logId || context.logID || context.logId);
}

function requiredRecordId(formItemParams: UnknownRecord): string {
  const recordId = scalarText(formItemParams.targetRecordId);
  if (!recordId) {
    throw new DispatchFailure("TARGET_RECORD_MISSING", "validate-input", "缺少目标案件记录 ID");
  }
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(recordId)) {
    throw new DispatchFailure("TARGET_RECORD_INVALID", "validate-input", "目标案件记录 ID 格式不合法");
  }
  return recordId;
}

function requiredTriggerKind(formItemParams: UnknownRecord): string {
  const triggerKind = scalarText(formItemParams.triggerKind);
  if (!TRIGGER_KINDS.has(triggerKind)) {
    throw new DispatchFailure(
      "TRIGGER_KIND_INVALID",
      "validate-input",
      "触发类型必须是 record_created 或 case_document_changed",
    );
  }
  return triggerKind;
}

function tenantToken(context: RuntimeContext): string {
  const token = scalarText(context.tenantAccessToken);
  if (!token) {
    throw new DispatchFailure("CONTEXT_MISSING", "validate-runtime", "运行时缺少租户访问凭证");
  }
  return token;
}

function makeDispatchId(recordId: string): string {
  return "odm:" + recordId + ":" + Date.now() + ":" + Math.random().toString(36).slice(2, 10);
}

function parseWorkflowOutput(
  rawOutput: unknown,
  recordId: string,
  dispatchId: string,
  triggerKind: string,
): WorkflowOutput {
  if (typeof rawOutput !== "string" || !rawOutput.trim()) {
    throw new DispatchFailure("WORKFLOW_OUTPUT_MISSING", "validate-workflow-output", "工作流未返回输出");
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(rawOutput);
  } catch {
    throw new DispatchFailure("WORKFLOW_OUTPUT_INVALID", "validate-workflow-output", "工作流输出不是有效 JSON");
  }

  if (!isRecord(parsed)) {
    throw new DispatchFailure("WORKFLOW_OUTPUT_INVALID", "validate-workflow-output", "工作流输出必须是 JSON 对象");
  }
  if (
    scalarText(parsed.record_id) !== recordId
    || scalarText(parsed.dispatch_id) !== dispatchId
    || scalarText(parsed.trigger_kind) !== triggerKind
  ) {
    throw new DispatchFailure("WORKFLOW_OUTPUT_MISMATCH", "validate-workflow-output", "工作流输出与本次投递参数不一致");
  }
  return parsed as WorkflowOutput;
}

async function startWorkflow(
  context: RuntimeContext,
  token: string,
  input: WorkflowOutput,
): Promise<{ status: string; output: WorkflowOutput }> {
  if (typeof context.fetch !== "function") {
    throw new DispatchFailure("CONTEXT_MISSING", "start-workflow", "运行时未提供受控网络请求能力");
  }

  let response: RuntimeResponse;
  try {
    response = await context.fetch(WORKFLOW_START_URL, {
      method: "POST",
      headers: {
        authorization: "Bearer " + token,
        "content-type": "application/json; charset=utf-8",
      },
      body: JSON.stringify({ input: JSON.stringify(input) }),
    });
  } catch (error) {
    throw new DispatchFailure(
      "WORKFLOW_REQUEST_UNKNOWN",
      "start-workflow",
      safeMessage(error) || "工作流请求结果未知",
      true,
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new DispatchFailure("WORKFLOW_RESPONSE_INVALID", "start-workflow", "工作流接口返回不可解析响应", true);
  }

  if (!isRecord(body)) {
    throw new DispatchFailure("WORKFLOW_RESPONSE_INVALID", "start-workflow", "工作流接口返回结构不正确", true);
  }

  const code = Number(body.code);
  if (!response.ok || !Number.isFinite(code) || code !== 0) {
    const detail = safeMessage(body.msg || response.statusText || "HTTP " + (response.status || 0));
    throw new DispatchFailure("WORKFLOW_REQUEST_REJECTED", "start-workflow", detail || "工作流请求被拒绝");
  }

  const data = isRecord(body.data) ? body.data : undefined;
  const status = scalarText(data?.status);
  const output = parseWorkflowOutput(data?.output, input.record_id, input.dispatch_id, input.trigger_kind);
  if (status !== "success") {
    throw new DispatchFailure("WORKFLOW_EXECUTION_FAILED", "start-workflow", "工作流执行状态为 " + (status || "空"));
  }
  return { status, output };
}

function result(
  context: RuntimeContext,
  values: Partial<DispatchResult> & Pick<DispatchResult, "accepted" | "dispatchState" | "stage">,
): DispatchResult {
  return {
    accepted: values.accepted,
    dispatchState: values.dispatchState,
    stage: values.stage,
    errorCode: values.errorCode || "",
    errorMessage: values.errorMessage || "",
    workflowStatus: values.workflowStatus || "",
    workflowOutput: values.workflowOutput || "",
    dispatchId: values.dispatchId || "",
    targetRecordId: values.targetRecordId || "",
    triggerKind: values.triggerKind || "",
    buildId: BUILD_ID,
    logId: runtimeLogId(context),
  };
}

basekit.addDomainList(["open.feishu.cn"]);

basekit.addAction({
  description: "将当前案件记录坐标投递给纠纷材料处理工作流。",
  actionText: "启动纠纷材料处理工作流",
  permission: { type: 2 },
  useTenantAccessToken: true,
  formItems: [
    {
      itemId: "targetRecordId",
      label: "目标案件记录 ID",
      component: Component.Input,
      required: true,
      paramType: ParamType.String,
      componentProps: { placeholder: "映射当前触发记录的 Record ID" },
    },
    {
      itemId: "triggerKind",
      label: "触发类型",
      component: Component.Input,
      required: true,
      paramType: ParamType.String,
      componentProps: { placeholder: "record_created 或 case_document_changed" },
    },
  ],
  resultType: {
    type: ParamType.Object,
    properties: {
      accepted: { type: ParamType.Boolean, label: "工作流执行成功" },
      dispatchState: { type: ParamType.String, label: "投递状态" },
      stage: { type: ParamType.String, label: "执行阶段" },
      errorCode: { type: ParamType.String, label: "错误代码" },
      errorMessage: { type: ParamType.String, label: "错误信息" },
      workflowStatus: { type: ParamType.String, label: "工作流状态" },
      workflowOutput: { type: ParamType.String, label: "工作流输出" },
      dispatchId: { type: ParamType.String, label: "任务 ID" },
      targetRecordId: { type: ParamType.String, label: "目标案件记录 ID" },
      triggerKind: { type: ParamType.String, label: "触发类型" },
      buildId: { type: ParamType.String, label: "组件构建标识" },
      logId: { type: ParamType.String, label: "运行日志 ID" },
    },
  },
  execute: async (formItemParams: UnknownRecord, context: RuntimeContext) => {
    let targetRecordId = "";
    let triggerKind = "";
    let dispatchId = "";

    try {
      targetRecordId = requiredRecordId(formItemParams);
      triggerKind = requiredTriggerKind(formItemParams);
      dispatchId = makeDispatchId(targetRecordId);
      const workflow = await startWorkflow(context, tenantToken(context), {
        record_id: targetRecordId,
        dispatch_id: dispatchId,
        trigger_kind: triggerKind,
      });
      return result(context, {
        accepted: true,
        dispatchState: "success",
        stage: "start-workflow",
        workflowStatus: workflow.status,
        workflowOutput: JSON.stringify(workflow.output),
        dispatchId,
        targetRecordId,
        triggerKind,
      });
    } catch (error) {
      const failure = error instanceof DispatchFailure
        ? error
        : new DispatchFailure("COMPONENT_INTERNAL_ERROR", "dispatch", safeMessage(error));
      return result(context, {
        accepted: false,
        dispatchState: failure.unknownOutcome ? "unknown" : "failed",
        stage: failure.stage,
        errorCode: failure.code,
        errorMessage: safeMessage(failure),
        dispatchId,
        targetRecordId,
        triggerKind,
      });
    }
  },
});

export default basekit;
