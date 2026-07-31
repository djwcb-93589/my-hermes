import {
  type FormEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";

export type ControlTokenSubmitResult = "success" | "invalid" | "failed";

interface EphemeralControlTokenDialogProps {
  triggerRef: RefObject<HTMLButtonElement | null>;
  inputId: string;
  titleId: string;
  description: string;
  helpText: string;
  submitLabel: string;
  submittingLabel: string;
  invalidMessage?: string;
  onSubmit: (controlToken: string) => Promise<ControlTokenSubmitResult>;
  onClose: () => void;
}

export function EphemeralControlTokenDialog({
  triggerRef,
  inputId,
  titleId,
  description,
  helpText,
  submitLabel,
  submittingLabel,
  invalidMessage =
    "Control Token 无效或权限不足，请重新输入。Read Token 会话未受影响。",
  onSubmit,
  onClose,
}: EphemeralControlTokenDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);
  const submittingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const helpId = `${inputId}-help`;
  const messageId = `${inputId}-message`;

  useEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
      inputRef.current?.focus();
    }
    return () => {
      mountedRef.current = false;
      if (inputRef.current !== null) {
        inputRef.current.value = "";
      }
      if (dialog?.open === true) {
        dialog.close();
      }
      triggerRef.current?.focus();
    };
  }, [triggerRef]);

  const close = (): void => {
    if (submittingRef.current) {
      return;
    }
    if (inputRef.current !== null) {
      inputRef.current.value = "";
    }
    onClose();
  };

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (submittingRef.current) {
      return;
    }
    const submittedToken = inputRef.current?.value ?? "";
    submittingRef.current = true;
    setSubmitting(true);
    setMessage(null);
    let result: ControlTokenSubmitResult = "failed";
    try {
      result = await onSubmit(submittedToken);
    } catch {
      result = "failed";
    } finally {
      if (inputRef.current !== null) {
        inputRef.current.value = "";
      }
      submittingRef.current = false;
    }
    if (!mountedRef.current) {
      return;
    }
    if (result === "invalid") {
      setMessage(invalidMessage);
      setSubmitting(false);
      inputRef.current?.focus();
      return;
    }
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="dashboard-dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (submittingRef.current) {
          return;
        }
        close();
      }}
    >
      <form className="dialog-content" onSubmit={submit}>
        <p className="eyebrow">CONTROL AUTHORIZATION</p>
        <h2 id={titleId}>Dashboard Control Token</h2>
        <p>{description}</p>
        <label className="config-input-label" htmlFor={inputId}>
          Dashboard Control Token
          <input
            id={inputId}
            ref={inputRef}
            type="password"
            minLength={32}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            disabled={submitting}
            required
            aria-describedby={`${helpId} ${messageId}`}
          />
        </label>
        <p id={helpId} className="config-editor-help">
          {helpText}
        </p>
        <p id={messageId} className="dialog-error" role="alert">
          {message}
        </p>
        <div className="dialog-actions">
          <button
            type="button"
            className="text-button"
            disabled={submitting}
            onClick={close}
          >
            取消
          </button>
          <button type="submit" className="primary-button" disabled={submitting}>
            {submitting ? submittingLabel : submitLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
