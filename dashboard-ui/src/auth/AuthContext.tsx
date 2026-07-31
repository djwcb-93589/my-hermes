import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { HttpClient, HttpError } from "../api/http";
import { readStatus } from "../api/status";

export type AuthenticationState =
  | "signed_out"
  | "authenticating"
  | "signed_in";
export type AuthenticationResult = "accepted" | "invalid" | "unavailable";

interface AuthContextValue {
  client: HttpClient;
  state: AuthenticationState;
  authenticate: (token: string) => Promise<AuthenticationResult>;
  clearToken: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const tokenRef = useRef<string | null>(null);
  const [state, setState] = useState<AuthenticationState>("signed_out");

  const clearToken = useCallback((): void => {
    tokenRef.current = null;
    setState("signed_out");
  }, []);

  const client = useMemo(
    () => new HttpClient(() => tokenRef.current, clearToken),
    [clearToken],
  );

  const authenticate = useCallback(
    async (token: string): Promise<AuthenticationResult> => {
      if (token.length < 32 || token.trim() !== token) {
        return "invalid";
      }
      tokenRef.current = token;
      setState("authenticating");
      try {
        await readStatus(client);
        setState("signed_in");
        return "accepted";
      } catch (error: unknown) {
        tokenRef.current = null;
        setState("signed_out");
        if (
          error instanceof HttpError &&
          error.code === "authentication_required"
        ) {
          return "invalid";
        }
        return "unavailable";
      }
    },
    [client],
  );

  useEffect(
    () => () => {
      tokenRef.current = null;
    },
    [],
  );

  const value = useMemo<AuthContextValue>(
    () => ({ client, state, authenticate, clearToken }),
    [authenticate, clearToken, client, state],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return value;
}
