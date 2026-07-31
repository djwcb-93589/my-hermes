import { readBackendStatus } from "../../api/backend";
import { readDatabaseHealth } from "../../api/database";
import type { HttpClient } from "../../api/http";
import {
  readMonitoringOverview,
  readToolStats,
} from "../../api/monitoring";
import { listRuntimeComponents } from "../../api/runtime";
import { readStatus } from "../../api/status";
import type { StatusKind } from "../../components/StatusBadge";
import type {
  BackendStatusViewModel,
  DashboardStatusViewModel,
  DatabaseHealthViewModel,
  MonitoringOverviewViewModel,
  RuntimeComponentsViewModel,
  ToolStatsViewModel,
} from "./overviewModels";

const DAY_SECONDS = 24 * 60 * 60;

function monitoringWindow(): { startedAt: number; endedAt: number } {
  const endedAt = Math.floor(Date.now() / 1000);
  return { startedAt: endedAt - DAY_SECONDS, endedAt };
}

export async function loadDashboardStatus(
  client: HttpClient,
  signal: AbortSignal,
): Promise<DashboardStatusViewModel> {
  const response = await readStatus(client, signal);
  return {
    applicationName: response.application_name,
    projectVersion: response.project_version ?? "未标注",
    webStatus: response.web_status === "running" ? "running" : "unknown",
    gatewayLeaseStatus: gatewayLeaseStatus(response.gateway.status),
    currentTime: response.current_time,
  };
}

export async function loadBackendStatus(
  client: HttpClient,
  signal: AbortSignal,
): Promise<BackendStatusViewModel> {
  const response = await readBackendStatus(client, signal);
  return {
    observedAt: response.observed_at,
    supervisorStatus: response.supervisor.online ? "online" : "offline",
    gatewayStatus: backendObservedStatus(response.gateway.observed_state),
    ownershipStatus: ownershipStatus(response.gateway.ownership),
    leaseStatus: response.gateway.lease_active ? "online" : "offline",
    configChanged: response.gateway.config_changed_since_start,
    restartRecommended: response.gateway.restart_recommended,
  };
}

export async function loadRuntimeComponents(
  client: HttpClient,
  signal: AbortSignal,
): Promise<RuntimeComponentsViewModel> {
  const response = await listRuntimeComponents(client, signal);
  return {
    observedAt: response.observed_at,
    truncated: response.has_more,
    items: response.items.map((item) => ({
      key: `${item.component_type}:${item.component_id}`,
      componentType: item.component_type,
      componentId: item.component_id,
      reportedStatus: runtimeReportedStatus(item.reported_state),
      freshnessStatus: runtimeFreshnessStatus(item.freshness),
      effectiveStatus: runtimeEffectiveStatus(item.effective_status),
      enabled:
        typeof item.metadata.enabled === "boolean"
          ? item.metadata.enabled
          : null,
      phase:
        typeof item.metadata.phase === "string" ? item.metadata.phase : null,
      workerCount:
        typeof item.metadata.worker_count === "number"
          ? item.metadata.worker_count
          : null,
      queueDepth:
        typeof item.metadata.queue_depth === "number"
          ? item.metadata.queue_depth
          : null,
      heartbeatAgeSeconds: item.heartbeat_age_seconds,
    })),
  };
}

export async function loadMonitoringOverview(
  client: HttpClient,
  signal: AbortSignal,
): Promise<MonitoringOverviewViewModel> {
  const response = await readMonitoringOverview(
    client,
    monitoringWindow(),
    signal,
  );
  const observedEvents =
    response.runs.run_count +
    response.model_calls.model_call_count +
    response.tool_calls.tool_call_count;
  return {
    windowStartedAt: response.window.started_at,
    windowEndedAt: response.window.ended_at,
    hasData: observedEvents > 0,
    runCount: response.runs.run_count,
    runSuccessRate: response.runs.success_rate,
    modelCallCount: response.model_calls.model_call_count,
    toolCallCount: response.tool_calls.tool_call_count,
    toolFailureCount: response.tool_calls.failed_tool_call_count,
    averageDurationMs:
      response.model_calls.average_duration_ms ??
      response.tool_calls.average_duration_ms,
    tokenCoverageRate:
      response.model_calls.model_call_count === 0
        ? null
        : response.model_calls.token_coverage_count /
          response.model_calls.model_call_count,
    totalTokens: response.model_calls.total_tokens,
  };
}

