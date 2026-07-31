const TOKEN_HEADER = "X-Hermes-Control-Token";
const DEFAULT_TIMEOUT_MS = 10_000;

export type HttpErrorCode =
  | "authentication_required"
  | "invalid_response"
  | "network_unavailable"
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

export class HttpClient {
  readonly #readToken: () => string | null;
  readonly #handleUnauthorized: () => void;

  constructor(
    readToken: () => string | null,
    handleUnauthorized: () => void,
  ) {
    this.#readToken = readToken;
    this.#handleUnauthorized = handleUnauthorized;
  }

  async get<T>(path: string, options: HttpRequestOptions = {}): Promise<T> {
    if (!path.startsWith("/api/") && path !== "/healthz") {
      throw new HttpError("request_failed");
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

    const headers = new Headers({ Accept: "application/json" });
    const token = this.#readToken();
    if (token !== null) {
      headers.set(TOKEN_HEADER, token);
    }

    try {
      const response = await fetch(path, {
        method: "GET",
        headers,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
      });
      if (response.status === 401 || response.status === 403) {
        this.#handleUnauthorized();
        throw new HttpError("authentication_required", response.status);
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
