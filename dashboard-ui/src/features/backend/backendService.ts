import {
  readBackendControlRequest,
  readBackendStatus,
  submitGatewayRestart,
  submitGatewayStart,
  submitGatewayStop,
  type BackendControlAcceptedApiResponse,
  type BackendControlAction,
} from "../../api/backend";
import { HttpError, type HttpClient } from "../../api/http";
import { mapBackendPage, mapControlRequest } from "./backendMapper";
import type {
  BackendPageViewModel,
  ControlRequestViewModel,
} from "./backendModels";

export interface GatewayControlIntent {
  action: BackendControlAction;
  idempotencyKey: string;
}

export type BackendSubmissionFailure =
  | { kind: "invalid_control_token" }
  | { kind: "submission_unknown"; message: string }
  | { kind: "failed"; message: string };

export async function loadBackendPage(
  client: HttpClient,
  signal: AbortSignal,
): Promise<BackendPageViewModel> {
  return mapBackendPage(await readBackendStatus(client, signal));
}

export async function loadBackendControlRequest(
  client: HttpClient,
  requestId: string,
  expectedAction: BackendControlAction,
  signal: AbortSignal,
): Promise<ControlRequestViewModel> {
  const request = mapControlRequest(
    await readBackendControlRequest(client, requestId, signal),
  );
  if (request.action !== expectedAction) {
    throw new HttpError("invalid_response");
  }
  return request;
}

export function submitGatewayControl(
  action: BackendControlAction,
  controlToken: string,
  idempotencyKey: string,
): Promise<BackendControlAcceptedApiResponse> {
  switch (action) {
    case "start":
      return submitGatewayStart(controlToken, idempotencyKey);
    case "stop":
      return submitGatewayStop(controlToken, idempotencyKey);
    case "restart":
      return submitGatewayRestart(controlToken, idempotencyKey);
  }
}

export function createGatewayControlIntent(
  action: BackendControlAction,
): GatewayControlIntent {
  return {
    action,
    idempotencyKey: `dashboard-gateway-${secureUuid()}`,
  };
}

export function classifyBackendSubmissionError(
  error: unknown,
): BackendSubmissionFailure {
  if (!(error instanceof HttpError)) {
    return {
      kind: "submission_unknown",
      message: backendSubmissionUnknownMessage(),
    };
  }
  if (error.status === 401 || error.status === 403) {
    return { kind: "invalid_control_token" };
  }
  if (
    error.code === "network_unavailable" ||
    error.code === "request_timeout" ||
    error.code === "invalid_response" ||
    error.status === null
  ) {
    return {
      kind: "submission_unknown",
      message: backendSubmissionUnknownMessage(),
    };
  }
  switch (error.publicCode) {
    case "backend_unmanaged":
      return failed("Gateway 不受当前 Supervisor 管理，控制请求未提交。");
    case "backend_ownership_uncertain":
      return failed("Gateway 进程身份无法安全确认，控制请求未提交。");
    case "backend_control_conflict":
      return failed("已有其他控制请求正在执行，请等待后再刷新状态。");
    case "idempotency_conflict":
      return failed(
        "Idempotency-Key 与既有操作冲突。已停止重试，请重新读取 Backend 状态。",
      );
    case "supervisor_unavailable":
    case "backend_control_unavailable":
      return failed("Supervisor 或控制基础设施暂时不可用。");
    case "backend_already_running":
      return failed("Gateway 已经在运行，请刷新 Backend 状态。");
    case "backend_already_stopped":
      return failed("Gateway 已经停止，请刷新 Backend 状态。");
    case "backend_start_failed":
      return failed("Gateway 启动请求被后端明确拒绝。");
    case "backend_stop_failed":
      return failed("Gateway 停止请求被后端明确拒绝。");
    case "backend_restart_failed":
      return failed("Gateway 重启请求被后端明确拒绝。");
    case "backend_control_timeout":
      return failed("Gateway 控制操作已由后端判定超时。");
    case "backend_control_invalid_request":
      return failed("控制请求未通过后端校验，未创建新的控制任务。");
  }
  if (error.status === 409) {
    return failed("当前 Gateway 状态与控制请求冲突，请刷新后重试。");
  }
  if (error.status === 503) {
    return failed("Supervisor 或控制基础设施暂时不可用。");
  }
  return failed("控制请求被后端明确拒绝，未能进入跟踪阶段。");
}

export function backendStatusErrorMessage(error: unknown): string {
  if (error instanceof HttpError) {
    if (error.code === "invalid_response") {
      return "Backend Status 返回了无效的安全响应。";
    }
    if (error.code === "request_timeout") {
      return "Backend Status 查询超时，将按受控退避策略重试。";
    }
    if (error.code === "network_unavailable") {
      return "网络暂时不可用，无法读取 Backend Status。";
    }
    if (error.status === 503) {
      return "Backend 状态读取服务暂时不可用。";
    }
  }
  return "暂时无法读取 Backend Status。";
}

export function backendRequestReadErrorMessage(error: unknown): string {
  if (error instanceof HttpError) {
    if (error.publicCode === "backend_request_not_found" || error.status === 404) {
      return "暂时无法找到该控制请求；页面将继续使用 READ Client 查询。";
    }
    if (error.code === "invalid_response") {
      return "控制请求查询返回了无效的安全响应。";
    }
    if (error.code === "request_timeout") {
      return "控制请求查询超时，将在受控退避后重试。";
    }
    if (error.code === "network_unavailable") {
      return "网络暂时不可用，无法查询控制请求状态。";
    }
    if (error.status === 503) {
      return "控制请求读取服务暂时不可用，将稍后重试。";
    }
  }
  return "暂时无法查询控制请求状态，将稍后重试。";
}

function failed(message: string): BackendSubmissionFailure {
  return { kind: "failed", message };
}

export function backendSubmissionUnknownMessage(): string {
  return "控制请求结果暂时无法确认。请求可能已被服务器接受；请使用同一操作和同一 Idempotency-Key 确认请求状态。";
}

function secureUuid(): string {
  const webCrypto = globalThis.crypto;
  if (typeof webCrypto.randomUUID === "function") {
    return webCrypto.randomUUID();
  }
  const bytes = webCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  );
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex
    .slice(6, 8)
    .join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}
