import type { BackendOperationViewModel } from "../backendModels";

interface BackendOperationFeedbackProps {
  operation: BackendOperationViewModel;
  onRetryUnknown: () => void;
  onRefreshStatus: () => void;
  onDismiss: () => void;
}

export function BackendOperationFeedback({
  operation,
  onRetryUnknown,
  onRefreshStatus,
  onDismiss,
}: BackendOperationFeedbackProps) {
  switch (operation.phase) {
    case "submitting":
      return (
        <aside className="backend-operation-feedback info" role="status">
          <div>
            <strong>正在提交 {operation.actionLabel} 请求</strong>
            <span>请等待服务器返回明确结果；当前 POST 不会被页面关闭路径中止。</span>
          </div>
        </aside>
      );
    case "submission_unknown":
      return (
        <aside className="backend-operation-feedback warning" role="alert">
          <div>
            <strong>{operation.actionLabel} 请求结果暂时无法确认</strong>
            <span>{operation.message}</span>
          </div>
          <div className="backend-feedback-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={onRefreshStatus}
            >
              刷新 Backend Status
            </button>
            <button
              type="button"
              className="primary-button"
              onClick={onRetryUnknown}
            >
              确认请求状态
            </button>
          </div>
        </aside>
      );
    case "completed":
      return (
        <aside className="backend-operation-feedback success" role="status">
          <div>
            <strong>{operation.request.actionLabel} 操作成功</strong>
            <span>
              {operation.request.resultLabel ??
                "Supervisor 已完成 Gateway 控制请求。"}
            </span>
          </div>
          <button type="button" className="secondary-button" onClick={onDismiss}>
            确认并同步状态
          </button>
        </aside>
      );
    case "failed":
      return (
        <aside className="backend-operation-feedback danger" role="alert">
          <div>
            <strong>
              {operation.actionLabel === null
                ? "控制请求未完成"
                : `${operation.actionLabel} 请求未完成`}
            </strong>
            <span>{operation.message}</span>
          </div>
          <button type="button" className="secondary-button" onClick={onDismiss}>
            确认并刷新状态
          </button>
        </aside>
      );
    case "idle":
    case "confirming":
    case "awaiting_control_token":
    case "tracking":
      return null;
  }
}
