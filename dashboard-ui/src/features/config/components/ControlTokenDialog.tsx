import {
  type FormEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";

export type ControlTokenSubmitResult = "success" | "invalid" | "failed";

interface ControlTokenDialogProps {
  triggerRef: RefObject<HTMLButtonElement | null>;
  onSubmit: (controlToken: string) => Promise<ControlTokenSubmitResult>;
  onClose: () => void;
}

export function ControlTokenDialog({
  triggerRef,
  onSubmit,
  onClose,
}: ControlTokenDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);
  const submittingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

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
      setMessage("Control Token 无效，请重新输入。Read Token 会话未受影响。");
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
      aria-labelledby="control-token-title"
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
        <h2 id="control-token-title">Dashboard Control Token</h2>
        <p>
          Token 只用于本次配置 PATCH；不会进入全局认证状态或浏览器持久存储。
        </p>
        <label className="config-input-label" htmlFor="dashboard-control-token">
          Dashboard Control Token
          <input
            id="dashboard-control-token"
            ref={inputRef}
            type="password"
            minLength={32}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            disabled={submitting}
            required
            aria-describedby="control-token-help control-token-message"
          />
        </label>
        <p id="control-token-help" className="config-editor-help">
          每次提交都必须重新输入；关闭窗口或请求结束后会立即清除输入引用。
        </p>
        <p id="control-token-message" className="dialog-error" role="alert">
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
            {submitting ? "正在安全保存…" : "授权并保存"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
