import {
  type FormEvent,
  type RefObject,
  useEffect,
  useRef,
} from "react";

import type { BackendActionConfirmationViewModel } from "../backendModels";

interface BackendActionConfirmationProps {
  confirmation: BackendActionConfirmationViewModel;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onCancel: () => void;
  onConfirm: () => void;
}

export function BackendActionConfirmation({
  confirmation,
  triggerRef,
  onCancel,
  onConfirm,
}: BackendActionConfirmationProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
      confirmButtonRef.current?.focus();
    }
    return () => {
      if (dialog?.open === true) {
        dialog.close();
      }
      triggerRef.current?.focus();
    };
  }, [triggerRef]);

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    onConfirm();
  };

  return (
    <dialog
      ref={dialogRef}
      className="dashboard-dialog"
      aria-modal="true"
      aria-labelledby="backend-confirmation-title"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
    >
      <form className="dialog-content" onSubmit={submit}>
        <p className="eyebrow">
          CONFIRM {confirmation.actionLabel.toUpperCase()}
        </p>
        <h2 id="backend-confirmation-title">{confirmation.title}</h2>
        <p>{confirmation.description}</p>
        <dl className="confirmation-summary">
          <div>
            <dt>Action</dt>
            <dd>{confirmation.actionLabel}</dd>
          </div>
          <div>
            <dt>Gateway state</dt>
            <dd>{confirmation.gatewayStateLabel}</dd>
          </div>
          <div>
            <dt>Ownership</dt>
            <dd>{confirmation.ownershipLabel}</dd>
          </div>
        </dl>
        <div className="backend-confirmation-copy">
          {confirmation.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
        <div className="dialog-actions">
          <button type="button" className="text-button" onClick={onCancel}>
            取消
          </button>
          <button ref={confirmButtonRef} type="submit" className="primary-button">
            继续输入 Control Token
          </button>
        </div>
      </form>
    </dialog>
  );
}
