import type {
  MonitoringOverviewResponseDto,
  ToolStatsResponseDto,
} from "./dto";
import type { HttpClient } from "./http";

export interface MonitoringWindowQuery {
  startedAt: number;
  endedAt: number;
}

function windowQuery(query: MonitoringWindowQuery): string {
  const parameters = new URLSearchParams({
    started_at: String(query.startedAt),
    ended_at: String(query.endedAt),
  });
  return parameters.toString();
}

export function readMonitoringOverview(
  client: HttpClient,
  query: MonitoringWindowQuery,
  signal?: AbortSignal,
): Promise<MonitoringOverviewResponseDto> {
  return client.get<MonitoringOverviewResponseDto>(
    `/api/monitoring/overview?${windowQuery(query)}`,
    { signal },
  );
}

export function readToolStats(
  client: HttpClient,
  query: MonitoringWindowQuery,
  signal?: AbortSignal,
): Promise<ToolStatsResponseDto> {
  return client.get<ToolStatsResponseDto>(
    `/api/monitoring/tools/stats?${windowQuery(query)}`,
    { signal },
  );
}
