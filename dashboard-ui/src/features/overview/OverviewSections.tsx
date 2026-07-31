import { AsyncContent } from "../../components/AsyncContent";
import { MetricCard } from "../../components/MetricCard";
import {
  StatusBadge,
  statusLabel,
} from "../../components/StatusBadge";
import type { PollingQueryState } from "../../hooks/usePollingQuery";
import {
  formatBytes,
  formatDuration,
  formatHeartbeatAge,
  formatPercentage,
  formatUnixWindow,
  formatUtcDateTime,
} from "./format";
import type {
  BackendStatusViewModel,
  DashboardStatusViewModel,
  DatabaseHealthViewModel,
  MonitoringOverviewViewModel,
  RuntimeComponentsViewModel,
  ToolStatsViewModel,
} from "./overviewModels";

interface SystemStatusSectionProps {
  dashboard: PollingQueryState<DashboardStatusViewModel>;
  backend: PollingQueryState<BackendStatusViewModel>;
}

export function SystemStatusSection({
  dashboard,
  backend,
}: SystemStatusSectionProps) {
  return (
    <section className="dashboard-section" aria-labelledby="system-title">
      <SectionHeading
        id="system-title"
        eyebrow="SYSTEM"
        title="系统状态"
        description="Dashboard、Gateway lease 与 Supervisor 的独立只读快照。"
      />
      <div className="two-column-grid">
        <article className="content-card">
          <h3>Dashboard 与 Gateway lease</h3>
          <AsyncContent state={dashboard} emptyMessage="暂无服务状态。">
            {(data) => (
              <dl className="status-list">
                <StatusRow
                  label="Dashboard"
                  value={<StatusBadge status={data.webStatus} />}
                />
                <StatusRow
                  label="Gateway lease"
                  value={<StatusBadge status={data.gatewayLeaseStatus} />}
                />
                <StatusRow label="应用" value={data.applicationName} />
                <StatusRow label="版本" value={data.projectVersion} />
                <StatusRow
                  label="服务时间"
                  value={formatUtcDateTime(data.currentTime)}
                />
              </dl>
            )}
          </AsyncContent>
        </article>
        <article className="content-card">
          <h3>Gateway Supervisor</h3>
          <AsyncContent state={backend} emptyMessage="暂无后端状态。">
            {(data) => (
              <dl className="status-list">
                <StatusRow
                  label="Supervisor"
                  value={<StatusBadge status={data.supervisorStatus} />}
                />
                <StatusRow
                  label="Gateway 进程"
                  value={<StatusBadge status={data.gatewayStatus} />}
                />
                <StatusRow
                  label="管理关系"
                  value={<StatusBadge status={data.ownershipStatus} />}
                />
                <StatusRow
                  label="运行 lease"
                  value={<StatusBadge status={data.leaseStatus} />}
                />
                <StatusRow
                  label="重启建议"
                  value={
                    <StatusBadge
                      status={
                        data.restartRecommended === true
                          ? "degraded"
                          : data.restartRecommended === false
                            ? "healthy"
                            : "unknown"
                      }
                      label={
                        data.restartRecommended === true
                          ? "建议重启"
                          : data.restartRecommended === false
                            ? "无需重启"
                            : "尚无结论"
                      }
                    />
                  }
                />
                <StatusRow
                  label="观测时间"
                  value={formatUtcDateTime(data.observedAt)}
                />
              </dl>
            )}
          </AsyncContent>
        </article>
      </div>
    </section>
  );
}

interface RuntimeSectionProps {
  state: PollingQueryState<RuntimeComponentsViewModel>;
}

