import { StatusBadge } from "../../../components/StatusBadge";
import {
  formatBackendFlag,
  formatBackendTimestamp,
} from "../backendFormat";
import type { GatewayViewModel } from "../backendModels";

interface GatewayStatusCardProps {
  gateway: GatewayViewModel;
}

export function GatewayStatusCard({ gateway }: GatewayStatusCardProps) {
  return (
    <section
      className="content-card backend-status-card"
      aria-labelledby="gateway-status-title"
    >
      <div className="backend-card-heading">
        <div>
          <p className="eyebrow">GATEWAY</p>
          <h2 id="gateway-status-title">
            Gateway {gateway.observedStateLabel}
          </h2>
        </div>
        <div className="inline-badges backend-card-badges">
          <StatusBadge
            status={gateway.observedStatus}
            label={gateway.observedStateLabel}
          />
          <StatusBadge
            status={gateway.ownershipStatus}
            label={gateway.ownershipLabel}
          />
        </div>
      </div>
      <p className="backend-ownership-note">{gateway.ownershipExplanation}</p>
      <dl className="status-list">
        <div>
          <dt>Gateway lease</dt>
          <dd>{gateway.leaseActive ? "Active" : "Inactive"}</dd>
        </div>
        <div>
          <dt>Supervisor managed</dt>
          <dd>{gateway.managed ? "Managed" : "Not managed"}</dd>
        </div>
        <div>
          <dt>Started at</dt>
          <dd>{formatBackendTimestamp(gateway.startedAt)}</dd>
        </div>
        <div>
          <dt>Last exit at</dt>
          <dd>{formatBackendTimestamp(gateway.lastExitAt)}</dd>
        </div>
        <div>
          <dt>Last exit code</dt>
          <dd>{gateway.lastExitCode ?? "暂无"}</dd>
        </div>
        <div>
          <dt>Config changed</dt>
          <dd>{formatBackendFlag(gateway.configChangedSinceStart)}</dd>
        </div>
        <div>
          <dt>Restart recommended</dt>
          <dd>{formatBackendFlag(gateway.restartRecommended)}</dd>
        </div>
      </dl>
    </section>
  );
}
