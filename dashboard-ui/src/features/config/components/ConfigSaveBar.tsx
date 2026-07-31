import type { RefObject } from "react";

interface ConfigSaveBarProps {
  saveButtonRef: RefObject<HTMLButtonElement | null>;
  changeCount: number;
  hasErrors: boolean;
  busy: boolean;
  synchronizationRequired: boolean;
  onDiscard: () => void;
  onSave: () => void;
}

export function ConfigSaveBar({
  saveButtonRef,
  changeCount,
  hasErrors,
  busy,
  synchronizationRequired,
  onDiscard,
  onSave,
}: ConfigSaveBarProps) {
  return (
    <aside className="config-save-bar" aria-label="本地配置修改">
      <div>
        <strong>
          {hasErrors
            ? "本地草稿包含基础类型错误"
            : changeCount > 0
              ? `${changeCount} 个未保存修改`
              : "没有未保存修改"}
        </strong>
        <span>
          {synchronizationRequired
            ? "必须先重新读取最新安全快照，才能继续保存。"
            : "只会提交真正变化且允许写入的普通字段。"}
        </span>
      </div>
      <div className="save-bar-actions">
        <button
          type="button"
          className="text-button"
          disabled={busy || (changeCount === 0 && !hasErrors)}
          onClick={onDiscard}
        >
          放弃本地修改
        </button>
        <button
          ref={saveButtonRef}
          type="button"
          className="primary-button"
          disabled={
            busy ||
            hasErrors ||
            changeCount === 0 ||
            synchronizationRequired
          }
          onClick={onSave}
        >
          保存配置
        </button>
      </div>
    </aside>
  );
}
