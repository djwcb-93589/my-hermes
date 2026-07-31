import { useRef } from "react";

import type { BackendControlAction } from "../../api/backend";
import { BackendActionConfirmation } from "./components/BackendActionConfirmation";
import {
  BackendControlActions,
  type BackendActionButtonRefs,
} from "./components/BackendControlActions";
import { BackendControlTokenDialog } from "./components/BackendControlTokenDialog";
import { BackendOperationFeedback } from "./components/BackendOperationFeedback";
import { BackendRequestStatus } from "./components/BackendRequestStatus";
import { BackendRestartNotice } from "./components/BackendRestartNotice";
import { GatewayStatusCard } from "./components/GatewayStatusCard";
import { SupervisorStatusCard } from "./components/SupervisorStatusCard";
import { formatBackendTimestamp } from "./backendFormat";
import { useBackendController } from "./useBackendController";

export function BackendPage() {
  const controller = useBackendController();
  const startButtonRef = useRef<HTMLButtonElement>(null);
  const stopButtonRef = useRef<HTMLButtonElement>(null);
  const restartButtonRef = useRef<HTMLButtonElement>(null);
  const buttonRefs: BackendActionButtonRefs = {
    start: startButtonRef,
    stop: stopButtonRef,
    restart: restartButtonRef,
  };
  const view = controller.displayedView;
  const tokenAction =
    controller.operation.phase === "awaiting_control_token" ||
    controller.operation.phase === "submitting"
      ? controller.operation.action
      : null;
  const tokenActionLabel =
    controller.operation.phase === "awaiting_control_token" ||
    controller.operation.phase === "submitting"
      ? controller.operation.actionLabel
      : null;
  const currentRequest =
    controller.operation.phase === "tracking" ||
    controller.operation.phase === "completed"
      ? controller.operation.request
      : controller.operation.phase === "failed"
        ? controller.operation.request
        : null;
  const requestQueryError =
    controller.operation.phase === "tracking"
      ? controller.operation.queryError
      : null;

  return (
    <>
      <header className="hero backend-hero">
        <div>
          <p className="eyebrow">SUPERVISED BACKEND</p>
          <h1>Gateway 状态清晰，控制意图可追踪</h1>
          <p>
            读取 Supervisor 与 Gateway 的安全状态投影。所有生命周期操作都经过明确确认、一次性 Control Token、幂等提交和持久化请求跟踪。
          </p>
        </div>
        <div className="hero-actions">
          <span className="controlled-chip">CONTROLLED LIFECYCLE</span>
          <button
            type="button"
            className="secondary-button"
            disabled={controller.pageState.phase === "loading"}
            onClick={controller.refreshStatus}
          >
            刷新 Backend Status
          </button>
        </div>
      </header>

      <aside className="configuration-boundary-note">
        本页面不读取 PID、命令行、进程列表、SQLite 或 lease 表，也不会接触运行时 Python 对象；真实安全决策始终由后端 Supervisor 控制链完成。
      </aside>

      <BackendOperationFeedback
        operation={controller.operation}
        onRetryUnknown={controller.retryUnknownSubmission}
        onRefreshStatus={controller.refreshStatus}
        onDismiss={controller.dismissOperation}
      />

      {controller.pageState.phase === "loading" ? (
        <div className="config-page-state" role="status">
          <span className="loading-pulse" aria-hidden="true" />
          正在读取 Backend Status…
        </div>
      ) : null}

      {controller.pageState.phase === "error" ? (
        <div className="config-page-state config-page-error">
          <span role="alert">{controller.pageState.message}</span>
          <button
            type="button"
            className="secondary-button"
            onClick={controller.refreshStatus}
          >
            重新读取
          </button>
        </div>
      ) : null}

      {controller.pageState.phase === "ready" && view !== null ? (
        <>
          {controller.pageState.statusError === null ? null : (
            <div className="page-error backend-status-error" role="alert">
              <span>{controller.pageState.statusError}</span>
              <button
                type="button"
                className="text-button"
                onClick={controller.refreshStatus}
              >
                立即重试
              </button>
            </div>
          )}
          <BackendRestartNotice visible={view.gateway.showRestartNotice} />
          <p className="backend-observed-at">
            状态观测时间：{formatBackendTimestamp(view.observedAt)}
          </p>
          <section className="two-column-grid backend-status-grid">
            <SupervisorStatusCard supervisor={view.supervisor} />
            <GatewayStatusCard gateway={view.gateway} />
          </section>
          <BackendControlActions
            controls={view.controls}
            buttonRefs={buttonRefs}
            onAction={controller.beginAction}
          />
          {currentRequest !== null || view.latestRequest !== null ? (
            <BackendRequestStatus
              request={currentRequest ?? view.latestRequest!}
              current={currentRequest !== null}
              queryError={requestQueryError}
            />
          ) : null}
        </>
      ) : null}

      {controller.operation.phase === "confirming" && view !== null ? (
        <BackendActionConfirmation
          confirmation={controller.operation.confirmation}
          triggerRef={buttonRefs[controller.operation.confirmation.action]}
          onCancel={controller.cancelConfirmation}
          onConfirm={controller.confirmAction}
        />
      ) : null}

      {tokenAction === null || tokenActionLabel === null ? null : (
        <BackendControlTokenDialog
          actionLabel={tokenActionLabel}
          triggerRef={buttonRefForAction(tokenAction, buttonRefs)}
          onSubmit={controller.submitControlToken}
          onClose={controller.closeControlDialog}
        />
      )}
    </>
  );
}

function buttonRefForAction(
  action: BackendControlAction,
  refs: BackendActionButtonRefs,
) {
  return refs[action];
}
