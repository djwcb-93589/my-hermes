import type {
  ConfigApiValue,
  ConfigApplyMode,
  ConfigRestartTarget,
  ConfigValueSource,
  ConfigValueType,
} from "../../api/config";

export interface ConfigSourceViewModel {
  code: ConfigValueSource;
  label: string;
  detail: string;
}

export interface ConfigApplyModeViewModel {
  code: ConfigApplyMode;
  label: string;
  requiresRestart: boolean;
}

export interface OrdinaryConfigFieldViewModel {
  kind: "ordinary";
  name: string;
  description: string | null;
  valueType: ConfigValueType;
  fileValue: ConfigApiValue;
  effectiveValue: ConfigApiValue;
  editorValue: ConfigApiValue;
  source: ConfigSourceViewModel;
  applyMode: ConfigApplyModeViewModel;
  configured: boolean;
  writable: boolean;
  nullable: boolean;
  shadowedByEnvironment: boolean;
  editable: boolean;
}

export interface SensitiveConfigFieldViewModel {
  kind: "sensitive";
  name: string;
  configured: boolean;
  readOnly: true;
}

export type ConfigFieldViewModel =
  | OrdinaryConfigFieldViewModel
  | SensitiveConfigFieldViewModel;

export interface ConfigGroupViewModel {
  id: string;
  label: string;
  fields: ConfigFieldViewModel[];
}

export interface ConfigPageViewModel {
  revision: string;
  groups: ConfigGroupViewModel[];
  ordinaryFields: OrdinaryConfigFieldViewModel[];
}

export interface ConfigChangeViewModel {
  name: string;
  valueType: ConfigValueType;
  value: ConfigApiValue;
  applyMode: ConfigApplyModeViewModel;
}

export interface ConfigSaveResultViewModel {
  changedFields: string[];
  applyModes: ConfigApplyModeViewModel[];
  restartRequired: boolean;
  restartTargets: Array<{
    code: ConfigRestartTarget;
    label: string;
  }>;
}

export type ConfigConflictKind =
  | "revision"
  | "shadowed"
  | "unknown";

