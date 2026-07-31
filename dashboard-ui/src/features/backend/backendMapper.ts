import type {
  BackendControlAcceptedApiResponse,
  BackendControlAction,
  BackendControlRequestApiResponse,
  BackendControlRequestStatus,
  BackendObservedState,
  BackendOwnership,
  BackendResultCode,
  BackendStatusApiResponse,
  SupervisorInstanceState,
} from "../../api/backend";
import type { StatusKind } from "../../components/StatusBadge";
import type {
  BackendActionConfirmationViewModel,
  BackendPageViewModel,
  ControlActionAvailabilityViewModel,
  ControlAvailabilityViewModel,
  ControlRequestViewModel,
  GatewayViewModel,
  SupervisorViewModel,
} from "./backendModels";

export function mapBackendPage(
  response: BackendStatusApiResponse,
): BackendPageViewModel {
  const supervisor = mapSupervisor(response);
  const gateway = mapGateway(response);
  const latestRequest =
    response.latest_request === null
      ? null
      : mapControlRequest(response.latest_request);
  return {
    observedAt: response.observed_at,
    supervisor,
    gateway,
    controls: controlAvailability(supervisor, gateway, latestRequest),
    latestRequest,
  };
}

export function mapControlRequest(
  request: BackendControlRequestApiResponse,
): ControlRequestViewModel {
  return {
    requestId: request.request_id,
    action: request.action,
    actionLabel: actionLabel(request.action),
    status: request.status,
    statusKind: requestStatusKind(request.status),
    statusLabel: requestStatusLabel(request.status),
    terminal: isTerminalRequestStatus(request.status),
    createdAt: request.created_at,
    startedAt: request.started_at,
    completedAt: request.completed_at,
    resultLabel:
      request.result_code === null
        ? null
        : backendResultLabel(request.result_code),
    forcedTermination: request.forced_termination,
  };
}

export function mapAcceptedControlRequest(
  request: BackendControlAcceptedApiResponse,
): ControlRequestViewModel {
  return {
    requestId: request.request_id,
    action: request.action,
    actionLabel: actionLabel(request.action),
    status: request.status,
    statusKind: requestStatusKind(request.status),
    statusLabel: requestStatusLabel(request.status),
    terminal: isTerminalRequestStatus(request.status),
    createdAt: null,
    startedAt: null,
    completedAt: null,
    resultLabel: null,
    forcedTermination: false,
  };
}

export function buildActionConfirmation(
  action: BackendControlAction,
  view: BackendPageViewModel,
): BackendActionConfirmationViewModel {
  const shared = {
    action,
    actionLabel: actionLabel(action),
    gatewayStateLabel: view.gateway.observedStateLabel,
    ownershipLabel: view.gateway.ownershipLabel,
  };
  switch (action) {
    case "start":
      return {
        ...shared,
        title: "确认启动 Gateway",
        description: "将请求 Supervisor 启动 Gateway。",
        warnings: ["Gateway 将使用当前正式配置启动。"],
      };
    case "stop":
      return {
        ...shared,
        title: "确认停止 Gateway",
        description: "Gateway 当前会话和后台能力将停止。",
        warnings: ["Supervisor 会优先尝试优雅关闭。"],
      };
    case "restart":
      return {
        ...shared,
        title: "确认重启 Gateway",
        description: "Gateway 将先停止再重新启动。",
        warnings: [
          "正在处理的 Gateway 工作可能被中断。",
          "最新配置将在新实例启动时加载。",
        ],
      };
  }
}

export function lockBackendControls(
  view: BackendPageViewModel,
  reason: string,
): BackendPageViewModel {
  return {
    ...view,
    controls: disabledControls(reason),
  };
}

export function actionLabel(action: BackendControlAction): string {
  switch (action) {
    case "start":
      return "Start";
    case "stop":
      return "Stop";
    case "restart":
      return "Restart";
  }
}

export function isTerminalRequestStatus(
  status: BackendControlRequestStatus,
): boolean {
  return status === "succeeded" || status === "failed" || status === "rejected";
}

