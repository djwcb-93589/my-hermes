import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { BackendControlAction } from "../../api/backend";
import { useAuth } from "../../auth/AuthContext";
import {
  actionLabel,
  buildActionConfirmation,
  lockBackendControls,
  mapAcceptedControlRequest,
} from "./backendMapper";
import type {
  BackendActionConfirmationViewModel,
  BackendOperationViewModel,
  BackendPageState,
  BackendPageViewModel,
  ControlRequestViewModel,
} from "./backendModels";
import {
  backendRequestReadErrorMessage,
  backendStatusErrorMessage,
  backendSubmissionUnknownMessage,
  classifyBackendSubmissionError,
  createGatewayControlIntent,
  loadBackendControlRequest,
  loadBackendPage,
  submitGatewayControl,
  type GatewayControlIntent,
} from "./backendService";

const STATUS_VISIBLE_INTERVAL_MS = 4_000;
const STATUS_HIDDEN_INTERVAL_MS = 15_000;
const REQUEST_VISIBLE_INTERVAL_MS = 1_500;
const REQUEST_HIDDEN_INTERVAL_MS = 5_000;
const MAX_BACKOFF_MULTIPLIER = 4;

type InternalOperationState =
  | { phase: "idle" }
  | {
      phase: "confirming";
      confirmation: BackendActionConfirmationViewModel;
    }
  | {
      phase: "awaiting_control_token";
      intent: GatewayControlIntent;
      retryingUnknownSubmission: boolean;
    }
  | { phase: "submitting"; intent: GatewayControlIntent }
  | {
      phase: "submission_unknown";
      intent: GatewayControlIntent;
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
      message: string;
      request: ControlRequestViewModel | null;
    };

