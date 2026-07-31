import type {
  ConfigApiValue,
  ConfigApplyMode,
  ConfigFieldApiResponse,
  ConfigPatchApiResponse,
  ConfigRestartTarget,
  ConfigSnapshotApiResponse,
  ConfigValueSource,
} from "../../api/config";
import type {
  ConfigApplyModeViewModel,
  ConfigFieldViewModel,
  ConfigGroupViewModel,
  ConfigPageViewModel,
  ConfigSaveResultViewModel,
  OrdinaryConfigFieldViewModel,
} from "./configModels";

interface GroupDefinition {
  id: string;
  label: string;
}

const GROUPS: readonly GroupDefinition[] = [
  { id: "browser", label: "Browser" },
  { id: "background-review", label: "Background Review" },
  { id: "plugins", label: "Plugins" },
  { id: "gateway", label: "Gateway" },
  { id: "adapters", label: "Adapters" },
  { id: "model", label: "Model" },
  { id: "sensitive", label: "Sensitive Status" },
  { id: "other", label: "Other" },
];

export function mapConfigSnapshot(
  response: ConfigSnapshotApiResponse,
): ConfigPageViewModel {
  const grouped = new Map<string, ConfigFieldViewModel[]>();
  for (const group of GROUPS) {
    grouped.set(group.id, []);
  }
  const ordinaryFields: OrdinaryConfigFieldViewModel[] = [];
  for (const field of response.fields) {
    const mapped = mapField(field);
    grouped.get(groupForField(mapped))?.push(mapped);
    if (mapped.kind === "ordinary") {
      ordinaryFields.push(mapped);
    }
  }
  const groups: ConfigGroupViewModel[] = GROUPS.flatMap((definition) => {
    const fields = grouped.get(definition.id) ?? [];
    return fields.length === 0
      ? []
      : [{ id: definition.id, label: definition.label, fields }];
  });
  return {
    revision: response.revision,
    groups,
    ordinaryFields,
  };
}

export function mapConfigSaveResult(
  response: ConfigPatchApiResponse,
): ConfigSaveResultViewModel {
  return {
    changedFields: [...response.changed_fields],
    applyModes: response.apply_modes.map(applyModeViewModel),
    restartRequired: response.restart_required,
    restartTargets: response.restart_targets.map((target) => ({
      code: target,
      label: restartTargetLabel(target),
    })),
  };
}

export function applyModeViewModel(
  mode: ConfigApplyMode,
): ConfigApplyModeViewModel {
  switch (mode) {
    case "immediate":
      return { code: mode, label: "立即生效", requiresRestart: false };
    case "gateway_restart":
      return {
        code: mode,
        label: "需要 Gateway 重启",
        requiresRestart: true,
      };
    case "dashboard_restart":
      return {
        code: mode,
        label: "需要 Dashboard 重启",
        requiresRestart: true,
      };
    case "application_restart":
      return {
        code: mode,
        label: "需要应用重启",
        requiresRestart: true,
      };
  }
}

function mapField(field: ConfigFieldApiResponse): ConfigFieldViewModel {
  if (field.sensitive) {
    return {
      kind: "sensitive",
      name: field.name,
      configured: field.configured,
      readOnly: true,
    };
  }
  return {
    kind: "ordinary",
    name: field.name,
    description: field.description,
    valueType: field.value_type,
    fileValue: copyValue(field.file_value),
    effectiveValue: copyValue(field.effective_value),
    editorValue: editorValue(field),
    source: sourceViewModel(field.source),
    applyMode: applyModeViewModel(field.apply_mode),
    configured: field.configured,
    writable: field.writable,
    nullable: field.nullable,
    shadowedByEnvironment: field.shadowed_by_environment,
    editable: field.writable && !field.shadowed_by_environment,
  };
}

function editorValue(
  field: Exclude<ConfigFieldApiResponse, { sensitive: true }>,
): ConfigApiValue {
  if (field.source === "file" || field.file_value !== null) {
    return copyValue(field.file_value);
  }
  return copyValue(field.effective_value);
}

function copyValue(value: ConfigApiValue): ConfigApiValue {
  return Array.isArray(value) ? [...value] : value;
}

function sourceViewModel(source: ConfigValueSource) {
  switch (source) {
    case "file":
      return {
        code: source,
        label: "配置文件",
        detail: "当前值由配置文件提供",
      };
    case "environment":
      return {
        code: source,
        label: "环境提供",
        detail: "当前值由启动环境提供，具体值不公开",
      };
    case "default":
      return {
        code: source,
        label: "使用默认值",
        detail: "配置文件未设置，当前使用后端默认值",
      };
    case "derived":
      return {
        code: source,
        label: "派生值",
        detail: "当前值由公开配置规则派生",
      };
  }
}

function groupForField(field: ConfigFieldViewModel): string {
  if (field.kind === "sensitive") {
    return "sensitive";
  }
  if (field.name.startsWith("browser.")) {
    return "browser";
  }
  if (field.name.startsWith("background_review.")) {
    return "background-review";
  }
  if (field.name.startsWith("plugins.")) {
    return "plugins";
  }
  if (field.name.startsWith("gateway.platforms.")) {
    return "adapters";
  }
  if (field.name.startsWith("gateway.")) {
    return "gateway";
  }
  if (
    field.name === "model" ||
    field.name.startsWith("model.") ||
    field.name.startsWith("fallback.")
  ) {
    return "model";
  }
  return "other";
}

function restartTargetLabel(target: ConfigRestartTarget): string {
  switch (target) {
    case "gateway":
      return "Gateway";
    case "dashboard":
      return "Dashboard";
    case "application":
      return "应用";
  }
}

