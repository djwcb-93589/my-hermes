const READ_TOKEN_HEADER = "X-Hermes-Read-Token";
const CONTROL_TOKEN_HEADER = "X-Hermes-Control-Token";
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
  readonly publicCode: string | null;

  constructor(
    code: HttpErrorCode,
    status: number | null = null,
    publicCode: string | null = null,
  ) {
    super(code);
    this.name = "HttpError";
    this.code = code;
    this.status = status;
    this.publicCode = publicCode;
  }
}

export interface HttpRequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  allowedPublicErrorCodes?: readonly string[];
}

interface HttpClientOptions {
  authentication: HttpAuthenticationMode;
  readToken?: () => string | null;
  onAuthenticationRejected?: () => void;
}

interface TransportRequestOptions extends HttpRequestOptions {
  method: "GET" | "PATCH" | "POST";
  headers: Headers;
  body?: object;
}

class HttpTransport {
  async request<T>(
    path: string,
    options: TransportRequestOptions,
  ): Promise<T> {
    let requestBody: string | undefined;
    try {
      requestBody =
        options.body === undefined
          ? undefined
          : JSON.stringify(options.body);
    } catch {
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

    try {
      const response = await fetch(path, {
        method: options.method,
        headers: options.headers,
        body: requestBody,
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
      });
      if (!response.ok) {
        const publicCode = await readAllowedPublicErrorCode(
          response,
          options.allowedPublicErrorCodes ?? [],
        );
        if (response.status === 401) {
          throw new HttpError(
            "authentication_required",
            response.status,
            publicCode,
          );
        }
        if (response.status === 403) {
          throw new HttpError(
            "permission_forbidden",
            response.status,
            publicCode,
          );
        }
        throw new HttpError("request_failed", response.status, publicCode);
      }
      const contentType = response.headers.get("Content-Type") ?? "";
      if (!contentType.toLowerCase().includes("application/json")) {
        throw new HttpError("invalid_response", response.status);
      }
      try {
        return (await response.json()) as T;
      } catch {
        throw new HttpError("invalid_response", response.status);
      }
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

const transport = new HttpTransport();

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

    try {
      return await transport.request<T>(path, {
        ...options,
        method: "GET",
        headers,
      });
    } catch (error: unknown) {
      if (
        error instanceof HttpError &&
        (error.status === 401 ||
          (this.#authentication === "read_token" && error.status === 403))
      ) {
        this.#handleAuthenticationRejected?.();
      }
      throw error;
    }
  }
}

export type GatewayControlTransportAction = "start" | "stop" | "restart";

export class EphemeralControlTransport {
  #controlToken: string | null;

  constructor(controlToken: string) {
    if (typeof controlToken !== "string" || controlToken.length === 0) {
      throw new HttpError("authentication_required", 401);
    }
    this.#controlToken = controlToken;
  }

  async patchConfig<T>(
    body: object,
    options: HttpRequestOptions = {},
  ): Promise<T> {
    const headers = this.#headers();
    headers.set("Content-Type", "application/json");
    return transport.request<T>("/api/config", {
      ...options,
      method: "PATCH",
      headers,
      body,
    });
  }

  async postGatewayAction<T>(
    action: GatewayControlTransportAction,
    idempotencyKey: string,
    options: HttpRequestOptions = {},
  ): Promise<T> {
    const path = gatewayControlPath(action);
    if (
      typeof idempotencyKey !== "string" ||
      idempotencyKey.trim() !== idempotencyKey ||
      idempotencyKey.length < 8 ||
      idempotencyKey.length > 128
    ) {
      throw new HttpError("request_failed");
    }
    const headers = this.#headers();
    try {
      headers.set("Idempotency-Key", idempotencyKey);
    } catch {
      throw new HttpError("request_failed");
    }
    return transport.request<T>(path, {
      ...options,
      method: "POST",
      headers,
    });
  }

  dispose(): void {
    this.#controlToken = null;
  }

  #headers(): Headers {
    const controlToken = this.#controlToken;
    if (controlToken === null) {
      throw new HttpError("authentication_required", 401);
    }
    const headers = new Headers({ Accept: "application/json" });
    try {
      headers.set(CONTROL_TOKEN_HEADER, controlToken);
    } catch {
      throw new HttpError("authentication_required", 401);
    }
    return headers;
  }
}

// 保留 M6.2 的公开导入名称，实际 Token 生命周期仍由同一传输实现负责。
export { EphemeralControlTransport as EphemeralConfigControlClient };

function gatewayControlPath(action: GatewayControlTransportAction): string {
  switch (action) {
    case "start":
      return "/api/backend/gateway/start";
    case "stop":
      return "/api/backend/gateway/stop";
    case "restart":
      return "/api/backend/gateway/restart";
  }
}

async function readAllowedPublicErrorCode(
  response: Response,
  allowedCodes: readonly string[],
): Promise<string | null> {
  if (allowedCodes.length === 0) {
    return null;
  }
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    return null;
  }
  try {
    const payload: unknown = await response.json();
    if (typeof payload !== "object" || payload === null) {
      return null;
    }
    const code = Reflect.get(payload, "code");
    return typeof code === "string" && allowedCodes.includes(code)
      ? code
      : null;
  } catch {
    return null;
  }
}
