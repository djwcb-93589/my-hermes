export type GatewayLeaseStatus =
  | "running"
  | "stale"
  | "stopped"
  | "unavailable";

export interface StatusResponseDto {
  application_name: string;
  project_version: string | null;
  web_status: string;
  gateway: {
    status: GatewayLeaseStatus;
    heartbeat_at: string | null;
    expires_at: string | null;
  };
  current_time: string;
}

export type SupervisorInstanceState = "online" | "offline" | "unknown";
export type BackendObservedState =
  | "stopped"
  | "starting"
  | "running"
  | "stopping"
  | "exited"
  | "unknown";
export type BackendOwnership =
  | "managed"
  | "unmanaged"
  | "none"
  | "uncertain";
export type BackendControlAction = "start" | "stop" | "restart";
export type BackendControlRequestStatus =
  | "pending"
  | "claimed"
  | "executing"
  | "succeeded"
  | "failed"
  | "rejected";
export type BackendResultCode =
  | "started"
  | "stopped"
  | "restarted"
  | "already_running"
  | "already_stopped"
  | "unmanaged_instance"
  | "ownership_uncertain"
  | "control_conflict"
  | "start_failed"
  | "stop_failed"
  | "restart_failed"
  | "gateway_exited_before_ready"
  | "control_timeout"
  | "supervisor_lease_lost";

export interface BackendControlAcceptedApiResponse {
  request_id: string;
  action: BackendControlAction;
  status: BackendControlRequestStatus;
}

export interface BackendControlRequestApiResponse {
  request_id: string;
  backend_type: "gateway";
  action: BackendControlAction;
  status: BackendControlRequestStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  result_code: BackendResultCode | null;
  result_reference: string | null;
  exception_type: string | null;
  forced_termination: boolean;
}

export interface BackendStatusApiResponse {
  observed_at: string;
  supervisor: {
    online: boolean;
    lease_expires_at: string | null;
    instance_state: SupervisorInstanceState;
  };
  gateway: {
    observed_state: BackendObservedState;
    ownership: BackendOwnership;
    lease_active: boolean;
    managed: boolean;
    started_at: string | null;
    last_exit_at: string | null;
    last_exit_code: number | null;
    config_changed_since_start: boolean | null;
    restart_recommended: boolean | null;
  };
  latest_request: BackendControlRequestApiResponse | null;
}

export type BackendStatusResponseDto = BackendStatusApiResponse;

export type RuntimeReportedState =
  | "starting"
  | "running"
  | "idle"
  | "stopping"
  | "stopped"
  | "failed";
export type RuntimeFreshness =
  | "fresh"
  | "stale"
  | "terminal"
  | "clock_skewed";
export type RuntimeEffectiveStatus =
  | "healthy"
  | "degraded"
  | "stale"
  | "stopped"
  | "failed";

export interface RuntimeMetadataDto {
  enabled?: boolean;
  phase?: string;
  worker_count?: number;
  queue_depth?: number;
}

export interface RuntimeComponentDto {
  component_type: string;
  component_id: string;
  instance_id: string;
  reported_state: RuntimeReportedState;
  freshness: RuntimeFreshness;
  effective_status: RuntimeEffectiveStatus;
  started_at: string | null;
  heartbeat_at: string;
  heartbeat_age_seconds: number;
  heartbeat_interval_seconds: number;
  stale_after_seconds: number;
  is_stale: boolean;
  stopped_at: string | null;
  error_type: string | null;
  metadata: RuntimeMetadataDto;
}

export interface RuntimeComponentListResponseDto {
  observed_at: string;
  items: RuntimeComponentDto[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface MonitoringWindowDto {
  started_at: number;
  ended_at: number;
  environment: string | null;
  tool_name: string | null;
}

export interface MonitoringOverviewResponseDto {
  window: MonitoringWindowDto;
  runs: {
    run_count: number;
    completed_count: number;
    failed_count: number;
    cancelled_count: number;
    other_terminal_count: number;
    success_rate: number | null;
    average_iterations: number | null;
    average_tool_call_count: number | null;
    runs_with_final_reply: number;
    runs_without_final_reply: number;
  };
  model_calls: {
    model_call_count: number;
    calls_with_text: number;
    calls_without_text: number;
    total_tool_call_count: number;
    average_tool_call_count: number | null;
    total_prompt_tokens: number | null;
    total_completion_tokens: number | null;
    total_tokens: number | null;
    token_coverage_count: number;
    average_duration_ms: number | null;
  };
  tool_calls: {
    tool_call_count: number;
    successful_tool_call_count: number;
    failed_tool_call_count: number;
    success_rate: number | null;
    average_duration_ms: number | null;
  };
  tool_executions: {
    execution_count: number;
    prepared_count: number;
    awaiting_approval_count: number;
    running_count: number;
    succeeded_count: number;
    failed_count: number;
    unknown_count: number;
    with_result_count: number;
    with_external_operation_count: number;
    average_attempt_count: number | null;
  };
}

export interface ToolStatsResponseDto {
  window: MonitoringWindowDto;
  items: Array<{
    tool_name: string;
    call_count: number;
    success_count: number;
    failure_count: number;
    success_rate: number | null;
    average_duration_ms: number | null;
  }>;
}

export type DatabaseHealthStatus =
  | "healthy"
  | "degraded"
  | "unavailable"
  | "incompatible";
export type DatabaseJournalMode =
  | "delete"
  | "truncate"
  | "persist"
  | "memory"
  | "wal"
  | "off"
  | "other";
export type DatabaseProbeStatus =
  | "succeeded"
  | "unavailable"
  | "busy"
  | "failed"
  | "skipped";
export type DatabaseProbeName =
  | "open_connection"
  | "read_schema_version"
  | "read_journal_metrics"
  | "read_storage_metrics"
  | "recent_observation_lookup"
  | "recent_tool_execution_lookup";

export interface DatabaseHealthResponseDto {
  checked_at: string;
  status: DatabaseHealthStatus;
  schema: {
    current_version: number | null;
    expected_version: number;
    user_version: number | null;
    compatible: boolean;
    required_structures_available: boolean;
  };
  storage: {
    page_size_bytes: number;
    page_count: number;
    freelist_page_count: number;
    database_size_bytes: number;
    free_space_bytes: number;
    used_space_bytes: number;
    database_file_size_bytes: number | null;
    wal_present: boolean | null;
    wal_size_bytes: number | null;
  } | null;
  journal: {
    journal_mode: DatabaseJournalMode;
    query_only: boolean;
    foreign_keys: boolean;
    busy_timeout_ms: number;
  } | null;
  probes: {
    checked_at: string;
    total_duration_ms: number;
    budget_ms: number;
    budget_exhausted: boolean;
    probes: Array<{
      probe_name: DatabaseProbeName;
      status: DatabaseProbeStatus;
      duration_ms: number;
      returned_row_count: number | null;
      reason: string | null;
    }>;
  };
}
