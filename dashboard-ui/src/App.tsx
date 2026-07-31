import { AuthProvider, useAuth } from "./auth/AuthContext";
import { AuthenticationStatus } from "./auth/AuthenticationStatus";
import { TokenPrompt } from "./auth/TokenPrompt";
import { OverviewPage } from "./features/overview/OverviewPage";

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
      return <OverviewPage />;
  }
}

export function App() {
  return (
    <AuthProvider>
      <DashboardApplication />
    </AuthProvider>
  );
}
