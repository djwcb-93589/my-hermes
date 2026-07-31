import type { StatusKind } from "../../components/StatusBadge";

export interface DashboardStatusViewModel {
  applicationName: string;
  projectVersion: string;
  webStatus: StatusKind;
  gatewayLeaseStatus: StatusKind;
  currentTime: string;
}

export interface BackendStatusViewModel {
  observedAt: string;
  supervisorStatus: StatusKind;
  gatewayStatus: StatusKind;
  ownershipStatus: StatusKind;
  leaseStatus: StatusKind;
  configChanged: boolean | null;
  restartRecommended: boolean | null;
}

export interface RuntimeComponentViewModel {
  key: string;
  componentType: string;
  componentId: string;
  reportedStatus: StatusKind;
  freshnessStatus: StatusKind;
  effectiveStatus: StatusKind;
  enabled: boolean | null;
  phase: string | null;
  workerCount: number | null;
  queueDepth: number | null;
  heartbeatAgeSeconds: number;
}

export interface RuntimeComponentsViewModel {
  observedAt: string;
  items: RuntimeComponentViewModel[];
  truncated: boolean;
}

export interface MonitoringOverviewViewModel {
  windowStartedAt: number;
  windowEndedAt: number;
  hasData: boolean;
  runCount: number;
  runSuccessRate: number | null;
  modelCallCount: number;
  toolCallCount: number;
  toolFailureCount: number;
  averageDurationMs: number | null;
  tokenCoverageRate: number | null;
  totalTokens: number | null;
}

export interface ToolStatViewModel {
  toolName: string;
  callCount: number;
  failureCount: number;
  successRate: number | null;
  averageDurationMs: number | null;
}

export interface ToolStatsViewModel {
  items: ToolStatViewModel[];
}

export interface DatabaseProbeViewModel {
  name: string;
  status: StatusKind;
  durationMs: number;
}

export interface DatabaseHealthViewModel {
  checkedAt: string;
  overallStatus: StatusKind;
  schemaStatus: StatusKind;
  schemaVersion: string;
  requiredStructuresAvailable: boolean;
  journalMode: string;
  usedSpaceBytes: number | null;
  probeDurationMs: number;
  budgetExhausted: boolean;
  probes: DatabaseProbeViewModel[];
}
