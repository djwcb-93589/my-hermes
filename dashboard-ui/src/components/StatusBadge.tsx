export type StatusKind =
  | "busy"
  | "clock_skewed"
  | "compatible"
  | "degraded"
  | "disabled"
  | "failed"
  | "fresh"
  | "healthy"
  | "idle"
  | "incompatible"
  | "managed"
  | "offline"
  | "online"
  | "running"
  | "skipped"
  | "stale"
  | "starting"
  | "stopped"
  | "stopping"
  | "succeeded"
  | "terminal"
  | "unavailable"
  | "unknown"
  | "unmanaged";

type StatusTone = "positive" | "warning" | "danger" | "neutral" | "info";

const STATUS_PRESENTATION: Record<
  StatusKind,
  { label: string; tone: StatusTone }
> = {
  busy: { label: "繁忙", tone: "warning" },
  clock_skewed: { label: "时钟偏移", tone: "warning" },
  compatible: { label: "兼容", tone: "positive" },
  degraded: { label: "降级", tone: "warning" },
  disabled: { label: "已禁用", tone: "neutral" },
  failed: { label: "失败", tone: "danger" },
  fresh: { label: "新鲜", tone: "positive" },
  healthy: { label: "健康", tone: "positive" },
  idle: { label: "空闲", tone: "info" },
  incompatible: { label: "不兼容", tone: "danger" },
  managed: { label: "受管", tone: "positive" },
  offline: { label: "离线", tone: "neutral" },
  online: { label: "在线", tone: "positive" },
  running: { label: "运行中", tone: "positive" },
  skipped: { label: "已跳过", tone: "neutral" },
  stale: { label: "过期", tone: "warning" },
  starting: { label: "启动中", tone: "info" },
  stopped: { label: "已停止", tone: "neutral" },
  stopping: { label: "停止中", tone: "info" },
  succeeded: { label: "成功", tone: "positive" },
  terminal: { label: "终态", tone: "neutral" },
  unavailable: { label: "不可用", tone: "danger" },
  unknown: { label: "未知", tone: "neutral" },
  unmanaged: { label: "未受管", tone: "warning" },
};

interface StatusBadgeProps {
  status: StatusKind;
  label?: string;
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const presentation = STATUS_PRESENTATION[status];
  return (
    <span className={`status-badge status-${presentation.tone}`}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? presentation.label}
    </span>
  );
}

export function statusLabel(status: StatusKind): string {
  return STATUS_PRESENTATION[status].label;
}
