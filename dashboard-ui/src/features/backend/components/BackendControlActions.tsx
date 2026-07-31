import type { RefObject } from "react";

import type { BackendControlAction } from "../../../api/backend";
import type { ControlAvailabilityViewModel } from "../backendModels";

export type BackendActionButtonRefs = Record<
  BackendControlAction,
  RefObject<HTMLButtonElement | null>
>;

interface BackendControlActionsProps {
  controls: ControlAvailabilityViewModel;
  buttonRefs: BackendActionButtonRefs;
  onAction: (action: BackendControlAction) => void;
}

export function BackendControlActions({
  controls,
  buttonRefs,
  onAction,
}: BackendControlActionsProps) {
  return (
    <section
      className="backend-control-section"
      aria-labelledby="backend-controls-title"
    >
      <div className="section-heading backend-section-heading">
        <div>
          <p className="eyebrow">CONTROL</p>
          <h2 id="backend-controls-title">Gateway 生命周期操作</h2>
        </div>
        <p>前端仅做明显状态限制；后端会在提交时重新执行完整安全校验。</p>
      </div>
      <div className="backend-action-grid">
        {(["start", "stop", "restart"] as const).map((action) => {
          const control = controls[action];
          const reasonId = `backend-${action}-availability`;
          return (
            <article key={action} className="content-card backend-action-card">
              <div>
                <h3>{control.label} Gateway</h3>
                <p>
                  {action === "start"
                    ? "请求独立 Supervisor 启动正式 Gateway。"
                    : action === "stop"
                      ? "优先请求 Supervisor 优雅停止 Gateway。"
                      : "请求 Supervisor 停止后重新启动 Gateway。"}
                </p>
              </div>
              <p
                id={reasonId}
                className={`control-availability ${
                  control.enabled ? "enabled" : "disabled"
                }`}
              >
                {control.reason}
              </p>
              <button
                ref={buttonRefs[action]}
                type="button"
                className={
                  action === "stop"
                    ? "danger-button"
                    : action === "restart"
                      ? "primary-button"
                      : "secondary-button"
                }
                disabled={!control.enabled}
                aria-describedby={reasonId}
                onClick={() => onAction(action)}
              >
                {control.label} Gateway
              </button>
            </article>
          );
        })}
      </div>
    </section>
  );
}
