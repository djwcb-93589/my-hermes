import type { ConfigGroupViewModel } from "../configModels";
import type { ConfigDraft, ConfigDraftInput } from "../configDraft";
import { ConfigField } from "./ConfigField";

interface ConfigGroupProps {
  group: ConfigGroupViewModel;
  draft: ConfigDraft;
  errors: Map<string, string>;
  onDraftChange: (name: string, input: ConfigDraftInput) => void;
}

export function ConfigGroup({
  group,
  draft,
  errors,
  onDraftChange,
}: ConfigGroupProps) {
  const titleId = `config-group-${group.id}`;
  return (
    <section className="config-group" aria-labelledby={titleId}>
      <header className="config-group-heading">
        <div>
          <p className="eyebrow">CONFIGURATION</p>
          <h2 id={titleId}>{group.label}</h2>
        </div>
        <span>{group.fields.length} fields</span>
      </header>
      <div className="config-field-list">
        {group.fields.map((field) => (
          <ConfigField
            key={field.name}
            field={field}
            draftInput={draft.get(field.name)}
            error={errors.get(field.name)}
            onDraftChange={onDraftChange}
          />
        ))}
      </div>
    </section>
  );
}

