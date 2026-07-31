export function formatUtcDateTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "未知时间";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).format(timestamp);
}

export function formatUnixWindow(startedAt: number, endedAt: number): string {
  return `${formatUtcDateTime(new Date(startedAt * 1000).toISOString())} – ${formatUtcDateTime(new Date(endedAt * 1000).toISOString())}`;
}

export function formatPercentage(value: number | null): string {
  return value === null ? "暂无数据" : `${(value * 100).toFixed(1)}%`;
}

export function formatDuration(value: number | null): string {
  if (value === null) {
    return "暂无数据";
  }
  if (value < 1000) {
    return `${value.toFixed(0)} ms`;
  }
  return `${(value / 1000).toFixed(2)} s`;
}

export function formatHeartbeatAge(seconds: number): string {
  if (seconds < 60) {
    return `${Math.round(seconds)} 秒前`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)} 分钟前`;
  }
  return `${(seconds / 3600).toFixed(1)} 小时前`;
}

export function formatBytes(value: number | null): string {
  if (value === null) {
    return "暂无数据";
  }
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 ? 0 : 1;
  return `${amount.toFixed(digits)} ${units[unitIndex]}`;
}