function mapSupervisor(response: BackendStatusApiResponse): SupervisorViewModel {
  const state = response.supervisor.instance_state;
  if (state === "unknown") {
    return {
      online: response.supervisor.online,
      state,
      status: "unknown",
      statusLabel: "Supervisor state unknown",
      stateLabel: supervisorStateLabel(state),
      leaseExpiresAt: response.supervisor.lease_expires_at,
    };
  }
  if (response.supervisor.online && state === "online") {
    return {
      online: true,
      state,
      status: "online",
      statusLabel: "Supervisor online",
      stateLabel: supervisorStateLabel(state),
      leaseExpiresAt: response.supervisor.lease_expires_at,
    };
  }
  return {
    online: false,
    state,
    status: "offline",
    statusLabel: "Supervisor offline",
    stateLabel: supervisorStateLabel(state),
    leaseExpiresAt: response.supervisor.lease_expires_at,
  };
}

function mapGateway(response: BackendStatusApiResponse): GatewayViewModel {
  const gateway = response.gateway;
  return {
    observedState: gateway.observed_state,
    observedStatus: observedStateKind(gateway.observed_state),
    observedStateLabel: observedStateLabel(gateway.observed_state),
    ownership: gateway.ownership,
    ownershipStatus: ownershipKind(gateway.ownership),
    ownershipLabel: ownershipLabel(gateway.ownership),
    ownershipExplanation: ownershipExplanation(
      gateway.ownership,
      gateway.lease_active,
    ),
    leaseActive: gateway.lease_active,
    managed: gateway.managed,
    startedAt: gateway.started_at,
    lastExitAt: gateway.last_exit_at,
    lastExitCode: gateway.last_exit_code,
    configChangedSinceStart: gateway.config_changed_since_start,
    restartRecommended: gateway.restart_recommended,
    showRestartNotice:
      gateway.config_changed_since_start === true ||
      gateway.restart_recommended === true,
  };
}

function controlAvailability(
  supervisor: SupervisorViewModel,
  gateway: GatewayViewModel,
  latestRequest: ControlRequestViewModel | null,
): ControlAvailabilityViewModel {
  const commonReason = commonControlDisabledReason(
    supervisor,
    gateway,
    latestRequest,
  );
  if (commonReason !== null) {
    return disabledControls(commonReason);
  }

  const startEnabled =
    (gateway.observedState === "stopped" ||
      gateway.observedState === "exited") &&
    (gateway.ownership === "none" || gateway.ownership === "managed");
  const stopEnabled =
    gateway.observedState === "running" &&
    gateway.ownership === "managed" &&
    gateway.managed;
  const restartEnabled = stopEnabled;
  return {
    start: actionAvailability(
      "start",
      startEnabled,
      startEnabled
        ? "当前状态允许请求 Supervisor 启动 Gateway。"
        : "仅当 Gateway 已停止或已退出时可以启动。",
    ),
    stop: actionAvailability(
      "stop",
      stopEnabled,
      stopEnabled
        ? "当前 Gateway 由 Supervisor 管理，可以请求停止。"
        : "只有当前 Supervisor 管理的运行中 Gateway 才能停止。",
    ),
    restart: actionAvailability(
      "restart",
      restartEnabled,
      restartEnabled
        ? "当前 Gateway 由 Supervisor 管理，可以请求重启。"
        : "只有当前 Supervisor 管理的运行中 Gateway 才能重启。",
    ),
  };
}

function commonControlDisabledReason(
  supervisor: SupervisorViewModel,
  gateway: GatewayViewModel,
  latestRequest: ControlRequestViewModel | null,
): string | null {
  if (!supervisor.online) {
    return supervisor.state === "unknown"
      ? "Supervisor 状态未知，控制操作已禁用。"
      : "Supervisor offline，控制操作已禁用。";
  }
  if (supervisor.state !== "online") {
    return "Supervisor 实例状态尚未确认，控制操作已禁用。";
  }
  if (latestRequest !== null && !latestRequest.terminal) {
    return `已有 ${latestRequest.statusLabel} 的控制请求，完成前不能提交新操作。`;
  }
  if (
    gateway.observedState === "starting" ||
    gateway.observedState === "stopping"
  ) {
    return `Gateway 正处于${gateway.observedStateLabel}，请等待状态稳定。`;
  }
  if (gateway.ownership === "unmanaged") {
    return "Gateway 不受当前 Supervisor 管理，无法从 Dashboard 控制。";
  }
  if (gateway.ownership === "uncertain") {
    return "Gateway 进程所有权无法安全确认，控制操作已禁用。";
  }
  if (gateway.observedState === "unknown") {
    return "Gateway 状态未知，控制操作已禁用。";
  }
  return null;
}

