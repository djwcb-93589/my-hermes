import {
  patchConfig,
  readConfig,
  type ConfigPatchApiRequest,
} from "../../api/config";
import type { HttpClient } from "../../api/http";
import { mapConfigSaveResult, mapConfigSnapshot } from "./configMapper";
import type {
  ConfigChangeViewModel,
  ConfigPageViewModel,
  ConfigSaveResultViewModel,
} from "./configModels";

export async function loadConfiguration(
  client: HttpClient,
  signal: AbortSignal,
): Promise<ConfigPageViewModel> {
  return mapConfigSnapshot(await readConfig(client, signal));
}

export async function saveConfiguration(
  controlToken: string,
  revision: string,
  changes: readonly ConfigChangeViewModel[],
  signal: AbortSignal,
): Promise<ConfigSaveResultViewModel> {
  const request: ConfigPatchApiRequest = {
    expected_revision: revision,
    changes: changes.map((change) => ({
      name: change.name,
      value: Array.isArray(change.value)
        ? [...change.value]
        : change.value,
    })),
  };
  return mapConfigSaveResult(
    await patchConfig(controlToken, request, signal),
  );
}

