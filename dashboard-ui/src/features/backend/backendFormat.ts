export function formatBackendTimestamp(value: string | null): string {
  if (value === null) {
    return "暂无";
  }
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

export function formatBackendFlag(value: boolean | null): string {
  if (value === null) {
    return "状态不可用";
  }
  return value ? "是" : "否";
}
