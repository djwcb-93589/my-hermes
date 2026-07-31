import type { BackendStatusResponseDto } from "./dto";
import type { HttpClient } from "./http";

export function readBackendStatus(
  client: HttpClient,
  signal?: AbortSignal,
): Promise<BackendStatusResponseDto> {
  return client.get<BackendStatusResponseDto>("/api/backend/status", {
    signal,
  });
}
