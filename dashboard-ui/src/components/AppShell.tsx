import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

export function AppShell() {
  const { clearReadToken, state } = useAuth();
  const location = useLocation();
  const rootShowsOverview = location.pathname === "/";
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="app-header">
        <div className="brand-lockup">
          <div className="brand-mark compact" aria-hidden="true">
            H
          </div>
          <div>
            <strong>MyHermes</strong>
            <span>运行监控与安全配置</span>
          </div>
        </div>
        <div className="header-navigation">
          <nav aria-label="Dashboard 页面" className="page-navigation">
            <NavLink
              to="/overview"
              className={({ isActive }) =>
                isActive || rootShowsOverview ? "active" : undefined
              }
              aria-current={rootShowsOverview ? "page" : undefined}
            >
              Overview
            </NavLink>
            <NavLink to="/config">Configuration</NavLink>
          </nav>
          {state === "authenticated_with_read_token" ? (
            <button
              type="button"
              className="text-button"
              onClick={clearReadToken}
            >
              清除 Read Token
            </button>
          ) : null}
        </div>
      </header>
      <main id="main-content" className="main-content">
        <Outlet />
      </main>
      <footer className="app-footer">
        仅展示后端安全投影 · 不包含 Prompt、工具参数、结果或凭证
      </footer>
    </div>
  );
}
