import { useAuth } from "./AuthContext";

interface AuthenticationStatusProps {
  mode: "checking" | "unavailable";
}

export function AuthenticationStatus({ mode }: AuthenticationStatusProps) {
  const { retryAnonymousProbe } = useAuth();
  const isChecking = mode === "checking";
  return (
    <main className="auth-page">
      <section className="auth-panel" aria-labelledby="auth-status-title">
        <div className="brand-mark" aria-hidden="true">
          H
        </div>
        <p className="eyebrow">MYHERMES DASHBOARD</p>
        <h1 id="auth-status-title">
          {isChecking ? "正在检查只读访问" : "Dashboard 暂时不可用"}
        </h1>
        <p className="auth-intro" role="status">
          {isChecking
            ? "正在通过现有状态接口确认是否允许匿名 READ。"
            : "当前无法读取 Dashboard 状态。这不是 Token 错误，请稍后重试。"}
        </p>
        {isChecking ? (
          <div className="auth-progress" aria-hidden="true">
            <span className="loading-pulse" />
          </div>
        ) : (
          <button
            type="button"
            className="primary-button auth-retry"
            onClick={retryAnonymousProbe}
          >
            重新检查
          </button>
        )}
      </section>
    </main>
  );
}
