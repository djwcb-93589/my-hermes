import type { ConfigApiValue, ConfigValueType } from "../../api/config";
import type {
  ConfigChangeViewModel,
  ConfigPageViewModel,
  OrdinaryConfigFieldViewModel,
} from "./configModels";

export type ConfigDraftInput =
  | {
      kind: "boolean";
      isNull: boolean;
      value: boolean;
      touched: boolean;
    }
  | {
      kind: "integer" | "number" | "string" | "string_list";
      isNull: boolean;
      text: string;
      touched: boolean;
    };

export type ConfigDraft = Map<string, ConfigDraftInput>;

export interface ConfigDraftAnalysis {
  changes: ConfigChangeViewModel[];
  errors: Map<string, string>;
  hasUnsavedChanges: boolean;
}

type ParsedDraftValue =
  | { ok: true; value: ConfigApiValue }
  | { ok: false; message: string };

export function createConfigDraft(snapshot: ConfigPageViewModel): ConfigDraft {
  return new Map(
    snapshot.ordinaryFields.map((field) => [
      field.name,
      inputFromValue(field.valueType, field.editorValue),
    ]),
  );
}

export function updateDraftInput(
  draft: ConfigDraft,
  name: string,
  update: (current: ConfigDraftInput) => ConfigDraftInput,
): ConfigDraft {
  const current = draft.get(name);
  if (current === undefined) {
    return draft;
  }
  const next = new Map(draft);
  next.set(name, { ...update(current), touched: true });
  return next;
}

export function markDraftSynchronized(draft: ConfigDraft): ConfigDraft {
  return new Map(
    [...draft].map(([name, input]) => [
      name,
      { ...input, touched: false },
    ]),
  );
}

export function analyzeConfigDraft(
  snapshot: ConfigPageViewModel,
  draft: ConfigDraft,
): ConfigDraftAnalysis {
  const changes: ConfigChangeViewModel[] = [];
  const errors = new Map<string, string>();
  for (const field of snapshot.ordinaryFields) {
    if (!field.editable) {
      continue;
    }
    const input = draft.get(field.name);
    if (input === undefined || !input.touched) {
      continue;
    }
    const parsed = parseInput(field, input);
    if (!parsed.ok) {
      errors.set(field.name, parsed.message);
      continue;
    }
    if (!configValuesEqual(parsed.value, field.editorValue)) {
      changes.push({
        name: field.name,
        valueType: field.valueType,
        value: parsed.value,
        applyMode: field.applyMode,
      });
    }
  }
  return {
    changes,
    errors,
    hasUnsavedChanges: changes.length > 0 || errors.size > 0,
  };
}

export function configValueTypeLabel(valueType: ConfigValueType): string {
  switch (valueType) {
    case "boolean":
      return "Boolean";
    case "integer":
      return "Integer";
    case "number":
      return "Number";
    case "string":
      return "String";
    case "string_list":
      return "String list";
  }
}

function inputFromValue(
  valueType: ConfigValueType,
  value: ConfigApiValue,
): ConfigDraftInput {
  const isNull = value === null;
  if (valueType === "boolean") {
    return {
      kind: valueType,
      isNull,
      value: typeof value === "boolean" ? value : false,
      touched: false,
    };
  }
  if (valueType === "string_list") {
    return {
      kind: valueType,
      isNull,
      text: Array.isArray(value) ? value.join("\n") : "",
      touched: false,
    };
  }
  return {
    kind: valueType,
    isNull,
    text: value === null ? "" : String(value),
    touched: false,
  };
}

function parseInput(
  field: OrdinaryConfigFieldViewModel,
  input: ConfigDraftInput,
): ParsedDraftValue {
  if (input.isNull) {
    return field.nullable
      ? { ok: true, value: null }
      : { ok: false, message: "该字段不允许设为 null。" };
  }
  switch (input.kind) {
    case "boolean":
      return { ok: true, value: input.value };
    case "integer": {
      if (input.text.trim() === "") {
        return { ok: false, message: "请输入整数。" };
      }
      const value = Number(input.text);
      return Number.isFinite(value) && Number.isInteger(value)
        ? { ok: true, value }
        : { ok: false, message: "只能输入有限整数。" };
    }
    case "number": {
      if (input.text.trim() === "") {
        return { ok: false, message: "请输入数字。" };
      }
      const value = Number(input.text);
      return Number.isFinite(value)
        ? { ok: true, value }
        : { ok: false, message: "只能输入有限数字。" };
    }
    case "string":
      return { ok: true, value: input.text };
    case "string_list": {
      if (input.text === "") {
        return { ok: true, value: [] };
      }
      const items = input.text.split("\n");
      return items.some((item) => item.trim() === "")
        ? {
            ok: false,
            message: "列表中不能包含空白项；请每行输入一个值。",
          }
        : { ok: true, value: items };
    }
  }
}

function configValuesEqual(
  left: ConfigApiValue,
  right: ConfigApiValue,
): boolean {
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((item, index) => item === right[index])
    );
  }
  return left === right;
}
