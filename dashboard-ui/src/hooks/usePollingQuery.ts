import { useCallback, useEffect, useRef, useState } from "react";

export type QueryPhase = "idle" | "loading" | "ready" | "empty" | "error";

export interface PollingQueryState<T> {
  phase: QueryPhase;
  data: T | null;
  refreshedAt: number | null;
  refresh: () => void;
}

interface PollingQueryOptions<T> {
  enabled: boolean;
  intervalMs: number;
  query: (signal: AbortSignal) => Promise<T>;
  isEmpty?: (data: T) => boolean;
}

interface QuerySnapshot<T> {
  phase: QueryPhase;
  data: T | null;
  refreshedAt: number | null;
}

const INITIAL_SNAPSHOT: QuerySnapshot<never> = {
  phase: "idle",
  data: null,
  refreshedAt: null,
};

export function usePollingQuery<T>({
  enabled,
  intervalMs,
  query,
  isEmpty,
}: PollingQueryOptions<T>): PollingQueryState<T> {
  const [snapshot, setSnapshot] = useState<QuerySnapshot<T>>(
    INITIAL_SNAPSHOT,
  );
  const queryRef = useRef(query);
  const isEmptyRef = useRef(isEmpty);
  const enabledRef = useRef(enabled);
  const mountedRef = useRef(false);
  const inFlightRef = useRef(false);
  const pendingRefreshRef = useRef(false);
  const failuresRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const runRef = useRef<() => void>(() => undefined);

  queryRef.current = query;
  isEmptyRef.current = isEmpty;
  enabledRef.current = enabled;

  const clearTimer = useCallback((): void => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const schedule = useCallback(
    (delayMs: number): void => {
      clearTimer();
      if (!enabledRef.current || document.hidden) {
        return;
      }
      timerRef.current = window.setTimeout(() => runRef.current(), delayMs);
    },
    [clearTimer],
  );

  const run = useCallback(async (): Promise<void> => {
    if (!enabledRef.current || document.hidden) {
      return;
    }
    if (inFlightRef.current) {
      pendingRefreshRef.current = true;
      return;
    }

    clearTimer();
    inFlightRef.current = true;
    const controller = new AbortController();
    controllerRef.current = controller;
    setSnapshot((current) => ({
      ...current,
      phase: current.data === null ? "loading" : current.phase,
    }));

    try {
      const data = await queryRef.current(controller.signal);
      if (!mountedRef.current || controller.signal.aborted) {
        return;
      }
      failuresRef.current = 0;
      setSnapshot({
        phase: isEmptyRef.current?.(data) === true ? "empty" : "ready",
        data,
        refreshedAt: Date.now(),
      });
    } catch {
      if (mountedRef.current && !controller.signal.aborted) {
        failuresRef.current += 1;
        setSnapshot((current) => ({ ...current, phase: "error" }));
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
      }
      inFlightRef.current = false;
      if (!mountedRef.current || !enabledRef.current || document.hidden) {
        return;
      }
      if (pendingRefreshRef.current) {
        pendingRefreshRef.current = false;
        window.queueMicrotask(() => runRef.current());
        return;
      }
      const backoffMultiplier = Math.min(2 ** failuresRef.current, 4);
      schedule(intervalMs * backoffMultiplier);
    }
  }, [clearTimer, intervalMs, schedule]);

  runRef.current = () => {
    void run();
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearTimer();
      controllerRef.current?.abort();
      controllerRef.current = null;
    };
  }, [clearTimer]);

  useEffect(() => {
    if (enabled) {
      runRef.current();
      return;
    }
    clearTimer();
    pendingRefreshRef.current = false;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setSnapshot(INITIAL_SNAPSHOT);
  }, [clearTimer, enabled]);

  useEffect(() => {
    const handleVisibilityChange = (): void => {
      if (document.hidden) {
        clearTimer();
        pendingRefreshRef.current = false;
        controllerRef.current?.abort();
        return;
      }
      if (enabledRef.current) {
        runRef.current();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [clearTimer]);

  const refresh = useCallback((): void => {
    if (!enabledRef.current) {
      return;
    }
    if (inFlightRef.current) {
      pendingRefreshRef.current = true;
      return;
    }
    runRef.current();
  }, []);

  return { ...snapshot, refresh };
}
