import { useEffect, useRef } from "react";

import type { ConfigChangeViewModel } from "../configModels";
import { configValueTypeLabel } from "../configDraft";

interface ConfigSaveConfirmationProps {
  changes: readonly ConfigChangeViewModel[];
  onCancel: () => void;
  onContinue: () => void;
}

export function ConfigSaveConfirmation({
  changes,
  onCancel,
  onContinue,
}: ConfigSaveConfirmationProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const applyModes = [
    ...new Set(changes.map((change) => change.applyMode.label)),
  ];
  const restartPossible = changes.some(
    (change) => change.applyMode.requiresRestart,
  );

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
    return () => {
      if (dialog?.open === true) {
        dialog.close();
      }
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="dashboard-dialog"
      aria-labelledby="config-confirm-title"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
    >
      <div className="dialog-content">
        <p className="eyebrow">REVIEW CHANGES</p>
        <h2 id="config-confirm-title">确认保存配置</h2>
        <p>
          将提交 {changes.length} 个普通配置字段。确认页不显示字段值，完整校验仍由后端完成。
        </p>
        <ul className="confirmation-list">
          {changes.map((change) => (
            <li key={change.name}>
              <strong>{change.name}</strong>
              <span>{configValueTypeLabel(change.valueType)}</span>
            </li>
          ))}
        </ul>
        <dl className="confirmation-summary">
          <div>
            <dt>可能的应用方式</dt>
            <dd>{applyModes.join("、")}</dd>
          </div>
          <div>
            <dt>可能需要重启</dt>
            <dd>{restartPossible ? "是" : "否"}</dd>
          </div>
        </dl>
        <div className="dialog-actions">
          <button type="button" className="text-button" onClick={onCancel}>
            返回编辑
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={onContinue}
          >
            继续并输入 Control Token
          </button>
        </div>
      </div>
    </dialog>
  );
}

