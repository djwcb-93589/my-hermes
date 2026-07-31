import type { ConfigApiValue } from "../../../api/config";
import type {
  ConfigFieldViewModel,
  OrdinaryConfigFieldViewModel,
} from "../configModels";
import type { ConfigDraftInput } from "../configDraft";
import { configValueTypeLabel } from "../configDraft";
import { ConfigValueEditor } from "./ConfigValueEditor";

interface ConfigFieldProps {
  field: ConfigFieldViewModel;
  draftInput?: ConfigDraftInput;
  error?: string;
  onDraftChange: (name: string, input: ConfigDraftInput) => void;
}

export function ConfigField({
  field,
  draftInput,
  error,
  onDraftChange,
}: ConfigFieldProps) {
  if (field.kind === "sensitive") {
    return (
      <article className="config-field config-field-sensitive">
        <header className="config-field-header">
          <div>
            <p className="config-field-name">{field.name}</p>
            <p className="config-field-description">
              敏感字段仅公开是否已配置，不公开值、来源或摘要。
            </p>
          </div>
          <span className="field-state field-state-readonly">Read only</span>
        </header>
        <dl className="config-status-grid">
          <StatusItem
            label="状态"
            value={field.configured ? "Configured" : "Not configured"}
          />
          <StatusItem label="权限" value="Read only" />
        </dl>
      </article>
    );
  }

  const descriptionId = `config-description-${field.name.replaceAll(".", "-")}`;

  return (
    <article
      className={`config-field${field.editable ? "" : " config-field-readonly"}`}
    >
      <header className="config-field-header">
        <div>
          <p className="config-field-name">{field.name}</p>
          <p id={descriptionId} className="config-field-description">
            {field.description ?? "后端未提供该字段的公开说明。"}
          </p>
        </div>
        <span
          className={`field-state ${
            field.editable ? "field-state-writable" : "field-state-readonly"
          }`}
        >
          {field.editable ? "Writable" : "Read only"}
        </span>
      </header>

      {field.shadowedByEnvironment ? (
        <p className="environment-warning" role="status">
          该字段由启动环境提供，修改配置文件不会生效
        </p>
      ) : null}

      <dl className="config-status-grid">
        <StatusItem label="文件值" value={fileValueLabel(field)} />
        <StatusItem label="当前有效值" value={effectiveValueLabel(field)} />
        <StatusItem
          label="来源"
          value={field.source.label}
          detail={field.source.detail}
        />
        <StatusItem label="应用方式" value={field.applyMode.label} />
        <StatusItem
          label="配置状态"
          value={field.configured ? "Configured" : "Not configured"}
        />
        <StatusItem
          label="字段类型"
          value={configValueTypeLabel(field.valueType)}
        />
      </dl>

      {draftInput === undefined ? null : (
        <ConfigValueEditor
          field={field}
          input={draftInput}
          error={error ?? null}
          disabled={!field.editable}
          descriptionId={descriptionId}
          onChange={(input) => onDraftChange(field.name, input)}
        />
      )}
    </article>
  );
}

interface StatusItemProps {
  label: string;
  value: string;
  detail?: string;
}

function StatusItem({ label, value, detail }: StatusItemProps) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <span>{value}</span>
        {detail === undefined ? null : <small>{detail}</small>}
      </dd>
    </div>
  );
}

function fileValueLabel(field: OrdinaryConfigFieldViewModel): string {
  if (field.source.code !== "file" && field.fileValue === null) {
    return "未在配置文件中设置";
  }
  return formatConfigValue(field.fileValue);
}

function effectiveValueLabel(field: OrdinaryConfigFieldViewModel): string {
  if (field.source.code === "environment") {
    return "由环境提供（值不公开）";
  }
  return formatConfigValue(field.effectiveValue);
}

function formatConfigValue(value: ConfigApiValue): string {
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? "[]" : value.join(" · ");
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  return String(value);
}
