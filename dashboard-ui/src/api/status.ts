import type { StatusResponseDto } from "./dto";
import type { HttpClient } from "./http";

export function readStatus(
  client: HttpClient,
  signal?: AbortSignal,
): Promise<StatusResponseDto> {
  return client.get<StatusResponseDto>("/api/status", { signal });
}
