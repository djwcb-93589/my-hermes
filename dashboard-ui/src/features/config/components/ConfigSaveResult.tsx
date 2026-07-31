import { Link } from "react-router-dom";

import type { ConfigSaveResultViewModel } from "../configModels";

interface ConfigSaveResultProps {
  result: ConfigSaveResultViewModel;
  synchronizing: boolean;
  synchronizationFailed: boolean;
  onReload: () => void;
}

export function ConfigSaveResult({
  result,
  synchronizing,
  synchronizationFailed,
  onReload,
}: ConfigSaveResultProps) {
  return (
    <aside className="save-result" aria-live="polite">
      <div>
        <p className="eyebrow">CONFIG SAVED</p>
        <h2>配置修改已提交</h2>
      </div>
      {result.restartRequired ? (
        <div className="restart-result config-restart-result">
          <span>配置已保存，需要重启 Gateway 才能生效</span>
          {result.restartTargets.some((target) => target.code === "gateway") ? (
            <Link className="secondary-button button-link" to="/backend">
              前往 Backend 页面
            </Link>
          ) : null}
        </div>
      ) : (
        <p>本次变更不需要执行重启操作。</p>
      )}
      <dl className="save-result-grid">
        <div>
          <dt>已修改字段</dt>
          <dd>{result.changedFields.join("、") || "无"}</dd>
        </div>
        <div>
          <dt>应用方式</dt>
          <dd>
            {result.applyModes.map((mode) => mode.label).join("、") || "无"}
          </dd>
        </div>
        <div>
          <dt>重启目标</dt>
          <dd>
            {result.restartTargets.map((target) => target.label).join("、") ||
              "无"}
          </dd>
        </div>
      </dl>
      {synchronizing ? (
        <p role="status">正在使用当前 READ 会话重新读取最新配置…</p>
      ) : null}
      {synchronizationFailed ? (
        <div className="inline-error" role="alert">
          <span>配置已保存，但最新安全快照读取失败。请手动重新加载。</span>
          <button type="button" className="text-button" onClick={onReload}>
            重新加载配置
          </button>
        </div>
      ) : null}
    </aside>
  );
}
