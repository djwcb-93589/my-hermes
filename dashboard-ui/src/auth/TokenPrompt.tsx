import { type FormEvent, useRef, useState } from "react";

import { useAuth } from "./AuthContext";

export function TokenPrompt() {
  const { authenticate, state } = useAuth();
  const inputRef = useRef<HTMLInputElement>(null);
  const [message, setMessage] = useState<string | null>(null);
  const isBusy = state === "authenticating";

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const submittedToken = inputRef.current?.value ?? "";
    if (inputRef.current !== null) {
      inputRef.current.value = "";
    }
    setMessage(null);
    const result = await authenticate(submittedToken);
    if (result === "invalid") {
      setMessage("Token 无效，请重新输入有效的 Dashboard READ Token。");
    } else if (result === "unavailable") {
      setMessage("暂时无法验证 Token，请确认 Dashboard 服务可用后重试。");
    }
  };

  return (
    <main className="auth-page">
      <section className="auth-panel" aria-labelledby="auth-title">
        <div className="brand-mark" aria-hidden="true">
          H
        </div>
        <p className="eyebrow">MYHERMES DASHBOARD</p>
        <h1 id="auth-title">连接只读运行总览</h1>
        <p className="auth-intro">
          输入 Dashboard READ Token。Token 仅保存在当前页面内存中，刷新、关闭页面或认证失败后即释放。
        </p>
        <form onSubmit={submit} className="auth-form">
          <label htmlFor="dashboard-token">Dashboard READ Token</label>
          <input
            id="dashboard-token"
            ref={inputRef}
            type="password"
            minLength={32}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            disabled={isBusy}
            required
            aria-describedby="token-help auth-message"
          />
          <p id="token-help" className="field-help">
            Token 不会写入浏览器存储、Cookie、URL 或日志。
          </p>
          <button type="submit" className="primary-button" disabled={isBusy}>
            {isBusy ? "正在验证…" : "进入 Dashboard"}
          </button>
          <div id="auth-message" className="auth-message" aria-live="polite">
            {message}
          </div>
        </form>
      </section>
    </main>
  );
}
