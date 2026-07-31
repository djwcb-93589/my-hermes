import { useCallback } from "react";

import { useAuth } from "../../auth/AuthContext";
import { usePollingQuery } from "../../hooks/usePollingQuery";
import {
  loadBackendStatus,
  loadDashboardStatus,
  loadDatabaseHealth,
  loadMonitoringOverview,
  loadRuntimeComponents,
  loadToolStats,
} from "./overviewService";
import {
  DatabaseSection,
  MonitoringSection,
  RuntimeSection,
  SystemStatusSection,
  ToolStatsSection,
} from "./OverviewSections";

const FAST_POLL_INTERVAL_MS = 5_000;
const SLOW_POLL_INTERVAL_MS = 30_000;

export function OverviewPage() {
  const { client, clearToken } = useAuth();
  const dashboardQuery = useCallback(
    (signal: AbortSignal) => loadDashboardStatus(client, signal),
    [client],
  );
  const backendQuery = useCallback(
    (signal: AbortSignal) => loadBackendStatus(client, signal),
    [client],
  );
  const runtimeQuery = useCallback(
    (signal: AbortSignal) => loadRuntimeComponents(client, signal),
    [client],
  );
  const monitoringQuery = useCallback(
    (signal: AbortSignal) => loadMonitoringOverview(client, signal),
    [client],
  );
  const toolStatsQuery = useCallback(
    (signal: AbortSignal) => loadToolStats(client, signal),
    [client],
  );
  const databaseQuery = useCallback(
    (signal: AbortSignal) => loadDatabaseHealth(client, signal),
    [client],
  );

  const dashboard = usePollingQuery({
    enabled: true,
    intervalMs: FAST_POLL_INTERVAL_MS,
    query: dashboardQuery,
  });
  const backend = usePollingQuery({
    enabled: true,
    intervalMs: FAST_POLL_INTERVAL_MS,
    query: backendQuery,
  });
  const runtime = usePollingQuery({
    enabled: true,
    intervalMs: FAST_POLL_INTERVAL_MS,
    query: runtimeQuery,
    isEmpty: (data) => data.items.length === 0,
  });
  const monitoring = usePollingQuery({
    enabled: true,
    intervalMs: SLOW_POLL_INTERVAL_MS,
    query: monitoringQuery,
    isEmpty: (data) => !data.hasData,
  });
  const toolStats = usePollingQuery({
    enabled: true,
    intervalMs: SLOW_POLL_INTERVAL_MS,
    query: toolStatsQuery,
    isEmpty: (data) => data.items.length === 0,
  });
  const database = usePollingQuery({
    enabled: true,
    intervalMs: SLOW_POLL_INTERVAL_MS,
    query: databaseQuery,
  });

  const refreshAll = (): void => {
    dashboard.refresh();
    backend.refresh();
    runtime.refresh();
    monitoring.refresh();
    toolStats.refresh();
    database.refresh();
  };

  const restartRecommended =
    backend.data?.configChanged === true &&
    backend.data.restartRecommended === true;

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="app-header">
        <div className="brand-lockup">
          <div className="brand-mark compact" aria-hidden="true">
            H
          </div>
          <div>
            <strong>MyHermes</strong>
            <span>只读运行总览</span>
          </div>
        </div>
        <nav aria-label="Dashboard 操作" className="header-actions">
          <button type="button" className="secondary-button" onClick={refreshAll}>
            刷新全部
          </button>
          <button type="button" className="text-button" onClick={clearToken}>
            释放 Token
          </button>
        </nav>
      </header>
      <main id="main-content" className="main-content">
        <header className="hero">
          <div>
            <p className="eyebrow">GLOBAL OVERVIEW</p>
            <h1>运行态势，一页掌握</h1>
            <p>
              各数据源独立刷新。单个区域不可用不会阻断其他只读监控信息。
            </p>
          </div>
          <div className="read-only-chip">READ ONLY</div>
        </header>

        {restartRecommended ? (
          <aside className="restart-notice" role="status">
            <strong>配置已修改，需要重启 Gateway 才能生效</strong>
            <span>
              当前页面仅提供只读提示；请在受控运维流程中执行重启。
            </span>
          </aside>
        ) : null}

        <SystemStatusSection dashboard={dashboard} backend={backend} />
        <RuntimeSection state={runtime} />
        <MonitoringSection state={monitoring} />
        <ToolStatsSection state={toolStats} />
        <DatabaseSection state={database} />
      </main>
      <footer className="app-footer">
        仅展示后端安全投影 · 不包含 Prompt、工具参数、结果或凭证
      </footer>
    </div>
  );
}
