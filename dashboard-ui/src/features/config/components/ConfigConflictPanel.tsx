import type { ConfigConflictKind } from "../configModels";

interface ConfigConflictPanelProps {
  kind: ConfigConflictKind;
  onLoadLatest: () => void;
}

export function ConfigConflictPanel({
  kind,
  onLoadLatest,
}: ConfigConflictPanelProps) {
  const message =
    kind === "revision"
      ? "配置已被其他进程或页面修改"
      : kind === "shadowed"
        ? "配置字段当前由启动环境覆盖，不能通过配置文件修改"
        : "配置当前状态与本地安全快照冲突";
  return (
    <aside className="conflict-panel" role="alert">
      <div>
        <strong>{message}</strong>
        <span>不会自动重试或合并；当前本地草稿仍然保留。</span>
      </div>
      <button type="button" className="secondary-button" onClick={onLoadLatest}>
        加载最新配置
      </button>
    </aside>
  );
}

