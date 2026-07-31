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
  | "checking_anonymous"
  | "anonymous"
  | "read_token_required"
  | "authenticated_with_read_token"
  | "unavailable";
export type AuthenticationResult = "accepted" | "invalid" | "unavailable";

interface AuthContextValue {
  client: HttpClient;
  state: AuthenticationState;
  isAuthenticating: boolean;
  authenticateReadToken: (token: string) => Promise<AuthenticationResult>;
  clearReadToken: () => void;
  retryAnonymousProbe: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const readTokenRef = useRef<string | null>(null);
  const anonymousProbeRef = useRef<AbortController | null>(null);
  const readAuthenticationRef = useRef<AbortController | null>(null);
  const [state, setState] = useState<AuthenticationState>(
    "checking_anonymous",
  );
  const [isAuthenticating, setIsAuthenticating] = useState(false);

  const handleAnonymousAuthenticationRequired = useCallback((): void => {
    readTokenRef.current = null;
    setIsAuthenticating(false);
    setState("read_token_required");
  }, []);

  const handleReadAuthenticationRejected = useCallback((): void => {
    readTokenRef.current = null;
    setIsAuthenticating(false);
    setState("read_token_required");
  }, []);

  const anonymousClient = useMemo(
    () =>
      new HttpClient({
        authentication: "anonymous",
        onAuthenticationRejected: handleAnonymousAuthenticationRequired,
      }),
    [handleAnonymousAuthenticationRequired],
  );
  const readTokenClient = useMemo(
    () =>
      new HttpClient({
        authentication: "read_token",
        readToken: () => readTokenRef.current,
        onAuthenticationRejected: handleReadAuthenticationRejected,
      }),
    [handleReadAuthenticationRejected],
  );

  const probeAnonymousRead = useCallback(async (): Promise<void> => {
    anonymousProbeRef.current?.abort();
    const controller = new AbortController();
    anonymousProbeRef.current = controller;
    readTokenRef.current = null;
    setIsAuthenticating(false);
    setState("checking_anonymous");
    try {
      await readStatus(anonymousClient, controller.signal);
      if (!controller.signal.aborted) {
        setState("anonymous");
      }
    } catch (error: unknown) {
      if (controller.signal.aborted) {
        return;
      }
      if (error instanceof HttpError && error.status === 401) {
        setState("read_token_required");
      } else {
        setState("unavailable");
      }
    } finally {
      if (anonymousProbeRef.current === controller) {
        anonymousProbeRef.current = null;
      }
    }
  }, [anonymousClient]);

  const authenticateReadToken = useCallback(
    async (token: string): Promise<AuthenticationResult> => {
      if (token.length < 32 || token.trim() !== token) {
        return "invalid";
      }
      readAuthenticationRef.current?.abort();
      const controller = new AbortController();
      readAuthenticationRef.current = controller;
      readTokenRef.current = token;
      setIsAuthenticating(true);
      try {
        await readStatus(readTokenClient, controller.signal);
        if (controller.signal.aborted) {
          return "unavailable";
        }
        setState("authenticated_with_read_token");
        return "accepted";
      } catch (error: unknown) {
        if (controller.signal.aborted) {
          return "unavailable";
        }
        readTokenRef.current = null;
        if (
          error instanceof HttpError &&
          (error.status === 401 || error.status === 403)
        ) {
          setState("read_token_required");
          return "invalid";
        }
        setState("unavailable");
        return "unavailable";
      } finally {
        if (readAuthenticationRef.current === controller) {
          readAuthenticationRef.current = null;
        }
        if (!controller.signal.aborted) {
          setIsAuthenticating(false);
        }
      }
    },
    [readTokenClient],
  );

  const clearReadToken = useCallback((): void => {
    readAuthenticationRef.current?.abort();
    readAuthenticationRef.current = null;
    readTokenRef.current = null;
    setIsAuthenticating(false);
    setState("read_token_required");
  }, []);

  const retryAnonymousProbe = useCallback((): void => {
    void probeAnonymousRead();
  }, [probeAnonymousRead]);

  useEffect(() => {
    void probeAnonymousRead();
    return () => {
      anonymousProbeRef.current?.abort();
      anonymousProbeRef.current = null;
      readAuthenticationRef.current?.abort();
      readAuthenticationRef.current = null;
      readTokenRef.current = null;
    };
  }, [probeAnonymousRead]);

  const client =
    state === "authenticated_with_read_token"
      ? readTokenClient
      : anonymousClient;
  const value = useMemo<AuthContextValue>(
    () => ({
      client,
      state,
      isAuthenticating,
      authenticateReadToken,
      clearReadToken,
      retryAnonymousProbe,
    }),
    [
      authenticateReadToken,
      clearReadToken,
      client,
      isAuthenticating,
      retryAnonymousProbe,
      state,
    ],
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
