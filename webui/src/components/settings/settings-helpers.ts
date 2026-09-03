import type { SettingsPayload } from "@/lib/types";

export interface ModelConfigurationDraft {
  label: string;
  provider: string;
  model: string;
}

export function modelPresetValue(payload: SettingsPayload): string {
  return payload.agent.model_preset || "default";
}

export function defaultPreset(payload: SettingsPayload): SettingsPayload["model_presets"][number] | null {
  return payload.model_presets.find((preset) => preset.is_default) ?? null;
}

export function normalizeContextWindowTokens(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 200_000;
}

export function editableDefaultProvider(payload: SettingsPayload): string {
  const base = defaultPreset(payload);
  return base?.provider ?? payload.agent.provider ?? payload.agent.resolved_provider ?? "";
}

export function settingsProviderRow(
  payload: SettingsPayload,
  provider: string | null | undefined,
): SettingsPayload["providers"][number] | null {
  if (!provider) return null;
  return payload.providers.find((row) => row.name === provider) ?? null;
}

export function settingsProviderConfigured(
  payload: SettingsPayload,
  provider: string | null | undefined,
): boolean {
  const row = settingsProviderRow(payload, provider);
  if (row) return row.configured;
  if (provider === "auto") {
    const resolvedRow = settingsProviderRow(
      payload,
      payload.agent.resolved_provider ?? payload.agent.provider,
    );
    if (resolvedRow) return resolvedRow.configured;
  }
  return payload.agent.has_api_key;
}

export const DEFAULT_TRANSCRIPTION_SETTINGS: NonNullable<SettingsPayload["transcription"]> = {
  enabled: true,
  provider: "groq",
  provider_configured: false,
  model: "whisper-large-v3",
  language: null,
  max_duration_sec: 120,
  max_upload_mb: 25,
  providers: [],
};

export function agentProviderIsConfigured(payload: SettingsPayload): boolean {
  const provider = payload.agent.provider === "auto"
    ? payload.agent.resolved_provider ?? payload.agent.provider
    : payload.agent.provider;
  return settingsProviderConfigured(payload, provider);
}

export type WebSearchProviderOption = SettingsPayload["web_search"]["providers"][number];

export function webSearchProviderAcceptsApiKey(provider?: WebSearchProviderOption): boolean {
  return provider?.credential === "api_key" || provider?.credential === "optional_api_key";
}

export function webSearchProviderRequiresApiKey(provider?: WebSearchProviderOption): boolean {
  return provider?.credential === "api_key";
}
