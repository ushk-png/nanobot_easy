import {
  Activity,
  Blocks,
  Bot,
  Brain,
  CalendarClock,
  Globe2,
  ImageIcon,
  Mic,
  Palette,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type { SettingsPayload } from "@/lib/types";

export type SettingsSectionKey =
  | "overview"
  | "easy-setup"
  | "appearance"
  | "models"
  | "image"
  | "voice"
  | "browser"
  | "apps"
  | "automations"
  | "skills"
  | "tools"
  | "agent-management"
  | "runtime"
  | "advanced";

export type SettingsNavTier = "basic" | "advanced";

// `easy-setup` is deliberately absent: it's been replaced by the full-screen
// OnboardingWizardPage and only ever opens automatically for new installs,
// never via this list. "advanced" items exist and stay reachable by direct
// link/URL, they just don't show up here unless the sidebar's own advanced
// toggle is on -- see SettingsSidebar's `showAdvanced`.
export const SETTINGS_NAV_ITEMS: Array<{
  key: SettingsSectionKey;
  icon: LucideIcon;
  fallback: string;
  tier: SettingsNavTier;
}> = [
  { key: "overview", icon: Activity, fallback: "Overview", tier: "basic" },
  { key: "models", icon: SlidersHorizontal, fallback: "Models", tier: "basic" },
  { key: "apps", icon: Blocks, fallback: "Connections", tier: "basic" },
  { key: "tools", icon: Wrench, fallback: "App Tools", tier: "basic" },
  { key: "skills", icon: Brain, fallback: "Skills", tier: "basic" },
  { key: "automations", icon: CalendarClock, fallback: "Automations", tier: "basic" },
  { key: "agent-management", icon: Bot, fallback: "Agent management", tier: "basic" },
  { key: "appearance", icon: Palette, fallback: "Appearance", tier: "advanced" },
  { key: "image", icon: ImageIcon, fallback: "Image", tier: "advanced" },
  { key: "voice", icon: Mic, fallback: "Voice", tier: "advanced" },
  { key: "browser", icon: Globe2, fallback: "Web", tier: "advanced" },
  { key: "runtime", icon: Server, fallback: "System", tier: "advanced" },
  { key: "advanced", icon: ShieldCheck, fallback: "Security", tier: "advanced" },
];

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