export function useBackendController() {
  const { client } = useAuth();
  const [pageState, setPageState] = useState<BackendPageState>({
    phase: "loading",
  });
  const [operationState, setOperationState] = useState<InternalOperationState>({
    phase: "idle",
  });
  const operationRef = useRef<InternalOperationState>(operationState);
  const mountedRef = useRef(false);
  const submissionInFlightRef = useRef(false);
  const statusRefreshRef = useRef<() => void>(() => undefined);

  const setOperation = useCallback((next: InternalOperationState): void => {
    operationRef.current = next;
    setOperationState(next);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    let pendingRefresh = false;
    let failures = 0;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const clearTimer = (): void => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const schedule = (): void => {
      clearTimer();
      if (!active) {
        return;
      }
      const baseInterval = document.hidden
        ? STATUS_HIDDEN_INTERVAL_MS
        : STATUS_VISIBLE_INTERVAL_MS;
      const multiplier = Math.min(
        2 ** failures,
        MAX_BACKOFF_MULTIPLIER,
      );
      timer = window.setTimeout(() => {
        void run();
      }, baseInterval * multiplier);
    };

    const run = async (): Promise<void> => {
      if (!active) {
        return;
      }
      if (inFlight) {
        pendingRefresh = true;
        return;
      }
      clearTimer();
      inFlight = true;
      controller = new AbortController();
      const currentController = controller;
      try {
        const view = await loadBackendPage(client, currentController.signal);
        if (!active || currentController.signal.aborted) {
          return;
        }
        failures = 0;
        setPageState({ phase: "ready", view, statusError: null });
      } catch (error: unknown) {
        if (!active || currentController.signal.aborted) {
          return;
        }
        failures += 1;
        const message = backendStatusErrorMessage(error);
        setPageState((current) =>
          current.phase === "ready"
            ? { ...current, statusError: message }
            : { phase: "error", message },
        );
      } finally {
        if (controller === currentController) {
          controller = null;
        }
        inFlight = false;
        if (!active) {
          return;
        }
        if (pendingRefresh) {
          pendingRefresh = false;
          window.queueMicrotask(() => {
            void run();
          });
          return;
        }
        schedule();
      }
    };

    const refresh = (): void => {
      if (!active) {
        return;
      }
      if (inFlight) {
        pendingRefresh = true;
        return;
      }
      void run();
    };
    statusRefreshRef.current = refresh;

    const handleVisibilityChange = (): void => {
      clearTimer();
      if (document.hidden) {
        schedule();
      } else {
        refresh();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    void run();

    return () => {
      active = false;
      statusRefreshRef.current = () => undefined;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearTimer();
      controller?.abort();
      controller = null;
    };
  }, [client]);

  const refreshStatus = useCallback((): void => {
    statusRefreshRef.current();
  }, []);

  const trackingRequestId =
    operationState.phase === "tracking"
      ? operationState.request.requestId
      : null;
  const trackingAction =
    operationState.phase === "tracking"
      ? operationState.request.action
      : null;

  useEffect(() => {
    if (trackingRequestId === null || trackingAction === null) {
      return;
    }
    let active = true;
    let inFlight = false;
    let pendingRefresh = false;
    let failures = 0;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const clearTimer = (): void => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const schedule = (): void => {
      clearTimer();
      if (!active) {
        return;
      }
      const baseInterval = document.hidden
        ? REQUEST_HIDDEN_INTERVAL_MS
        : REQUEST_VISIBLE_INTERVAL_MS;
      const multiplier = Math.min(
        2 ** failures,
        MAX_BACKOFF_MULTIPLIER,
      );
      timer = window.setTimeout(() => {
        void run();
      }, baseInterval * multiplier);
    };

    const run = async (): Promise<void> => {
      if (!active) {
        return;
      }
      if (inFlight) {
        pendingRefresh = true;
        return;
      }
      clearTimer();
      inFlight = true;
      controller = new AbortController();
      const currentController = controller;
      try {
        const request = await loadBackendControlRequest(
          client,
          trackingRequestId,
          trackingAction,
          currentController.signal,
        );
        if (!active || currentController.signal.aborted) {
          return;
        }
        failures = 0;
        if (request.terminal) {
          setOperation(terminalOperation(request));
          refreshStatus();
          return;
        }
        const current = operationRef.current;
        if (
          current.phase === "tracking" &&
          current.request.requestId === trackingRequestId
        ) {
          setOperation({ ...current, request, queryError: null });
        }
      } catch (error: unknown) {
        if (!active || currentController.signal.aborted) {
          return;
        }
        failures += 1;
        const current = operationRef.current;
        if (
          current.phase === "tracking" &&
          current.request.requestId === trackingRequestId
        ) {
          setOperation({
            ...current,
            queryError: backendRequestReadErrorMessage(error),
          });
        }
      } finally {
        if (controller === currentController) {
          controller = null;
        }
        inFlight = false;
        if (!active || operationRef.current.phase !== "tracking") {
          return;
        }
        if (pendingRefresh) {
          pendingRefresh = false;
          window.queueMicrotask(() => {
            void run();
          });
          return;
        }
        schedule();
      }
    };

    const refresh = (): void => {
      if (inFlight) {
        pendingRefresh = true;
        return;
      }
      void run();
    };
    const handleVisibilityChange = (): void => {
      clearTimer();
      if (document.hidden) {
        schedule();
      } else {
        refresh();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    void run();

    return () => {
      active = false;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearTimer();
      controller?.abort();
      controller = null;
    };
  }, [client, refreshStatus, setOperation, trackingAction, trackingRequestId]);

  const displayedView = useMemo<BackendPageViewModel | null>(() => {
    if (pageState.phase !== "ready") {
      return null;
    }
    if (pageState.statusError !== null) {
      return lockBackendControls(
        pageState.view,
        "Backend Status 暂时不可用，控制操作已禁用。",
      );
    }
    if (operationState.phase !== "idle") {
      return lockBackendControls(
        pageState.view,
        operationControlLockReason(operationState),
      );
    }
    return pageState.view;
  }, [operationState, pageState]);

  const operation = useMemo(
    () => operationViewModel(operationState),
    [operationState],
  );

  const beginAction = useCallback(
    (action: BackendControlAction): void => {
      if (displayedView === null || operationRef.current.phase !== "idle") {
        return;
      }
      const availability = displayedView.controls[action];
      if (!availability.enabled) {
        return;
      }
      setOperation({
        phase: "confirming",
        confirmation: buildActionConfirmation(action, displayedView),
      });
    },
    [displayedView, setOperation],
  );

  const cancelConfirmation = useCallback((): void => {
    if (operationRef.current.phase === "confirming") {
      setOperation({ phase: "idle" });
    }
  }, [setOperation]);

  const confirmAction = useCallback((): void => {
    const current = operationRef.current;
    if (current.phase !== "confirming") {
      return;
    }
    try {
      setOperation({
        phase: "awaiting_control_token",
        intent: createGatewayControlIntent(current.confirmation.action),
        retryingUnknownSubmission: false,
      });
    } catch {
      setOperation({
        phase: "failed",
        action: current.confirmation.action,
        message: "浏览器无法生成安全的控制请求标识，操作未提交。",
        request: null,
      });
    }
  }, [setOperation]);

  const closeControlDialog = useCallback((): void => {
    if (
      submissionInFlightRef.current ||
      operationRef.current.phase === "submitting"
    ) {
      return;
    }
    const current = operationRef.current;
    if (current.phase !== "awaiting_control_token") {
      return;
    }
    if (current.retryingUnknownSubmission) {
      setOperation({
        phase: "submission_unknown",
        intent: current.intent,
        message: backendSubmissionUnknownMessage(),
      });
    } else {
      setOperation({ phase: "idle" });
    }
  }, [setOperation]);

  const submitControlToken = useCallback(
    async (controlToken: string): Promise<"success" | "invalid" | "failed"> => {
      const current = operationRef.current;
      if (
        current.phase !== "awaiting_control_token" ||
        submissionInFlightRef.current
      ) {
        return "failed";
      }
      const intent = current.intent;
      submissionInFlightRef.current = true;
      setOperation({ phase: "submitting", intent });
      try {
        const accepted = await submitGatewayControl(
          intent.action,
          controlToken,
          intent.idempotencyKey,
        );
        if (!mountedRef.current) {
          return "failed";
        }
        setOperation({
          phase: "tracking",
          request: mapAcceptedControlRequest(accepted),
          queryError: null,
        });
        return "success";
      } catch (error: unknown) {
        if (!mountedRef.current) {
          return "failed";
        }
        const failure = classifyBackendSubmissionError(error);
        if (failure.kind === "invalid_control_token") {
          setOperation({
            phase: "awaiting_control_token",
            intent,
            retryingUnknownSubmission: current.retryingUnknownSubmission,
          });
          return "invalid";
        }
        if (failure.kind === "submission_unknown") {
          setOperation({
            phase: "submission_unknown",
            intent,
            message: failure.message,
          });
        } else {
          setOperation({
            phase: "failed",
            action: intent.action,
            message: failure.message,
            request: null,
          });
        }
        return "failed";
      } finally {
        submissionInFlightRef.current = false;
      }
    },
    [setOperation],
  );

  const retryUnknownSubmission = useCallback((): void => {
    const current = operationRef.current;
    if (current.phase !== "submission_unknown") {
      return;
    }
    setOperation({
      phase: "awaiting_control_token",
      intent: current.intent,
      retryingUnknownSubmission: true,
    });
  }, [setOperation]);

  const dismissOperation = useCallback((): void => {
    const current = operationRef.current;
    if (current.phase !== "completed" && current.phase !== "failed") {
      return;
    }
    setPageState((state) =>
      state.phase === "ready"
        ? {
            ...state,
            statusError: "正在重新同步 Backend Status…",
          }
        : state,
    );
    setOperation({ phase: "idle" });
    refreshStatus();
  }, [refreshStatus, setOperation]);

  return {
    pageState,
    displayedView,
    operation,
    refreshStatus,
    beginAction,
    cancelConfirmation,
    confirmAction,
    closeControlDialog,
    submitControlToken,
    retryUnknownSubmission,
    dismissOperation,
  };
}

function terminalOperation(
  request: ControlRequestViewModel,
): InternalOperationState {
  if (request.status === "succeeded") {
    return { phase: "completed", request };
  }
  return {
    phase: "failed",
    action: request.action,
    message:
      request.status === "rejected"
        ? "控制请求被 Supervisor 拒绝。"
        : request.resultLabel ?? "Gateway 控制操作失败。",
    request,
  };
}

function operationControlLockReason(operation: InternalOperationState): string {
  switch (operation.phase) {
    case "idle":
      return "";
    case "confirming":
      return "正在确认控制意图，暂不能提交其他操作。";
    case "awaiting_control_token":
      return "正在等待本次控制授权，暂不能提交其他操作。";
    case "submitting":
      return "控制请求正在提交，必须等待明确结果。";
    case "submission_unknown":
      return "控制请求结果尚未确认，不能提交新的控制意图。";
    case "tracking":
      return "已有控制请求正在跟踪，完成前不能提交新操作。";
    case "completed":
    case "failed":
      return "请先确认本次操作结果并重新同步 Backend Status。";
  }
}

function operationViewModel(
  operation: InternalOperationState,
): BackendOperationViewModel {
  switch (operation.phase) {
    case "idle":
    case "tracking":
    case "completed":
      return operation;
    case "confirming":
      return {
        phase: operation.phase,
        confirmation: operation.confirmation,
      };
    case "awaiting_control_token":
      return {
        phase: operation.phase,
        action: operation.intent.action,
        actionLabel: actionLabel(operation.intent.action),
      };
    case "submitting":
      return {
        phase: operation.phase,
        action: operation.intent.action,
        actionLabel: actionLabel(operation.intent.action),
      };
    case "submission_unknown":
      return {
        phase: operation.phase,
        action: operation.intent.action,
        actionLabel: actionLabel(operation.intent.action),
        message: operation.message,
      };
    case "failed":
      return {
        ...operation,
        actionLabel:
          operation.action === null ? null : actionLabel(operation.action),
      };
  }
}
