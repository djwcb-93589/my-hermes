import type { DatabaseHealthResponseDto } from "./dto";
import type { HttpClient } from "./http";

export function readDatabaseHealth(
  client: HttpClient,
  signal?: AbortSignal,
): Promise<DatabaseHealthResponseDto> {
  return client.get<DatabaseHealthResponseDto>(
    "/api/monitoring/database/health",
    { signal },
  );
}
