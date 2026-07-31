import {
  EphemeralControlTransport,
  HttpError,
  type HttpClient,
} from "./http";

export type ConfigApiValue =
  | boolean
  | number
  | string
  | string[]
  | null;
export type ConfigValueType =
  | "boolean"
  | "integer"
  | "number"
  | "string"
  | "string_list";
export type ConfigValueSource =
  | "file"
  | "environment"
  | "default"
  | "derived";
export type ConfigApplyMode =
  | "immediate"
  | "gateway_restart"
  | "dashboard_restart"
  | "application_restart";
export type ConfigRestartTarget = "gateway" | "dashboard" | "application";

export interface ConfigValueFieldApiResponse {
  name: string;
  file_value: ConfigApiValue;
  effective_value: ConfigApiValue;
  source: ConfigValueSource;
  value_type: ConfigValueType;
  writable: boolean;
  sensitive: false;
  apply_mode: ConfigApplyMode;
  nullable: boolean;
  configured: boolean;
  shadowed_by_environment: boolean;
  description: string | null;
}

export interface SensitiveConfigFieldApiResponse {
  name: string;
  configured: boolean;
  writable: false;
  sensitive: true;
}

export type ConfigFieldApiResponse =
  | ConfigValueFieldApiResponse
  | SensitiveConfigFieldApiResponse;

export interface ConfigSnapshotApiResponse {
  revision: string;
  fields: ConfigFieldApiResponse[];
}

export interface ConfigPatchChangeApiRequest {
  name: string;
  value: ConfigApiValue;
}

export interface ConfigPatchApiRequest {
  expected_revision: string;
  changes: ConfigPatchChangeApiRequest[];
}

export interface ConfigPatchApiResponse {
  previous_revision: string;
  new_revision: string;
  changed_fields: string[];
  apply_modes: ConfigApplyMode[];
  restart_required: boolean;
  restart_targets: ConfigRestartTarget[];
}

const CONFIG_FIELD_NAME = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$/;
const CONFIG_REVISION = /^sha256:[0-9a-f]{64}$/;
const MAX_CONFIG_FIELDS = 128;
const MAX_CONFIG_VALUE_TEXT_LENGTH = 20_000;
const MAX_CONFIG_LIST_ITEMS = 1_024;
const CONFIG_VALUE_TYPES: readonly ConfigValueType[] = [
  "boolean",
  "integer",
  "number",
  "string",
  "string_list",
];
const CONFIG_VALUE_SOURCES: readonly ConfigValueSource[] = [
  "file",
  "environment",
  "default",
  "derived",
];
const CONFIG_APPLY_MODES: readonly ConfigApplyMode[] = [
  "immediate",
  "gateway_restart",
  "dashboard_restart",
  "application_restart",
];
const CONFIG_RESTART_TARGETS: readonly ConfigRestartTarget[] = [
  "gateway",
  "dashboard",
  "application",
];
const CONFIG_PUBLIC_ERROR_CODES = [
  "config_invalid",
  "config_field_unknown",
  "config_field_read_only",
  "config_value_invalid",
  "config_not_found",
  "config_conflict",
  "config_shadowed",
  "config_unavailable",
  "config_write_failed",
] as const;

export async function readConfig(
  client: HttpClient,
  signal?: AbortSignal,
): Promise<ConfigSnapshotApiResponse> {
  const payload = await client.get<unknown>("/api/config", { signal });
  return parseConfigSnapshot(payload);
}

export async function patchConfig(
  controlToken: string,
  request: ConfigPatchApiRequest,
  signal?: AbortSignal,
): Promise<ConfigPatchApiResponse> {
  const client = new EphemeralControlTransport(controlToken);
  try {
    const payload = await client.patchConfig<unknown>(request, {
      signal,
      allowedPublicErrorCodes: CONFIG_PUBLIC_ERROR_CODES,
    });
    return parseConfigPatchResponse(payload);
  } finally {
    client.dispose();
  }
}

function parseConfigSnapshot(value: unknown): ConfigSnapshotApiResponse {
  const payload = objectValue(value);
  const revision = revisionValue(property(payload, "revision"));
  const rawFields = property(payload, "fields");
  if (!Array.isArray(rawFields) || rawFields.length > MAX_CONFIG_FIELDS) {
    invalidResponse();
  }
  const names = new Set<string>();
  const fields = rawFields.map((field) => {
    const parsed = parseConfigField(field);
    if (names.has(parsed.name)) {
      invalidResponse();
    }
    names.add(parsed.name);
    return parsed;
  });
  return { revision, fields };
}

