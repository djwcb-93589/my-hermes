const READ_TOKEN_HEADER = "X-Hermes-Read-Token";
const DEFAULT_TIMEOUT_MS = 10_000;

export type HttpAuthenticationMode = "anonymous" | "read_token";

export type HttpErrorCode =
  | "authentication_required"
  | "invalid_response"
  | "network_unavailable"
  | "permission_forbidden"
  | "request_failed"
  | "request_timeout";

export class HttpError extends Error {
  readonly code: HttpErrorCode;
  readonly status: number | null;

  constructor(code: HttpErrorCode, status: number | null = null) {
    super(code);
    this.name = "HttpError";
    this.code = code;
    this.status = status;
  }
}

export interface HttpRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

interface HttpClientOptions {
  authentication: HttpAuthenticationMode;
  readToken?: () => string | null;
  onAuthenticationRejected?: () => void;
}

export class HttpClient {
  readonly #authentication: HttpAuthenticationMode;
  readonly #readToken: (() => string | null) | null;
  readonly #handleAuthenticationRejected: (() => void) | null;

  constructor(options: HttpClientOptions) {
    this.#authentication = options.authentication;
    this.#readToken = options.readToken ?? null;
    this.#handleAuthenticationRejected =
      options.onAuthenticationRejected ?? null;
    if (this.#authentication === "read_token" && this.#readToken === null) {
      throw new Error("read_token authentication requires a token provider");
    }
  }

  async get<T>(path: string, options: HttpRequestOptions = {}): Promise<T> {
    if (!path.startsWith("/api/") && path !== "/healthz") {
      throw new HttpError("request_failed");
    }

    const headers = new Headers({ Accept: "application/json" });
    if (this.#authentication === "read_token") {
      const token = this.#readToken?.() ?? null;
      if (token === null) {
        this.#handleAuthenticationRejected?.();
        throw new HttpError("authentication_required", 401);
      }
      headers.set(READ_TOKEN_HEADER, token);
    }

    const controller = new AbortController();
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    let timedOut = false;
    const abortFromCaller = (): void => controller.abort();
    if (options.signal?.aborted === true) {
      controller.abort();
    } else {
      options.signal?.addEventListener("abort", abortFromCaller, {
        once: true,
      });
    }
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);

    try {
      const response = await fetch(path, {
        method: "GET",
        headers,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
      });
      if (response.status === 401 || response.status === 403) {
        if (
          this.#authentication === "read_token" ||
          response.status === 401
        ) {
          this.#handleAuthenticationRejected?.();
        }
        throw new HttpError(
          response.status === 401
            ? "authentication_required"
            : "permission_forbidden",
          response.status,
        );
      }
      if (!response.ok) {
        throw new HttpError("request_failed", response.status);
      }
      const contentType = response.headers.get("Content-Type") ?? "";
      if (!contentType.toLowerCase().includes("application/json")) {
        throw new HttpError("invalid_response", response.status);
      }
      const payload: unknown = await response.json();
      return payload as T;
    } catch (error: unknown) {
      if (error instanceof HttpError) {
        throw error;
      }
      if (controller.signal.aborted) {
        if (timedOut) {
          throw new HttpError("request_timeout");
        }
        throw error;
      }
      throw new HttpError("network_unavailable");
    } finally {
      window.clearTimeout(timeoutId);
      options.signal?.removeEventListener("abort", abortFromCaller);
    }
  }
}
