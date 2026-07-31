import type { OrdinaryConfigFieldViewModel } from "../configModels";
import type { ConfigDraftInput } from "../configDraft";

interface ConfigValueEditorProps {
  field: OrdinaryConfigFieldViewModel;
  input: ConfigDraftInput;
  error: string | null;
  disabled: boolean;
  descriptionId: string;
  onChange: (input: ConfigDraftInput) => void;
}

export function ConfigValueEditor({
  field,
  input,
  error,
  disabled,
  descriptionId,
  onChange,
}: ConfigValueEditorProps) {
  const inputId = `config-input-${field.name.replaceAll(".", "-")}`;
  const helpId = `${inputId}-help`;
  const errorId = `${inputId}-error`;
  const describedBy =
    error === null
      ? `${descriptionId} ${helpId}`
      : `${descriptionId} ${helpId} ${errorId}`;
  const valueDisabled = disabled || input.isNull;

  return (
    <div className="config-editor">
      {field.nullable ? (
        <label className="null-toggle" htmlFor={`${inputId}-null`}>
          <input
            id={`${inputId}-null`}
            type="checkbox"
            checked={input.isNull}
            disabled={disabled}
            aria-describedby={describedBy}
            onChange={(event) =>
              onChange({ ...input, isNull: event.currentTarget.checked })
            }
          />
          设为 null / unset
        </label>
      ) : null}

      {input.kind === "boolean" ? (
        <label className="boolean-editor" htmlFor={inputId}>
          <input
            id={inputId}
            type="checkbox"
            checked={input.value}
            disabled={valueDisabled}
            aria-describedby={describedBy}
            onChange={(event) =>
              onChange({ ...input, value: event.currentTarget.checked })
            }
          />
          <span>启用</span>
        </label>
      ) : null}

      {input.kind === "integer" || input.kind === "number" ? (
        <label className="config-input-label" htmlFor={inputId}>
          编辑值
          <input
            id={inputId}
            type="number"
            step={input.kind === "integer" ? "1" : "any"}
            value={input.text}
            disabled={valueDisabled}
            aria-invalid={error !== null}
            aria-describedby={describedBy}
            onChange={(event) =>
              onChange({ ...input, text: event.currentTarget.value })
            }
          />
        </label>
      ) : null}

      {input.kind === "string" ? (
        <label className="config-input-label" htmlFor={inputId}>
          编辑值
          <input
            id={inputId}
            type="text"
            value={input.text}
            disabled={valueDisabled}
            aria-invalid={error !== null}
            aria-describedby={describedBy}
            onChange={(event) =>
              onChange({ ...input, text: event.currentTarget.value })
            }
          />
        </label>
      ) : null}

      {input.kind === "string_list" ? (
        <label className="config-input-label" htmlFor={inputId}>
          编辑值（一行一项）
          <textarea
            id={inputId}
            rows={Math.max(3, Math.min(8, input.text.split("\n").length + 1))}
            value={input.text}
            disabled={valueDisabled}
            aria-invalid={error !== null}
            aria-describedby={describedBy}
            onChange={(event) =>
              onChange({ ...input, text: event.currentTarget.value })
            }
          />
        </label>
      ) : null}

      <p id={helpId} className="config-editor-help">
        {disabled
          ? "该字段当前只读，编辑控件已禁用。"
          : "这里只做基础类型检查，保存时仍以后端完整校验为准。"}
      </p>
      {error === null ? null : (
        <p id={errorId} className="config-field-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