function parseConfigField(value: unknown): ConfigFieldApiResponse {
  const field = objectValue(value);
  const sensitive = property(field, "sensitive");
  if (sensitive === true) {
    const writable = property(field, "writable");
    if (writable !== false) {
      invalidResponse();
    }
    return {
      name: fieldName(property(field, "name")),
      configured: booleanValue(property(field, "configured")),
      writable,
      sensitive,
    };
  }
  if (sensitive !== false) {
    invalidResponse();
  }
  const valueType = enumValue(
    property(field, "value_type"),
    CONFIG_VALUE_TYPES,
  );
  const description = nullableString(property(field, "description"));
  if (description !== null && description.length > 256) {
    invalidResponse();
  }
  const source = enumValue(
    property(field, "source"),
    CONFIG_VALUE_SOURCES,
  );
  const fileValue = configValue(property(field, "file_value"), valueType);
  const effectiveValue = configValue(
    property(field, "effective_value"),
    valueType,
  );
  const shadowedByEnvironment = booleanValue(
    property(field, "shadowed_by_environment"),
  );
  if (
    shadowedByEnvironment !== (source === "environment") ||
    (source === "environment" && effectiveValue !== null)
  ) {
    invalidResponse();
  }
  return {
    name: fieldName(property(field, "name")),
    file_value: fileValue,
    effective_value: effectiveValue,
    source,
    value_type: valueType,
    writable: booleanValue(property(field, "writable")),
    sensitive,
    apply_mode: enumValue(
      property(field, "apply_mode"),
      CONFIG_APPLY_MODES,
    ),
    nullable: booleanValue(property(field, "nullable")),
    configured: booleanValue(property(field, "configured")),
    shadowed_by_environment: shadowedByEnvironment,
    description,
  };
}

function parseConfigPatchResponse(value: unknown): ConfigPatchApiResponse {
  const payload = objectValue(value);
  const changedFields = uniqueStringArray(
    property(payload, "changed_fields"),
    fieldName,
  );
  const applyModes = uniqueStringArray(
    property(payload, "apply_modes"),
    (item) => enumValue(item, CONFIG_APPLY_MODES),
  );
  const restartTargets = uniqueStringArray(
    property(payload, "restart_targets"),
    (item) => enumValue(item, CONFIG_RESTART_TARGETS),
  );
  return {
    previous_revision: revisionValue(
      property(payload, "previous_revision"),
    ),
    new_revision: revisionValue(property(payload, "new_revision")),
    changed_fields: changedFields,
    apply_modes: applyModes,
    restart_required: booleanValue(
      property(payload, "restart_required"),
    ),
    restart_targets: restartTargets,
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

function nullableString(value: unknown): string | null {
  if (value === null || typeof value === "string") {
    return value;
  }
  invalidResponse();
}

function fieldName(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length > 128 ||
    !CONFIG_FIELD_NAME.test(value)
  ) {
    invalidResponse();
  }
  return value;
}

function revisionValue(value: unknown): string {
  if (typeof value !== "string" || !CONFIG_REVISION.test(value)) {
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

function configValue(
  value: unknown,
  valueType: ConfigValueType,
): ConfigApiValue {
  if (value === null) {
    return null;
  }
  switch (valueType) {
    case "boolean":
      if (typeof value === "boolean") {
        return value;
      }
      break;
    case "integer":
      if (
        typeof value === "number" &&
        Number.isFinite(value) &&
        Number.isInteger(value)
      ) {
        return value;
      }
      break;
    case "number":
      if (typeof value === "number" && Number.isFinite(value)) {
        return value;
      }
      break;
    case "string":
      if (
        typeof value === "string" &&
        value.length <= MAX_CONFIG_VALUE_TEXT_LENGTH
      ) {
        return value;
      }
      break;
    case "string_list":
      if (
        Array.isArray(value) &&
        value.length <= MAX_CONFIG_LIST_ITEMS &&
        value.every(
          (item) =>
            typeof item === "string" &&
            item.length <= MAX_CONFIG_VALUE_TEXT_LENGTH,
        )
      ) {
        return [...value];
      }
      break;
  }
  invalidResponse();
}

function uniqueStringArray<T extends string>(
  value: unknown,
  parse: (item: unknown) => T,
): T[] {
  if (!Array.isArray(value) || value.length > MAX_CONFIG_FIELDS) {
    invalidResponse();
  }
  const parsed = value.map(parse);
  if (new Set(parsed).size !== parsed.length) {
    invalidResponse();
  }
  return parsed;
}

function invalidResponse(): never {
  throw new HttpError("invalid_response");
}
