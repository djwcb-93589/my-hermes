import type {
  BackendControlAcceptedApiResponse,
  BackendControlAction,
  BackendControlRequestApiResponse,
  BackendControlRequestStatus,
  BackendObservedState,
  BackendOwnership,
  BackendResultCode,
  BackendStatusApiResponse,
  SupervisorInstanceState,
} from "./dto";
import {
  EphemeralControlTransport,
  HttpError,
  type HttpClient,
} from "./http";

export type {
  BackendControlAcceptedApiResponse,
  BackendControlAction,
  BackendControlRequestApiResponse,
  BackendControlRequestStatus,
  BackendObservedState,
  BackendOwnership,
  BackendResultCode,
  BackendStatusApiResponse,
  SupervisorInstanceState,
};

const BACKEND_REQUEST_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SAFE_REFERENCE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SAFE_EXCEPTION_TYPE = /^[A-Za-z][A-Za-z0-9_.]{0,127}$/;
const ISO_TIMESTAMP =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
const BACKEND_ACTIONS: readonly BackendControlAction[] = [
  "start",
  "stop",
  "restart",
];
const REQUEST_STATUSES: readonly BackendControlRequestStatus[] = [
  "pending",
  "claimed",
  "executing",
  "succeeded",
  "failed",
  "rejected",
];
const OBSERVED_STATES: readonly BackendObservedState[] = [
  "stopped",
  "starting",
  "running",
  "stopping",
  "exited",
  "unknown",
];
const OWNERSHIP_STATES: readonly BackendOwnership[] = [
  "managed",
  "unmanaged",
  "none",
  "uncertain",
];
const SUPERVISOR_STATES: readonly SupervisorInstanceState[] = [
  "online",
  "offline",
  "unknown",
];
const RESULT_CODES: readonly BackendResultCode[] = [
  "started",
  "stopped",
  "restarted",
  "already_running",
  "already_stopped",
  "unmanaged_instance",
  "ownership_uncertain",
  "control_conflict",
  "start_failed",
  "stop_failed",
  "restart_failed",
  "gateway_exited_before_ready",
  "control_timeout",
  "supervisor_lease_lost",
];
const BACKEND_PUBLIC_ERROR_CODES = [
  "backend_control_invalid_request",
  "backend_request_not_found",
  "backend_control_conflict",
  "idempotency_conflict",
  "backend_unmanaged",
  "backend_ownership_uncertain",
  "supervisor_unavailable",
  "backend_control_unavailable",
  "backend_already_running",
  "backend_already_stopped",
  "backend_start_failed",
  "backend_stop_failed",
  "backend_restart_failed",
  "backend_control_timeout",
] as const;

export async function readBackendStatus(
  client: HttpClient,
  signal?: AbortSignal,
): Promise<BackendStatusApiResponse> {
  const payload = await client.get<unknown>("/api/backend/status", {
    signal,
    allowedPublicErrorCodes: BACKEND_PUBLIC_ERROR_CODES,
  });
  return parseBackendStatus(payload);
}

export async function readBackendControlRequest(
  client: HttpClient,
  requestId: string,
  signal?: AbortSignal,
): Promise<BackendControlRequestApiResponse> {
  const normalizedRequestId = requestIdValue(requestId);
  const payload = await client.get<unknown>(
    `/api/backend/requests/${normalizedRequestId}`,
    {
      signal,
      allowedPublicErrorCodes: BACKEND_PUBLIC_ERROR_CODES,
    },
  );
  const request = parseBackendControlRequest(payload);
  if (request.request_id !== normalizedRequestId) {
    invalidResponse();
  }
  return request;
}

export function submitGatewayStart(
  controlToken: string,
  idempotencyKey: string,
): Promise<BackendControlAcceptedApiResponse> {
  return submitGatewayAction(controlToken, "start", idempotencyKey);
}

export function submitGatewayStop(
  controlToken: string,
  idempotencyKey: string,
): Promise<BackendControlAcceptedApiResponse> {
  return submitGatewayAction(controlToken, "stop", idempotencyKey);
}

export function submitGatewayRestart(
  controlToken: string,
  idempotencyKey: string,
): Promise<BackendControlAcceptedApiResponse> {
  return submitGatewayAction(controlToken, "restart", idempotencyKey);
}

async function submitGatewayAction(
  controlToken: string,
  action: BackendControlAction,
  idempotencyKey: string,
): Promise<BackendControlAcceptedApiResponse> {
  const client = new EphemeralControlTransport(controlToken);
  try {
    const payload = await client.postGatewayAction<unknown>(
      action,
      idempotencyKey,
      {
        allowedPublicErrorCodes: BACKEND_PUBLIC_ERROR_CODES,
      },
    );
    const accepted = parseBackendControlAccepted(payload);
    if (accepted.action !== action) {
      invalidResponse();
    }
    return accepted;
  } finally {
    client.dispose();
  }
}

function parseBackendStatus(value: unknown): BackendStatusApiResponse {
  const payload = objectValue(value);
  const supervisor = objectValue(property(payload, "supervisor"));
  const gateway = objectValue(property(payload, "gateway"));
  const latestRequestValue = property(payload, "latest_request");
  const ownership = enumValue(
    property(gateway, "ownership"),
    OWNERSHIP_STATES,
  );
  const managed = booleanValue(property(gateway, "managed"));
  if (managed !== (ownership === "managed")) {
    invalidResponse();
  }
  return {
    observed_at: timestampValue(property(payload, "observed_at")),
    supervisor: {
      online: booleanValue(property(supervisor, "online")),
      lease_expires_at: nullableTimestamp(
        property(supervisor, "lease_expires_at"),
      ),
      instance_state: enumValue(
        property(supervisor, "instance_state"),
        SUPERVISOR_STATES,
      ),
    },
    gateway: {
      observed_state: enumValue(
        property(gateway, "observed_state"),
        OBSERVED_STATES,
      ),
      ownership,
      lease_active: booleanValue(property(gateway, "lease_active")),
      managed,
      started_at: nullableTimestamp(property(gateway, "started_at")),
      last_exit_at: nullableTimestamp(property(gateway, "last_exit_at")),
      last_exit_code: nullableExitCode(
        property(gateway, "last_exit_code"),
      ),
      config_changed_since_start: nullableBoolean(
        property(gateway, "config_changed_since_start"),
      ),
      restart_recommended: nullableBoolean(
        property(gateway, "restart_recommended"),
      ),
    },
    latest_request:
      latestRequestValue === null
        ? null
        : parseBackendControlRequest(latestRequestValue),
  };
}