export function RuntimeSection({ state }: RuntimeSectionProps) {
  return (
    <section className="dashboard-section" aria-labelledby="runtime-title">
      <SectionHeading
        id="runtime-title"
        eyebrow="RUNTIME"
        title="运行组件"
        description="有效状态由上报状态和心跳新鲜度推导；禁用不等同于正在运行。"
      />
      <article className="content-card table-card">
        <AsyncContent state={state} emptyMessage="当前没有 Runtime Component 快照。">
          {(data) => (
            <>
              <div className="card-toolbar">
                <span>观测于 {formatUtcDateTime(data.observedAt)}</span>
                {data.truncated ? <span>仅展示前 100 项</span> : null}
              </div>
              <div className="table-scroll">
                <table>
                  <caption className="sr-only">Runtime Component 当前状态</caption>
                  <thead>
                    <tr>
                      <th scope="col">组件</th>
                      <th scope="col">有效状态</th>
                      <th scope="col">上报 / 新鲜度</th>
                      <th scope="col">心跳</th>
                      <th scope="col">阶段与容量</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((item) => (
                      <tr key={item.key}>
                        <td>
                          <strong>{item.componentType}</strong>
                          <span className="cell-detail">{item.componentId}</span>
                        </td>
                        <td>
                          {item.enabled === false ? (
                            <div className="stacked-status">
                              <StatusBadge status="disabled" />
                              <span>
                                基础设施 {statusLabel(item.effectiveStatus)}
                              </span>
                            </div>
                          ) : (
                            <div className="stacked-status">
                              <StatusBadge status={item.effectiveStatus} />
                              <span>
                                {item.enabled === true
                                  ? "已启用"
                                  : "启用状态未上报"}
                              </span>
                            </div>
                          )}
                        </td>
                        <td>
                          <div className="inline-badges">
                            <StatusBadge status={item.reportedStatus} />
                            <StatusBadge status={item.freshnessStatus} />
                          </div>
                        </td>
                        <td>{formatHeartbeatAge(item.heartbeatAgeSeconds)}</td>
                        <td>
                          <span>{item.phase ?? "未提供阶段"}</span>
                          <span className="cell-detail">
                            {capacitySummary(
                              item.workerCount,
                              item.queueDepth,
                            )}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </AsyncContent>
      </article>
    </section>
  );
}

interface MonitoringSectionProps {
  state: PollingQueryState<MonitoringOverviewViewModel>;
}

export function MonitoringSection({ state }: MonitoringSectionProps) {
  return (
    <section className="dashboard-section" aria-labelledby="monitoring-title">
      <SectionHeading
        id="monitoring-title"
        eyebrow="OBSERVABILITY"
        title="过去 24 小时"
        description="基于安全 Observation 与 Tool Execution 投影的有限聚合。"
      />
      <AsyncContent
        state={state}
        emptyMessage="该时间窗口内没有运行、模型或工具调用数据；成功率不会被推断为 0%。"
      >
        {(data) => (
          <>
            <p className="window-caption">
              {formatUnixWindow(data.windowStartedAt, data.windowEndedAt)}
            </p>
            <div className="metric-grid">
              <MetricCard label="Run 数量" value={String(data.runCount)} />
              <MetricCard
                label="Run 成功率"
                value={formatPercentage(data.runSuccessRate)}
                detail={data.runSuccessRate === null ? "无终态样本" : undefined}
              />
              <MetricCard
                label="模型调用"
                value={String(data.modelCallCount)}
              />
              <MetricCard label="工具调用" value={String(data.toolCallCount)} />
              <MetricCard
                label="工具失败"
                value={String(data.toolFailureCount)}
              />
              <MetricCard
                label="平均调用耗时"
                value={formatDuration(data.averageDurationMs)}
              />
              <MetricCard
                label="Token 覆盖率"
                value={formatPercentage(data.tokenCoverageRate)}
                detail={
                  data.totalTokens === null
                    ? "Token 总量不可用"
                    : `已记录 ${data.totalTokens.toLocaleString("zh-CN")} tokens`
                }
              />
            </div>
          </>
        )}
      </AsyncContent>
    </section>
  );
}

interface ToolStatsSectionProps {
  state: PollingQueryState<ToolStatsViewModel>;
}

export function ToolStatsSection({ state }: ToolStatsSectionProps) {
  return (
    <section className="dashboard-section" aria-labelledby="tools-title">
      <SectionHeading
        id="tools-title"
        eyebrow="TOOLS"
        title="工具调用摘要"
        description="按后端固定排序展示过去 24 小时的前八项安全统计。"
      />
      <article className="content-card table-card">
        <AsyncContent state={state} emptyMessage="该时间窗口内没有工具调用。">
          {(data) => (
            <div className="table-scroll">
              <table>
                <caption className="sr-only">工具调用聚合摘要</caption>
                <thead>
                  <tr>
                    <th scope="col">工具</th>
                    <th scope="col">调用数</th>
                    <th scope="col">失败数</th>
                    <th scope="col">成功率</th>
                    <th scope="col">平均耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((item) => (
                    <tr key={item.toolName}>
                      <td>
                        <strong>{item.toolName}</strong>
                      </td>
                      <td>{item.callCount}</td>
                      <td>{item.failureCount}</td>
                      <td>{formatPercentage(item.successRate)}</td>
                      <td>{formatDuration(item.averageDurationMs)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </AsyncContent>
      </article>
    </section>
  );
}

interface DatabaseSectionProps {
  state: PollingQueryState<DatabaseHealthViewModel>;
}

export function DatabaseSection({ state }: DatabaseSectionProps) {
  return (
    <section className="dashboard-section" aria-labelledby="database-title">
      <SectionHeading
        id="database-title"
        eyebrow="DATABASE"
        title="数据库诊断"
        description="按需执行固定只读探针，不展示路径、表名、SQL 或底层错误。"
      />
      <article className="content-card">
        <AsyncContent state={state} emptyMessage="暂无数据库诊断快照。">
          {(data) => (
            <>
              <div className="database-summary">
                <MetricCard
                  label="整体状态"
                  value={statusLabel(data.overallStatus)}
                  detail={formatUtcDateTime(data.checkedAt)}
                />
                <MetricCard
                  label="Schema"
                  value={statusLabel(data.schemaStatus)}
                  detail={`当前 / 预期：${data.schemaVersion}`}
                />
                <MetricCard label="Journal mode" value={data.journalMode} />
                <MetricCard
                  label="已用空间"
                  value={formatBytes(data.usedSpaceBytes)}
                />
              </div>
              <div className="database-flags">
                <StatusBadge
                  status={
                    data.requiredStructuresAvailable ? "healthy" : "degraded"
                  }
                  label={
                    data.requiredStructuresAvailable
                      ? "关键结构完整"
                      : "关键结构不完整"
                  }
                />
                <StatusBadge
                  status={data.budgetExhausted ? "degraded" : "healthy"}
                  label={
                    data.budgetExhausted
                      ? "诊断预算已耗尽"
                      : `探针总耗时 ${formatDuration(data.probeDurationMs)}`
                  }
                />
              </div>
              <div className="probe-grid">
                {data.probes.map((probe) => (
                  <div className="probe-item" key={probe.name}>
                    <div>
                      <strong>{probe.name}</strong>
                      <span>{formatDuration(probe.durationMs)}</span>
                    </div>
                    <StatusBadge status={probe.status} />
                  </div>
                ))}
              </div>
            </>
          )}
        </AsyncContent>
      </article>
    </section>
  );
}

interface SectionHeadingProps {
  id: string;
  eyebrow: string;
  title: string;
  description: string;
}

function SectionHeading({
  id,
  eyebrow,
  title,
  description,
}: SectionHeadingProps) {
  return (
    <header className="section-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2 id={id}>{title}</h2>
      </div>
      <p>{description}</p>
    </header>
  );
}

interface StatusRowProps {
  label: string;
  value: React.ReactNode;
}

function StatusRow({ label, value }: StatusRowProps) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function capacitySummary(
  workerCount: number | null,
  queueDepth: number | null,
): string {
  const parts: string[] = [];
  if (workerCount !== null) {
    parts.push(`Worker ${workerCount}`);
  }
  if (queueDepth !== null) {
    parts.push(`Queue ${queueDepth}`);
  }
  return parts.length === 0 ? "无容量摘要" : parts.join(" · ");
}
