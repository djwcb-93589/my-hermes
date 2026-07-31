import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { AuthenticationStatus } from "./auth/AuthenticationStatus";
import { TokenPrompt } from "./auth/TokenPrompt";
import { AppShell } from "./components/AppShell";
import { BackendPage } from "./features/backend/BackendPage";
import { ConfigPage } from "./features/config/ConfigPage";
import { OverviewPage } from "./features/overview/OverviewPage";

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "overview", element: <OverviewPage /> },
      { path: "config", element: <ConfigPage /> },
      { path: "config/*", element: <ConfigPage /> },
      { path: "backend", element: <BackendPage /> },
      { path: "backend/*", element: <BackendPage /> },
    ],
  },
]);

function DashboardApplication() {
  const { state } = useAuth();
  switch (state) {
    case "checking_anonymous":
      return <AuthenticationStatus mode="checking" />;
    case "read_token_required":
      return <TokenPrompt />;
    case "unavailable":
      return <AuthenticationStatus mode="unavailable" />;
    case "anonymous":
    case "authenticated_with_read_token":
      return <RouterProvider router={router} />;
  }
}

export function App() {
  return (
    <AuthProvider>
      <DashboardApplication />
    </AuthProvider>
  );
}
