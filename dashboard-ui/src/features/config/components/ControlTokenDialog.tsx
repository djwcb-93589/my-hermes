import type { RefObject } from "react";

import {
  EphemeralControlTokenDialog,
  type ControlTokenSubmitResult,
} from "../../../components/EphemeralControlTokenDialog";

export type { ControlTokenSubmitResult };

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
  return (
    <EphemeralControlTokenDialog
      triggerRef={triggerRef}
      inputId="dashboard-control-token"
      titleId="control-token-title"
      description="Token 只用于本次配置 PATCH；不会进入全局认证状态或浏览器持久存储。"
      helpText="每次提交都必须重新输入；关闭窗口或请求结束后会立即清除输入引用。"
      submitLabel="授权并保存"
      submittingLabel="正在安全保存…"
      invalidMessage="Control Token 无效，请重新输入。Read Token 会话未受影响。"
      onSubmit={onSubmit}
      onClose={onClose}
    />
  );
}
