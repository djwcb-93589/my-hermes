import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// 独立测试配置：不改变生产 vite.config.ts 的构建语义。
// 测试文件统一放在 tests_dashboard/frontend/（仓库根），不从生产源码目录散落，
// 因此 root 指到仓库根，保证 vite server 能加载测试文件。
// 注意：vitest 4 的 worker 按 cwd 解析 root 相对路径，需在仓库根目录运行：
//   dashboard-ui/node_modules/.bin/vitest run --config dashboard-ui/vitest.config.ts
const repoRoot = fileURLToPath(new URL("../", import.meta.url));
// 测试文件位于 tests_dashboard/，不在 dashboard-ui 的 node_modules 解析链上，
// 将 @testing-library/* 直接映射到 dashboard-ui/node_modules 下的实际包。
const frontendNodeModules = fileURLToPath(
  new URL("../dashboard-ui/node_modules/", import.meta.url),
);

export default defineConfig({
  root: repoRoot,
  resolve: {
    alias: [
      // 字符串 find 只匹配精确名或 find + "/" 前缀，react 不会误伤 react-dom
      { find: "react", replacement: join(frontendNodeModules, "react") },
      { find: "react-dom", replacement: join(frontendNodeModules, "react-dom") },
      {
        find: "react-router-dom",
        replacement: join(frontendNodeModules, "react-router-dom"),
      },
      {
        find: "@testing-library/react",
        replacement: join(frontendNodeModules, "@testing-library/react"),
      },
      {
        find: "@testing-library/user-event",
        replacement: join(frontendNodeModules, "@testing-library/user-event"),
      },
      {
        find: "@testing-library/jest-dom",
        replacement: join(frontendNodeModules, "@testing-library/jest-dom"),
      },
    ],
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["dashboard-ui/src/test/setup.ts"],
    include: ["tests_dashboard/frontend/**/*.test.{ts,tsx}"],
    css: false,
  },
});
