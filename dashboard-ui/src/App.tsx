import { AuthProvider, useAuth } from "./auth/AuthContext";
import { TokenPrompt } from "./auth/TokenPrompt";
import { OverviewPage } from "./features/overview/OverviewPage";

function DashboardApplication() {
  const { state } = useAuth();
  if (state !== "signed_in") {
    return <TokenPrompt />;
  }
  return <OverviewPage />;
}

export function App() {
  return (
    <AuthProvider>
      <DashboardApplication />
    </AuthProvider>
  );
}
