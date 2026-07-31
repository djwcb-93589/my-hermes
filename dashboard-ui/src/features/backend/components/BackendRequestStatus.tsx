import { StatusBadge } from "../../../components/StatusBadge";
import { formatBackendTimestamp } from "../backendFormat";
import type { ControlRequestViewModel } from "../backendModels";

interface BackendRequestStatusProps {
  request: ControlRequestViewModel;
  current: boolean;
  queryError?: string | null;
}

export function BackendRequestStatus({
  request,
  current,
  queryError = null,
}: BackendRequestStatusProps) {
  return (
    <section
      className="content-card backend-request-card"
      aria-labelledby="backend-request-title"
      aria-live="polite"
    >
      <div className="backend-card-heading">
        <div>
          <p className="eyebrow">
            {current ? "CURRENT REQUEST" : "LATEST REQUEST"}
          </p>
          <h2 id="backend-request-title">{request.actionLabel} Gateway</h2>
        </div>
        <StatusBadge status={request.statusKind} label={request.statusLabel} />
      </div>
      {queryError === null ? null : (
        <p className="backend-query-error" role="alert">
          {queryError}
        </p>
      )}
      <dl className="backend-request-grid">
        <div>
          <dt>Action</dt>
          <dd>{request.actionLabel}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{request.statusLabel}</dd>
        </div>
        <div>
          <dt>Created at</dt>
          <dd>{formatBackendTimestamp(request.createdAt)}</dd>
        </div>
        <div>
          <dt>Started at</dt>
          <dd>{formatBackendTimestamp(request.startedAt)}</dd>
        </div>
        <div>
          <dt>Completed at</dt>
          <dd>{formatBackendTimestamp(request.completedAt)}</dd>
        </div>
        <div>
          <dt>Result</dt>
          <dd>{request.resultLabel ?? "尚无终态结果"}</dd>
        </div>
        <div>
          <dt>Forced termination</dt>
          <dd>{request.forcedTermination ? "是" : "否"}</dd>
        </div>
      </dl>
      {request.forcedTermination ? (
        <p className="forced-termination-warning" role="alert">
          Gateway 未在优雅关闭时间内退出，Supervisor 使用了强制终止。
        </p>
      ) : null}
    </section>
  );
}