function parseBackendControlAccepted(
  value: unknown,
): BackendControlAcceptedApiResponse {
  const payload = objectValue(value);
  return {
    request_id: requestIdValue(property(payload, "request_id")),
    action: enumValue(property(payload, "action"), BACKEND_ACTIONS),
    status: enumValue(property(payload, "status"), REQUEST_STATUSES),
  };
}

function parseBackendControlRequest(
  value: unknown,
): BackendControlRequestApiResponse {
  const payload = objectValue(value);
  const status = enumValue(
    property(payload, "status"),
    REQUEST_STATUSES,
  );
  const createdAt = timestampValue(property(payload, "created_at"));
  const startedAt = nullableTimestamp(property(payload, "started_at"));
  const completedAt = nullableTimestamp(property(payload, "completed_at"));
  const rawResultCode = property(payload, "result_code");
  const resultCode =
    rawResultCode === null ? null : enumValue(rawResultCode, RESULT_CODES);
  const terminal = isTerminalRequestStatus(status);
  if (terminal !== (completedAt !== null) || terminal !== (resultCode !== null)) {
    invalidResponse();
  }
  if (
    (startedAt !== null && Date.parse(startedAt) < Date.parse(createdAt)) ||
    (completedAt !== null &&
      Date.parse(completedAt) < Date.parse(startedAt ?? createdAt))
  ) {
    invalidResponse();
  }
  return {
    request_id: requestIdValue(property(payload, "request_id")),
    backend_type: backendTypeValue(property(payload, "backend_type")),
    action: enumValue(property(payload, "action"), BACKEND_ACTIONS),
    status,
    created_at: createdAt,
    started_at: startedAt,
    completed_at: completedAt,
    result_code: resultCode,
    result_reference: nullableSafeReference(
      property(payload, "result_reference"),
    ),
    exception_type: nullableExceptionType(
      property(payload, "exception_type"),
    ),
    forced_termination: booleanValue(
      property(payload, "forced_termination"),
    ),
  };
}

function objectValue(value: unknown): object {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    invalidResponse();
  }
  return value;
}

function property(value: object, key: string): unknown {
  return Reflect.get(value, key);
}

function booleanValue(value: unknown): boolean {
  if (typeof value !== "boolean") {
    invalidResponse();
  }
  return value;
}

function nullableBoolean(value: unknown): boolean | null {
  if (value === null || typeof value === "boolean") {
    return value;
  }
  invalidResponse();
}

function nullableExitCode(value: unknown): number | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    invalidResponse();
  }
  return value;
}

function timestampValue(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length > 64 ||
    !ISO_TIMESTAMP.test(value) ||
    !hasValidTimestampParts(value) ||
    !Number.isFinite(Date.parse(value))
  ) {
    invalidResponse();
  }
  return value;
}

function hasValidTimestampParts(value: string): boolean {
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const hour = Number(value.slice(11, 13));
  const minute = Number(value.slice(14, 16));
  const second = Number(value.slice(17, 19));
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysInMonth(year, month) ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return false;
  }
  if (value.endsWith("Z")) {
    return true;
  }
  const offsetIndex = Math.max(value.lastIndexOf("+"), value.lastIndexOf("-"));
  const offsetHour = Number(value.slice(offsetIndex + 1, offsetIndex + 3));
  const offsetMinute = Number(value.slice(offsetIndex + 4, offsetIndex + 6));
  return (
    offsetIndex >= 19 &&
    offsetHour <= 14 &&
    offsetMinute <= 59 &&
    (offsetHour < 14 || offsetMinute === 0)
  );
}

function daysInMonth(year: number, month: number): number {
  switch (month) {
    case 2:
      return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
        ? 29
        : 28;
    case 4:
    case 6:
    case 9:
    case 11:
      return 30;
    default:
      return 31;
  }
}

function nullableTimestamp(value: unknown): string | null {
  return value === null ? null : timestampValue(value);
}

function requestIdValue(value: unknown): string {
  if (typeof value !== "string" || !BACKEND_REQUEST_ID.test(value)) {
    invalidResponse();
  }
  return value;
}

function backendTypeValue(value: unknown): "gateway" {
  if (value !== "gateway") {
    invalidResponse();
  }
  return value;
}

function nullableSafeReference(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !SAFE_REFERENCE.test(value)) {
    invalidResponse();
  }
  return value;
}

function nullableExceptionType(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !SAFE_EXCEPTION_TYPE.test(value)) {
    invalidResponse();
  }
  return value;
}

function enumValue<T extends string>(
  value: unknown,
  allowedValues: readonly T[],
): T {
  if (typeof value !== "string" || !allowedValues.includes(value as T)) {
    invalidResponse();
  }
  return value as T;
}

function isTerminalRequestStatus(
  status: BackendControlRequestStatus,
): boolean {
  return status === "succeeded" || status === "failed" || status === "rejected";
}

function invalidResponse(): never {
  throw new HttpError("invalid_response");
}
