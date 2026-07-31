import type {
  BackendControlAction,
  BackendControlRequestStatus,
  BackendObservedState,
  BackendOwnership,
  SupervisorInstanceState,
} from "../../api/backend";
import type { StatusKind } from "../../components/StatusBadge";

export interface SupervisorViewModel {
  online: boolean;
  state: SupervisorInstanceState;
  status: StatusKind;
  statusLabel: string;
  stateLabel: string;
  leaseExpiresAt: string | null;
}

export interface GatewayViewModel {
  observedState: BackendObservedState;
  observedStatus: StatusKind;
  observedStateLabel: string;
  ownership: BackendOwnership;
  ownershipStatus: StatusKind;
  ownershipLabel: string;
  ownershipExplanation: string;
  leaseActive: boolean;
  managed: boolean;
  startedAt: string | null;
  lastExitAt: string | null;
  lastExitCode: number | null;
  configChangedSinceStart: boolean | null;
  restartRecommended: boolean | null;
  showRestartNotice: boolean;
}

export interface ControlActionAvailabilityViewModel {
  action: BackendControlAction;
  label: string;
  enabled: boolean;
  reason: string;
}

export interface ControlAvailabilityViewModel {
  start: ControlActionAvailabilityViewModel;
  stop: ControlActionAvailabilityViewModel;
  restart: ControlActionAvailabilityViewModel;
}

export interface ControlRequestViewModel {
  requestId: string;
  action: BackendControlAction;
  actionLabel: string;
  status: BackendControlRequestStatus;
  statusKind: StatusKind;
  statusLabel: string;
  terminal: boolean;
  createdAt: string | null;
  startedAt: string | null;
  completedAt: string | null;
  resultLabel: string | null;
  forcedTermination: boolean;
}

export interface BackendPageViewModel {
  observedAt: string;
  supervisor: SupervisorViewModel;
  gateway: GatewayViewModel;
  controls: ControlAvailabilityViewModel;
  latestRequest: ControlRequestViewModel | null;
}

export interface BackendActionConfirmationViewModel {
  action: BackendControlAction;
  actionLabel: string;
  title: string;
  description: string;
  warnings: string[];
  gatewayStateLabel: string;
  ownershipLabel: string;
}

export type BackendPageState =
  | { phase: "loading" }
  | {
      phase: "ready";
      view: BackendPageViewModel;
      statusError: string | null;
    }
  | { phase: "error"; message: string };

export type BackendOperationViewModel =
  | { phase: "idle" }
  | {
      phase: "confirming";
      confirmation: BackendActionConfirmationViewModel;
    }
  | {
      phase: "awaiting_control_token";
      action: BackendControlAction;
      actionLabel: string;
    }
  | {
      phase: "submitting";
      action: BackendControlAction;
      actionLabel: string;
    }
  | {
      phase: "submission_unknown";
      action: BackendControlAction;
      actionLabel: string;
      message: string;
    }
  | {
      phase: "tracking";
      request: ControlRequestViewModel;
      queryError: string | null;
    }
  | { phase: "completed"; request: ControlRequestViewModel }
  | {
      phase: "failed";
      action: BackendControlAction | null;
      actionLabel: string | null;
      message: string;
      request: ControlRequestViewModel | null;
    };
