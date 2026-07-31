import type { RefObject } from "react";

import {
  EphemeralControlTokenDialog,
  type ControlTokenSubmitResult,
} from "../../../components/EphemeralControlTokenDialog";

interface BackendControlTokenDialogProps {
  actionLabel: string;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onSubmit: (controlToken: string) => Promise<ControlTokenSubmitResult>;
  onClose: () => void;
}

export function BackendControlTokenDialog({
  actionLabel,
  triggerRef,
  onSubmit,
  onClose,
}: BackendControlTokenDialogProps) {
  return (
    <EphemeralControlTokenDialog
      triggerRef={triggerRef}
      inputId="backend-control-token"
      titleId="backend-control-token-title"
      description={`Token 只用于本次 ${actionLabel} Gateway POST；不会与 READ Token 一起发送，也不会进入浏览器持久存储。`}
      helpText="请求提交期间窗口不能关闭。请求结束后会立即清除 Token 输入引用。"
      submitLabel={`授权并${actionLabel}`}
      submittingLabel="正在安全提交…"
      onSubmit={onSubmit}
      onClose={onClose}
    />
  );
}
