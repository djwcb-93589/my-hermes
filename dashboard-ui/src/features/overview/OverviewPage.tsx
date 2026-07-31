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
  const { client } = useAuth();
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
    <>
      <header className="hero">
        <div>
          <p className="eyebrow">GLOBAL OVERVIEW</p>
          <h1>运行态势，一页掌握</h1>
          <p>
            各数据源独立刷新。单个区域不可用不会阻断其他只读监控信息。
          </p>
        </div>
        <div className="hero-actions">
          <div className="read-only-chip">READ ONLY</div>
          <button type="button" className="secondary-button" onClick={refreshAll}>
            刷新全部
          </button>
        </div>
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
    </>
  );
}
