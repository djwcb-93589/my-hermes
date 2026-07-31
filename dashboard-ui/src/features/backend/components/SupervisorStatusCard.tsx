import { StatusBadge } from "../../../components/StatusBadge";
import { formatBackendTimestamp } from "../backendFormat";
import type { SupervisorViewModel } from "../backendModels";

interface SupervisorStatusCardProps {
  supervisor: SupervisorViewModel;
}

export function SupervisorStatusCard({
  supervisor,
}: SupervisorStatusCardProps) {
  return (
    <section
      className="content-card backend-status-card"
      aria-labelledby="supervisor-status-title"
    >
      <div className="backend-card-heading">
        <div>
          <p className="eyebrow">SUPERVISOR</p>
          <h2 id="supervisor-status-title">{supervisor.statusLabel}</h2>
        </div>
        <StatusBadge status={supervisor.status} label={supervisor.stateLabel} />
      </div>
      <dl className="status-list">
        <div>
          <dt>Online</dt>
          <dd>{supervisor.online ? "是" : "否"}</dd>
        </div>
        <div>
          <dt>Instance state</dt>
          <dd>{supervisor.stateLabel}</dd>
        </div>
        <div>
          <dt>Lease expires at</dt>
          <dd>{formatBackendTimestamp(supervisor.leaseExpiresAt)}</dd>
        </div>
      </dl>
      <p className="backend-card-note">
        控制能力以 Supervisor 在线状态、Gateway ownership 和后端再次校验为准。
      </p>
    </section>
  );
}
