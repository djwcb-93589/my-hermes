import type { ReactNode } from "react";

import type { PollingQueryState } from "../hooks/usePollingQuery";

interface AsyncContentProps<T> {
  state: PollingQueryState<T>;
  emptyMessage: string;
  children: (data: T) => ReactNode;
}

export function AsyncContent<T>({
  state,
  emptyMessage,
  children,
}: AsyncContentProps<T>) {
  if (state.phase === "idle" || state.phase === "loading") {
    return (
      <div className="panel-state" role="status">
        <span className="loading-pulse" aria-hidden="true" />
        正在读取安全快照…
      </div>
    );
  }
  if (state.phase === "error") {
    return (
      <div className="panel-state panel-error" role="status">
        <span>该数据源暂时不可用，将按受控退避策略重试。</span>
        <button type="button" className="text-button" onClick={state.refresh}>
          立即重试
        </button>
      </div>
    );
  }
  if (state.phase === "empty" || state.data === null) {
    return <div className="panel-state">{emptyMessage}</div>;
  }
  return children(state.data);
}
