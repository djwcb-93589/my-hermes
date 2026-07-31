import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "");
  const proxyTarget = environment.HERMES_DASHBOARD_DEV_PROXY;

  return {
    base: "/",
    build: {
      outDir: "../hermes/web/frontend_dist",
      emptyOutDir: true,
      sourcemap: false,
    },
    ...(proxyTarget
      ? {
          server: {
            proxy: {
              "/api": { target: proxyTarget, changeOrigin: false },
              "/healthz": { target: proxyTarget, changeOrigin: false },
            },
          },
        }
      : {}),
  };
});