function disabledControls(reason: string): ControlAvailabilityViewModel {
  return {
    start: actionAvailability("start", false, reason),
    stop: actionAvailability("stop", false, reason),
    restart: actionAvailability("restart", false, reason),
  };
}

function actionAvailability(
  action: BackendControlAction,
  enabled: boolean,
  reason: string,
): ControlActionAvailabilityViewModel {
  return { action, label: actionLabel(action), enabled, reason };
}

function supervisorStateLabel(state: SupervisorInstanceState): string {
  switch (state) {
    case "online":
      return "online";
    case "offline":
      return "offline";
    case "unknown":
      return "unknown";
  }
}

function observedStateLabel(state: BackendObservedState): string {
  switch (state) {
    case "stopped":
      return "已停止";
    case "starting":
      return "启动中";
    case "running":
      return "运行中";
    case "stopping":
      return "停止中";
    case "exited":
      return "已退出";
    case "unknown":
      return "未知";
  }
}

function observedStateKind(state: BackendObservedState): StatusKind {
  switch (state) {
    case "stopped":
    case "exited":
      return "stopped";
    case "starting":
      return "starting";
    case "running":
      return "running";
    case "stopping":
      return "stopping";
    case "unknown":
      return "unknown";
  }
}

function ownershipLabel(ownership: BackendOwnership): string {
  switch (ownership) {
    case "managed":
      return "managed";
    case "unmanaged":
      return "unmanaged";
    case "none":
      return "none";
    case "uncertain":
      return "uncertain";
  }
}

function ownershipKind(ownership: BackendOwnership): StatusKind {
  switch (ownership) {
    case "managed":
      return "managed";
    case "unmanaged":
      return "unmanaged";
    case "none":
      return "offline";
    case "uncertain":
      return "unknown";
  }
}

function ownershipExplanation(
  ownership: BackendOwnership,
  leaseActive: boolean,
): string {
  switch (ownership) {
    case "managed":
      return "Gateway 由当前 Supervisor 管理；控制请求仍由后端最终校验。";
    case "unmanaged":
      return leaseActive
        ? "Gateway 在线，但并非由当前 Supervisor 管理；无法从 Dashboard 停止或重启。"
        : "检测到不受当前 Supervisor 管理的 Gateway 进程状态。";
    case "none":
      return "当前没有可归属到 Supervisor 的 Gateway 进程。";
    case "uncertain":
      return "Gateway 进程身份无法安全确认；Dashboard 不会尝试控制。";
  }
}

function requestStatusLabel(status: BackendControlRequestStatus): string {
  switch (status) {
    case "pending":
      return "请求已提交，等待 Supervisor";
    case "claimed":
      return "Supervisor 已领取请求";
    case "executing":
      return "正在执行";
    case "succeeded":
      return "操作成功";
    case "failed":
      return "操作失败";
    case "rejected":
      return "操作被拒绝";
  }
}

function requestStatusKind(status: BackendControlRequestStatus): StatusKind {
  switch (status) {
    case "pending":
    case "claimed":
      return "busy";
    case "executing":
      return "running";
    case "succeeded":
      return "succeeded";
    case "failed":
    case "rejected":
      return "failed";
  }
}

function backendResultLabel(result: BackendResultCode): string {
  switch (result) {
    case "started":
      return "Gateway 已启动";
    case "stopped":
      return "Gateway 已停止";
    case "restarted":
      return "Gateway 已重启";
    case "already_running":
      return "Gateway 已经在运行";
    case "already_stopped":
      return "Gateway 已经停止";
    case "unmanaged_instance":
      return "Gateway 不受当前 Supervisor 管理";
    case "ownership_uncertain":
      return "Gateway 进程所有权无法安全确认";
    case "control_conflict":
      return "存在冲突的控制请求";
    case "start_failed":
      return "Gateway 启动失败";
    case "stop_failed":
      return "Gateway 停止失败";
    case "restart_failed":
      return "Gateway 重启失败";
    case "gateway_exited_before_ready":
      return "Gateway 在就绪前退出";
    case "control_timeout":
      return "Gateway 控制操作超时";
    case "supervisor_lease_lost":
      return "Supervisor 已失去控制租约";
  }
}
