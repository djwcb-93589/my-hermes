import { type FormEvent, useRef, useState } from "react";

import { useAuth } from "./AuthContext";

export function TokenPrompt() {
  const { authenticateReadToken, isAuthenticating } = useAuth();
  const inputRef = useRef<HTMLInputElement>(null);
  const [message, setMessage] = useState<string | null>(null);
  const isBusy = isAuthenticating;

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const submittedToken = inputRef.current?.value ?? "";
    if (inputRef.current !== null) {
      inputRef.current.value = "";
    }
    setMessage(null);
    const result = await authenticateReadToken(submittedToken);
    if (result === "invalid") {
      setMessage("Token 无效，请重新输入有效的 Dashboard Read Token。");
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
          后端要求只读认证。输入独立的 Dashboard Read Token；它不能用于任何控制操作。
        </p>
        <form onSubmit={submit} className="auth-form">
          <label htmlFor="dashboard-read-token">Dashboard Read Token</label>
          <input
            id="dashboard-read-token"
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
            {isBusy ? "正在验证…" : "使用 Read Token 进入"}
          </button>
          <div id="auth-message" className="auth-message" aria-live="polite">
            {message}
          </div>
        </form>
      </section>
    </main>
  );
}
