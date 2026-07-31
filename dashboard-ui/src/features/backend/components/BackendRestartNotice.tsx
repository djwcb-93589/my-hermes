interface BackendRestartNoticeProps {
  visible: boolean;
}

export function BackendRestartNotice({ visible }: BackendRestartNoticeProps) {
  if (!visible) {
    return null;
  }
  return (
    <aside className="restart-notice backend-restart-notice">
      <div>
        <strong>建议重启 Gateway 以应用新配置</strong>
        <span>
          配置文件在当前 Gateway 启动后发生了变化。Dashboard 不会自动重启，请在确认影响后手动执行 Restart。
        </span>
      </div>
    </aside>
  );
}
