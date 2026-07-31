import type { RuntimeComponentListResponseDto } from "./dto";
import type { HttpClient } from "./http";

export function listRuntimeComponents(
  client: HttpClient,
  signal?: AbortSignal,
): Promise<RuntimeComponentListResponseDto> {
  return client.get<RuntimeComponentListResponseDto>(
    "/api/monitoring/runtime/components?limit=100&offset=0",
    { signal },
  );
}