export async function loadToolStats(
  client: HttpClient,
  signal: AbortSignal,
): Promise<ToolStatsViewModel> {
  const response = await readToolStats(client, monitoringWindow(), signal);
  return {
    items: response.items.slice(0, 8).map((item) => ({
      toolName: item.tool_name,
      callCount: item.call_count,
      failureCount: item.failure_count,
      successRate: item.success_rate,
      averageDurationMs: item.average_duration_ms,
    })),
  };
}

export async function loadDatabaseHealth(
  client: HttpClient,
  signal: AbortSignal,
): Promise<DatabaseHealthViewModel> {
  const response = await readDatabaseHealth(client, signal);
  return {
    checkedAt: response.checked_at,
    overallStatus: databaseHealthStatus(response.status),
    schemaStatus: response.schema.compatible ? "compatible" : "incompatible",
    schemaVersion: `${response.schema.current_version ?? "未知"} / ${response.schema.expected_version}`,
    requiredStructuresAvailable:
      response.schema.required_structures_available,
    journalMode: response.journal?.journal_mode.toUpperCase() ?? "不可用",
    usedSpaceBytes: response.storage?.used_space_bytes ?? null,
    probeDurationMs: response.probes.total_duration_ms,
    budgetExhausted: response.probes.budget_exhausted,
    probes: response.probes.probes.map((probe) => ({
      name: probeName(probe.probe_name),
      status: databaseProbeStatus(probe.status),
      durationMs: probe.duration_ms,
    })),
  };
}

function gatewayLeaseStatus(value: string): StatusKind {
  switch (value) {
    case "running":
      return "running";
    case "stale":
      return "stale";
    case "stopped":
      return "stopped";
    case "unavailable":
      return "unavailable";
    default:
      return "unknown";
  }
}

function backendObservedStatus(value: string): StatusKind {
  switch (value) {
    case "running":
      return "running";
    case "starting":
      return "starting";
    case "stopping":
      return "stopping";
    case "stopped":
    case "exited":
      return "stopped";
    case "unknown":
      return "unknown";
    default:
      return "unknown";
  }
}

function ownershipStatus(value: string): StatusKind {
  switch (value) {
    case "managed":
      return "managed";
    case "unmanaged":
    case "none":
      return "unmanaged";
    case "uncertain":
      return "unknown";
    default:
      return "unknown";
  }
}

function runtimeReportedStatus(value: string): StatusKind {
  switch (value) {
    case "starting":
      return "starting";
    case "running":
      return "running";
    case "idle":
      return "idle";
    case "stopping":
      return "stopping";
    case "stopped":
      return "stopped";
    case "failed":
      return "failed";
    default:
      return "unknown";
  }
}

function runtimeFreshnessStatus(value: string): StatusKind {
  switch (value) {
    case "fresh":
      return "fresh";
    case "stale":
      return "stale";
    case "terminal":
      return "terminal";
    case "clock_skewed":
      return "clock_skewed";
    default:
      return "unknown";
  }
}

function runtimeEffectiveStatus(value: string): StatusKind {
  switch (value) {
    case "healthy":
      return "healthy";
    case "degraded":
      return "degraded";
    case "stale":
      return "stale";
    case "stopped":
      return "stopped";
    case "failed":
      return "failed";
    default:
      return "unknown";
  }
}

function databaseHealthStatus(value: string): StatusKind {
  switch (value) {
    case "healthy":
      return "healthy";
    case "degraded":
      return "degraded";
    case "unavailable":
      return "unavailable";
    case "incompatible":
      return "incompatible";
    default:
      return "unknown";
  }
}

function databaseProbeStatus(value: string): StatusKind {
  switch (value) {
    case "succeeded":
      return "succeeded";
    case "unavailable":
      return "unavailable";
    case "busy":
      return "busy";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    default:
      return "unknown";
  }
}

function probeName(value: string): string {
  switch (value) {
    case "open_connection":
      return "只读连接";
    case "read_schema_version":
      return "Schema 版本";
    case "read_journal_metrics":
      return "Journal 指标";
    case "read_storage_metrics":
      return "存储指标";
    case "recent_observation_lookup":
      return "最近 Observation";
    case "recent_tool_execution_lookup":
      return "最近工具执行";
    default:
      return "未知探针";
  }
}
