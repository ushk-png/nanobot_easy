import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  Activity,
  Blocks,
  Bot,
  Brain,
  CalendarClock,
  ChevronLeft,
  Globe2,
  ImageIcon,
  Loader2,
  Mic,
  Palette,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  SkillsCatalogSettings,
} from "@/components/settings/SkillsCatalogSettings";
import {
  createModelConfiguration,
  disableNanobotFeature,
  enableNanobotFeature,
  fetchAutomations,
  fetchSettings,
  fetchSettingsUsage,
  fetchCliApps,
  fetchMcpPresets,
  fetchNanobotFeatures,
  importMcpConfig,
  loginProviderOAuth,
  logoutProviderOAuth,
  runAutomationAction,
  runCliAppAction,
  runMcpPresetAction,
  saveCustomMcpServer,
  updateAutomation,
  updateImageGenerationSettings,
  updateMcpServerTools,
  updateModelConfiguration,
  updateNetworkSafetySettings,
  updateProviderSettings,
  updateSettings,
  updateSkillGovernanceSettings,
  updateStudentModeSettings,
  updateTranscriptionSettings,
  updateWebSearchSettings,
} from "@/lib/api";
import { notifyCliAppsChanged } from "@/lib/cli-app-events";
import { notifyMcpPresetsChanged } from "@/lib/mcp-preset-events";
import {
} from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";
import type {
  AutomationsPayload,
  AutomationUpdatePayload,
  CliAppsPayload,
  ImageGenerationSettingsUpdate,
  InstalledExternalTool,
  McpPresetsPayload,
  NanobotFeatureInfo,
  NanobotFeaturesPayload,
  NetworkSafetySettingsUpdate,
  SkillGovernanceSettingsUpdate,
  SessionAutomationJob,
  SettingsPayload,
  SkillSummary,
  StudentModeSettingsUpdate,
  TranscriptionSettingsUpdate,
  WebSearchSettingsUpdate,
  WebuiDefaultAccessMode,
} from "@/lib/types";
import {
  SettingsGroup,
  SettingsRow,
} from "@/components/settings/settings-primitives";
import {
  DEFAULT_TRANSCRIPTION_SETTINGS,
  defaultPreset,
  editableDefaultProvider,
  modelPresetValue,
  normalizeContextWindowTokens,
  webSearchProviderAcceptsApiKey,
  webSearchProviderRequiresApiKey,
  type ModelConfigurationDraft,
} from "@/components/settings/settings-helpers";
import {
} from "@/components/settings/settings-provider-picker";
import {
  AutomationDeleteDialog,
  AutomationEditDialog,
  AutomationsSettings,
  NanobotFeatureInstallDialog,
} from "@/components/settings/AutomationsSettings";
import {
  AppsCatalogSettings,
  InstalledToolsSettings,
} from "@/components/settings/AppsCatalogSettings";
import { EasySetupWizard } from "@/components/settings/EasySetupWizard";
import { AgentToolsSettings } from "@/components/settings/AgentToolsSettings";
import { AgentManagementSettings } from "@/components/settings/AgentManagementSettings";
import {
  AdvancedSettings,
  SkillGovernanceQuickPanel,
  StudentModeQuickPanel,
} from "@/components/settings/AdvancedSettings";
import { OverviewSettings } from "@/components/settings/OverviewSettings";
import { AppearanceSettings } from "@/components/settings/AppearanceSettings";
import { ModelsSettings, NewModelConfigurationDialog } from "@/components/settings/ModelsSettings";
import { ProvidersSettings } from "@/components/settings/ProvidersSettings";
import { ImageGenerationSettings } from "@/components/settings/ImageGenerationSettings";
import { TranscriptionSettings } from "@/components/settings/TranscriptionSettings";
import { WebSettings } from "@/components/settings/WebSettings";
import { RuntimeSettings } from "@/components/settings/RuntimeSettings";
import { SettingsSidebar } from "@/components/settings/SettingsSidebar";

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

export type LocalDensity = "comfortable" | "compact";
export type LocalActivityMode = "auto" | "expanded";
export type AppsKindFilter = "all" | "nanobot" | "cli" | "mcp";
export type AutomationFilter = "all" | "active" | "paused" | "failed" | "system";
export type AutomationSort = "next" | "last" | "updated" | "name";
export type AutomationAction = "enable" | "disable" | "delete" | "run";
export interface LocalPreferences {
  density: LocalDensity;
  activityMode: LocalActivityMode;
  codeWrap: boolean;
  brandLogos: boolean;
  showAdvancedSettings: boolean;
}

export interface AgentSettingsDraft {
  model: string;
  provider: string;
  modelPreset: string;
  presetLabel: string;
  contextWindowTokens: number;
  timezone: string;
  botName: string;
  botIcon: string;
  toolHintMaxLength: number;
}

type PendingRestartSection = "runtime" | "browser" | "image";
type PendingRestartSections = Record<PendingRestartSection, boolean>;
type RestartAwarePayload = {
  requires_restart?: boolean;
  surface?: SettingsPayload["surface"];
  runtime_surface?: SettingsPayload["runtime_surface"];
  runtime_capabilities?: SettingsPayload["runtime_capabilities"];
};
export type ProviderApiType = "auto" | "chat_completions" | "responses";
export type ProviderForm = { apiKey: string; apiBase: string; apiType: ProviderApiType };
export type CustomMcpTransport = "stdio" | "streamableHttp" | "sse";

const CLI_APPS_REFRESH_RETRY_MS = 2_000;
const CLI_APPS_REFRESH_MAX_RETRIES = 30;

export interface CustomMcpForm {
  name: string;
  transport: CustomMcpTransport;
  command: string;
  args: string;
  url: string;
  env: string;
  headers: string;
  toolTimeout: string;
}

const LOCAL_PREFS_STORAGE_KEY = "nanobot-webui.settings-preferences";

const DEFAULT_LOCAL_PREFS: LocalPreferences = {
  density: "comfortable",
  activityMode: "auto",
  codeWrap: true,
  brandLogos: false,
  showAdvancedSettings: false,
};
const EMPTY_PENDING_RESTART_SECTIONS: PendingRestartSections = {
  runtime: false,
  browser: false,
  image: false,
};

const DEFAULT_CUSTOM_MCP_FORM: CustomMcpForm = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  url: "",
  env: "",
  headers: "",
  toolTimeout: "30",
};

interface SettingsViewProps {
  theme: "light" | "dark";
  initialSection?: SettingsSectionKey;
  initialSettings?: SettingsPayload | null;
  showSidebar?: boolean;
  onToggleTheme: () => void;
  onBackToChat: () => void;
  onModelNameChange: (modelName: string | null) => void;
  onSettingsChange?: (payload: SettingsPayload) => void;
  skills?: SkillSummary[];
  installedTools?: InstalledExternalTool[];
  onWorkspaceSettingsChange?: () => void | Promise<void>;
  onSectionChange?: (section: SettingsSectionKey) => void;
  onLogout?: () => void;
  onRestart?: () => void;
  onNativeEngineRestart?: () => Promise<string>;
  isRestarting?: boolean;
  hostChromeInset?: boolean;
}

function readLocalPreferences(): LocalPreferences {
  try {
    const raw = window.localStorage.getItem(LOCAL_PREFS_STORAGE_KEY);
    if (!raw) return DEFAULT_LOCAL_PREFS;
    const parsed = JSON.parse(raw) as Partial<LocalPreferences>;
    return {
      density: parsed.density === "compact" ? "compact" : "comfortable",
      activityMode: parsed.activityMode === "expanded" ? "expanded" : "auto",
      codeWrap: parsed.codeWrap !== false,
      brandLogos: parsed.brandLogos === true,
      showAdvancedSettings: parsed.showAdvancedSettings === true,
    };
  } catch {
    return DEFAULT_LOCAL_PREFS;
  }
}

const DEFAULT_AGENT_SETTINGS_DRAFT: AgentSettingsDraft = {
  model: "",
  provider: "",
  modelPreset: "default",
  presetLabel: "Default",
  contextWindowTokens: 200_000,
  timezone: "UTC",
  botName: "nanobot",
  botIcon: "",
  toolHintMaxLength: 40,
};

const DEFAULT_WEB_SEARCH_FORM: WebSearchSettingsUpdate = {
  provider: "duckduckgo",
  apiKey: "",
  baseUrl: "",
  maxResults: 5,
  timeout: 30,
  useJinaReader: true,
};

const DEFAULT_IMAGE_GENERATION_FORM: ImageGenerationSettingsUpdate = {
  enabled: false,
  provider: "openrouter",
  model: "openai/gpt-5.4-image-2",
  defaultAspectRatio: "1:1",
  defaultImageSize: "1K",
  maxImagesPerTurn: 4,
};

const DEFAULT_TRANSCRIPTION_FORM: TranscriptionSettingsUpdate = {
  enabled: true,
  provider: "groq",
  model: "",
  language: "",
  maxDurationSec: 120,
  maxUploadMb: 25,
};

const DEFAULT_NETWORK_SAFETY_FORM: NetworkSafetySettingsUpdate = {
  webuiAllowLocalServiceAccess: true,
  webuiDefaultAccessMode: "default",
};

const DEFAULT_SKILL_GOVERNANCE_FORM: Required<SkillGovernanceSettingsUpdate> = {
  webuiSkillManagementEnabled: false,
  externalToolSkillsEnabled: false,
  draftExpireDays: 30,
  minRoutingPasses: 7,
  securityRiskAtLeast: "medium",
  securityBlockAtLeast: "high",
  duplicateScoreAtLeast: 0.8,
  allowedInstallDomains: ["github.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org"],
  installRoot: "tools",
  denyGlobalInstall: true,
};

const DEFAULT_STUDENT_MODE_FORM: Required<StudentModeSettingsUpdate> = {
  mode: "general",
  coachName: "담임 선생님",
  reviewTeacherName: "AGENT_A 선생님",
  studyLogPath: "study_log.jsonl",
  reviewQueuePath: "review_queue.jsonl",
  dailyReviewCronName: "student-mode-daily-review",
};

function skillGovernanceFormFromPayload(payload: SettingsPayload): Required<SkillGovernanceSettingsUpdate> {
  const governance = payload.skill_governance;
  if (!governance) return DEFAULT_SKILL_GOVERNANCE_FORM;
  return {
    webuiSkillManagementEnabled: governance.webui_skill_management.enabled,
    externalToolSkillsEnabled: governance.external_tool_skills.enabled,
    draftExpireDays: governance.webui_skill_management.draft_expire_days,
    minRoutingPasses: governance.webui_skill_management.red_flags.min_routing_passes,
    securityRiskAtLeast: governance.webui_skill_management.red_flags.security_risk_at_least,
    securityBlockAtLeast: governance.webui_skill_management.red_flags.security_block_at_least,
    duplicateScoreAtLeast: governance.webui_skill_management.red_flags.duplicate_score_at_least,
    allowedInstallDomains: governance.external_tool_skills.allowed_install_domains,
    installRoot: governance.external_tool_skills.install_root,
    denyGlobalInstall: governance.external_tool_skills.deny_global_install,
  };
}

function studentModeFormFromPayload(payload: SettingsPayload): Required<StudentModeSettingsUpdate> {
  const student = payload.student_mode;
  if (!student) return DEFAULT_STUDENT_MODE_FORM;
  return {
    mode: student.mode,
    coachName: student.coach_name,
    reviewTeacherName: student.review_teacher_name,
    studyLogPath: student.study_log_path,
    reviewQueuePath: student.review_queue_path,
    dailyReviewCronName: student.daily_review_cron_name,
  };
}

function agentDraftFromPayload(payload: SettingsPayload): AgentSettingsDraft {
  const fallbackDefault = defaultPreset(payload);
  const activePresetName = modelPresetValue(payload);
  const activePreset =
    payload.model_presets.find((preset) => preset.name === activePresetName) ?? fallbackDefault;
  return {
    model: activePreset?.model ?? payload.agent.model,
    provider: activePreset?.is_default
      ? editableDefaultProvider(payload)
      : activePreset?.provider ?? editableDefaultProvider(payload),
    modelPreset: activePresetName,
    presetLabel: activePreset?.label ?? activePresetName,
    contextWindowTokens: normalizeContextWindowTokens(
      activePreset?.context_window_tokens ?? payload.agent.context_window_tokens,
    ),
    timezone: payload.agent.timezone,
    botName: payload.agent.bot_name,
    botIcon: payload.agent.bot_icon,
    toolHintMaxLength: payload.agent.tool_hint_max_length,
  };
}

function webSearchFormFromPayload(
  payload: SettingsPayload,
  previous?: WebSearchSettingsUpdate,
): WebSearchSettingsUpdate {
  return {
    provider: payload.web_search.provider,
    apiKey: previous?.provider === payload.web_search.provider ? previous.apiKey ?? "" : "",
    baseUrl: payload.web_search.base_url ?? "",
    maxResults: payload.web_search.max_results,
    timeout: payload.web_search.timeout,
    useJinaReader: payload.web.fetch.use_jina_reader,
  };
}

function imageGenerationFormFromPayload(payload: SettingsPayload): ImageGenerationSettingsUpdate {
  return {
    enabled: payload.image_generation.enabled,
    provider: payload.image_generation.provider,
    model: payload.image_generation.model,
    defaultAspectRatio: payload.image_generation.default_aspect_ratio,
    defaultImageSize: payload.image_generation.default_image_size,
    maxImagesPerTurn: payload.image_generation.max_images_per_turn,
  };
}

function transcriptionFormFromPayload(payload: SettingsPayload): TranscriptionSettingsUpdate {
  const transcription = payload.transcription ?? DEFAULT_TRANSCRIPTION_SETTINGS;
  return {
    enabled: transcription.enabled,
    provider: transcription.provider,
    model: transcription.model,
    language: transcription.language ?? "",
    maxDurationSec: transcription.max_duration_sec,
    maxUploadMb: transcription.max_upload_mb,
  };
}

function networkSafetyFormFromPayload(payload: SettingsPayload): NetworkSafetySettingsUpdate {
  return {
    webuiAllowLocalServiceAccess:
      payload.advanced.webui_allow_local_service_access ??
      payload.advanced.allow_local_preview_access ??
      true,
    webuiDefaultAccessMode: visibleWebuiDefaultAccessMode(
      payload.advanced.webui_default_access_mode,
    ),
  };
}

function pendingRestartSectionsFromPayload(payload: SettingsPayload): PendingRestartSections {
  const sections = payload.restart_required_sections ?? [];
  return {
    runtime: sections.includes("runtime"),
    browser: sections.includes("browser"),
    image: sections.includes("image"),
  };
}

export function SettingsView({
  theme,
  initialSection = "overview",
  initialSettings = null,
  showSidebar = true,
  onToggleTheme,
  onBackToChat,
  onModelNameChange,
  onSettingsChange,
  skills = [],
  installedTools = [],
  onWorkspaceSettingsChange,
  onSectionChange,
  onLogout,
  onRestart,
  onNativeEngineRestart,
  isRestarting = false,
  hostChromeInset = false,
}: SettingsViewProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [settings, setSettings] = useState<SettingsPayload | null>(() => initialSettings);
  const [cliApps, setCliApps] = useState<CliAppsPayload | null>(null);
  const [nanobotFeatures, setNanobotFeatures] = useState<NanobotFeaturesPayload | null>(null);
  const [mcpPresets, setMcpPresets] = useState<McpPresetsPayload | null>(null);
  const [automations, setAutomations] = useState<AutomationsPayload | null>(null);
  const [loading, setLoading] = useState(() => initialSettings === null);
  const [cliAppsLoading, setCliAppsLoading] = useState(true);
  const [nanobotFeaturesLoading, setNanobotFeaturesLoading] = useState(true);
  const [mcpPresetsLoading, setMcpPresetsLoading] = useState(true);
  const [automationsLoading, setAutomationsLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modelConfigurationOpen, setModelConfigurationOpen] = useState(false);
  const [modelConfigurationSaving, setModelConfigurationSaving] = useState(false);
  const [modelConfigurationForm, setModelConfigurationForm] = useState<ModelConfigurationDraft>({
    label: "",
    provider: "",
    model: "",
  });
  const [cliAppsAction, setCliAppsAction] = useState<string | null>(null);
  const [nanobotFeatureAction, setNanobotFeatureAction] = useState<string | null>(null);
  const [nanobotFeatureConfirm, setNanobotFeatureConfirm] = useState<NanobotFeatureInfo | null>(null);
  const [mcpPresetAction, setMcpPresetAction] = useState<string | null>(null);
  const [providerSaving, setProviderSaving] = useState<string | null>(null);
  const [webSearchSaving, setWebSearchSaving] = useState(false);
  const [imageGenerationSaving, setImageGenerationSaving] = useState(false);
  const [transcriptionSaving, setTranscriptionSaving] = useState(false);
  const [networkSafetySaving, setNetworkSafetySaving] = useState(false);
  const [skillGovernanceSaving, setSkillGovernanceSaving] = useState(false);
  const [studentModeSaving, setStudentModeSaving] = useState(false);
  const [hostEngineApplying, setHostEngineApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSectionKey>(initialSection);
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [providerQuery, setProviderQuery] = useState("");
  const [appsQuery, setAppsQuery] = useState("");
  const [automationsQuery, setAutomationsQuery] = useState("");
  const [automationsFilter, setAutomationsFilter] = useState<AutomationFilter>("all");
  const [automationsSort, setAutomationsSort] = useState<AutomationSort>("next");
  const [cliAppsMessage, setCliAppsMessage] = useState<string | null>(null);
  const [cliAppsError, setCliAppsError] = useState<string | null>(null);
  const [nanobotFeaturesMessage, setNanobotFeaturesMessage] = useState<string | null>(null);
  const [nanobotFeaturesError, setNanobotFeaturesError] = useState<string | null>(null);
  const [cliAppsFocusName, setCliAppsFocusName] = useState<string | null>(null);
  const [appsKindFilter, setAppsKindFilter] = useState<AppsKindFilter>("all");
  const [mcpMessage, setMcpMessage] = useState<string | null>(null);
  const [mcpError, setMcpError] = useState<string | null>(null);
  const [automationsError, setAutomationsError] = useState<string | null>(null);
  const [automationAction, setAutomationAction] = useState<string | null>(null);
  const [automationPendingDelete, setAutomationPendingDelete] =
    useState<SessionAutomationJob | null>(null);
  const [automationPendingEdit, setAutomationPendingEdit] =
    useState<SessionAutomationJob | null>(null);
  const [mcpFieldValues, setMcpFieldValues] = useState<Record<string, Record<string, string>>>({});
  const [customMcpForm, setCustomMcpForm] = useState<CustomMcpForm>(DEFAULT_CUSTOM_MCP_FORM);
  const [mcpConfigImport, setMcpConfigImport] = useState("");
  const [providerForms, setProviderForms] = useState<Record<string, ProviderForm>>({});
  const [visibleProviderKeys, setVisibleProviderKeys] = useState<Record<string, boolean>>({});
  const [editingProviderKeys, setEditingProviderKeys] = useState<Record<string, boolean>>({});
  const [pendingRestartSections, setPendingRestartSections] = useState<PendingRestartSections>(
    EMPTY_PENDING_RESTART_SECTIONS,
  );
  const [localPrefs, setLocalPrefs] = useState<LocalPreferences>(() => readLocalPreferences());
  const [webSearchForm, setWebSearchForm] = useState<WebSearchSettingsUpdate>(() =>
    initialSettings ? webSearchFormFromPayload(initialSettings) : DEFAULT_WEB_SEARCH_FORM,
  );
  const [imageGenerationForm, setImageGenerationForm] = useState<ImageGenerationSettingsUpdate>(
    () =>
      initialSettings
        ? imageGenerationFormFromPayload(initialSettings)
        : DEFAULT_IMAGE_GENERATION_FORM,
  );
  const [transcriptionForm, setTranscriptionForm] = useState<TranscriptionSettingsUpdate>(
    () => initialSettings ? transcriptionFormFromPayload(initialSettings) : DEFAULT_TRANSCRIPTION_FORM,
  );
  const [networkSafetyForm, setNetworkSafetyForm] = useState<NetworkSafetySettingsUpdate>(() =>
    initialSettings ? networkSafetyFormFromPayload(initialSettings) : DEFAULT_NETWORK_SAFETY_FORM,
  );
  const [skillGovernanceForm, setSkillGovernanceForm] = useState<Required<SkillGovernanceSettingsUpdate>>(
    () => initialSettings ? skillGovernanceFormFromPayload(initialSettings) : DEFAULT_SKILL_GOVERNANCE_FORM,
  );
  const [studentModeForm, setStudentModeForm] = useState<Required<StudentModeSettingsUpdate>>(
    () => initialSettings ? studentModeFormFromPayload(initialSettings) : DEFAULT_STUDENT_MODE_FORM,
  );

  useEffect(() => {
    setActiveSection(initialSection);
  }, [initialSection]);

  const selectSection = useCallback(
    (section: SettingsSectionKey) => {
      setActiveSection(section);
      onSectionChange?.(section);
    },
    [onSectionChange],
  );
  const [webSearchKeyVisible, setWebSearchKeyVisible] = useState(false);
  const [webSearchKeyEditing, setWebSearchKeyEditing] = useState(false);
  const [form, setForm] = useState<AgentSettingsDraft>(() =>
    initialSettings ? agentDraftFromPayload(initialSettings) : DEFAULT_AGENT_SETTINGS_DRAFT,
  );

  const text = useCallback(
    (key: string, fallback: string, options?: Record<string, unknown>) =>
      t(key, { defaultValue: fallback, ...(options ?? {}) }),
    [t],
  );

  const applyPayload = useCallback((payload: SettingsPayload) => {
    setSettings(payload);
    setForm(agentDraftFromPayload(payload));
    setWebSearchForm((prev) => webSearchFormFromPayload(payload, prev));
    setImageGenerationForm(imageGenerationFormFromPayload(payload));
    setTranscriptionForm(transcriptionFormFromPayload(payload));
    setNetworkSafetyForm(networkSafetyFormFromPayload(payload));
    setSkillGovernanceForm(skillGovernanceFormFromPayload(payload));
    setStudentModeForm(studentModeFormFromPayload(payload));
    if (payload.restart_required_sections) {
      setPendingRestartSections(pendingRestartSectionsFromPayload(payload));
    }
    onSettingsChange?.(payload);
  }, [onSettingsChange]);

  useEffect(() => {
    if (!initialSettings || settings !== null) return;
    applyPayload(initialSettings);
    setLoading(false);
  }, [applyPayload, initialSettings, settings]);

  useEffect(() => {
    let cancelled = false;
    const showLoading = settings === null;
    if (showLoading) setLoading(true);
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) {
          applyPayload(payload);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled && showLoading) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyPayload, token]);

  const hasSettings = settings !== null;
  useEffect(() => {
    if (activeSection !== "overview" || !hasSettings) return;
    let cancelled = false;
    const refresh = () => {
      fetchSettingsUsage(token)
        .then((usage) => {
          if (cancelled) return;
          setSettings((current) => (current ? { ...current, usage } : current));
        })
        .catch(() => {});
    };
    void refresh();
    const interval = window.setInterval(refresh, 5000);
    const onFocus = () => refresh();
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [activeSection, hasSettings, token]);

  useEffect(() => {
    if (activeSection !== "apps") return;
    let cancelled = false;
    let retry: number | null = null;
    let retryCount = 0;
    const loadCliApps = (showLoading: boolean) => {
      if (showLoading) setCliAppsLoading(true);
      fetchCliApps(token)
        .then((payload) => {
          if (cancelled) return;
          if (payload.catalog_refresh_pending && retryCount < CLI_APPS_REFRESH_MAX_RETRIES) {
            retryCount += 1;
            retry = window.setTimeout(() => {
              retry = null;
              loadCliApps(false);
            }, CLI_APPS_REFRESH_RETRY_MS);
          }
          setCliApps(payload);
          setCliAppsError(null);
          setCliAppsLoading(false);
        })
        .catch((err) => {
          if (!cancelled) {
            setCliAppsError((err as Error).message);
            setCliAppsLoading(false);
          }
        });
    };
    loadCliApps(true);
    return () => {
      cancelled = true;
      if (retry !== null) window.clearTimeout(retry);
    };
  }, [activeSection, token]);

  useEffect(() => {
    if (activeSection !== "apps") return;
    let cancelled = false;
    setNanobotFeaturesLoading(true);
    fetchNanobotFeatures(token)
      .then((payload) => {
        if (!cancelled) {
          setNanobotFeatures(payload);
          setNanobotFeaturesError(null);
        }
      })
      .catch((err) => {
        const message = (err as Error).message;
        if (!cancelled && message !== "HTTP 404") setNanobotFeaturesError(message);
      })
      .finally(() => {
        if (!cancelled) setNanobotFeaturesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSection, token]);

  useEffect(() => {
    if (activeSection !== "apps") return;
    let cancelled = false;
    setMcpPresetsLoading(true);
    fetchMcpPresets(token)
      .then((payload) => {
        if (!cancelled) {
          setMcpPresets(payload);
          setMcpError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setMcpError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setMcpPresetsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSection, token]);

  const refreshAutomations = useCallback(
    async (showLoading = false) => {
      if (showLoading) setAutomationsLoading(true);
      try {
        const payload = await fetchAutomations(token);
        setAutomations(payload);
        setAutomationsError(null);
      } catch (err) {
        setAutomationsError((err as Error).message);
      } finally {
        if (showLoading) setAutomationsLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    if (activeSection !== "automations") return;
    let cancelled = false;
    const refresh = async (showLoading = false) => {
      if (cancelled) return;
      if (showLoading) setAutomationsLoading(true);
      try {
        const payload = await fetchAutomations(token);
        if (cancelled) return;
        setAutomations(payload);
        setAutomationsError(null);
      } catch (err) {
        if (!cancelled) setAutomationsError((err as Error).message);
      } finally {
        if (!cancelled && showLoading) setAutomationsLoading(false);
      }
    };
    void refresh(true);
    const interval = window.setInterval(() => void refresh(false), 5000);
    const refreshOnFocus = () => {
      if (document.visibilityState !== "hidden") void refresh(false);
    };
    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnFocus);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnFocus);
    };
  }, [activeSection, token]);

  useEffect(() => {
    try {
      window.localStorage.setItem(LOCAL_PREFS_STORAGE_KEY, JSON.stringify(localPrefs));
    } catch {
      // Browser-only preferences should never block settings.
    }
  }, [localPrefs]);

  useEffect(() => {
    if (!settings) return;
    setProviderForms((prev) => {
      const next = { ...prev };
      for (const provider of settings.providers) {
        next[provider.name] = {
          apiKey: next[provider.name]?.apiKey ?? "",
          apiBase: next[provider.name]?.apiBase ?? provider.api_base ?? provider.default_api_base ?? "",
          apiType: next[provider.name]?.apiType ?? provider.api_type ?? "auto",
        };
      }
      return next;
    });
  }, [settings]);

  const modelDirty = useMemo(() => {
    if (!settings) return false;
    const activePresetName = modelPresetValue(settings);
    const selectedPreset = settings.model_presets.find((preset) => preset.name === form.modelPreset);
    if (!selectedPreset) return form.modelPreset !== activePresetName;
    const selectedProvider = selectedPreset.is_default
      ? editableDefaultProvider(settings)
      : selectedPreset.provider;
    return (
      form.modelPreset !== activePresetName ||
      form.model !== selectedPreset.model ||
      form.provider !== selectedProvider ||
      form.contextWindowTokens !== normalizeContextWindowTokens(selectedPreset.context_window_tokens) ||
      (!selectedPreset.is_default && form.presetLabel.trim() !== selectedPreset.label)
    );
  }, [form, settings]);

  const runtimeDirty = useMemo(() => {
    if (!settings) return false;
    return (
      form.timezone !== settings.agent.timezone ||
      form.botName !== settings.agent.bot_name ||
      form.botIcon !== settings.agent.bot_icon
    );
  }, [form, settings]);

  const imageGenerationDirty = useMemo(() => {
    if (!settings) return false;
    return (
      imageGenerationForm.enabled !== settings.image_generation.enabled ||
      imageGenerationForm.provider !== settings.image_generation.provider ||
      imageGenerationForm.model !== settings.image_generation.model ||
      imageGenerationForm.defaultAspectRatio !== settings.image_generation.default_aspect_ratio ||
      imageGenerationForm.defaultImageSize !== settings.image_generation.default_image_size ||
      imageGenerationForm.maxImagesPerTurn !== settings.image_generation.max_images_per_turn
    );
  }, [imageGenerationForm, settings]);

  const transcriptionDirty = useMemo(() => {
    if (!settings) return false;
    const transcription = settings.transcription ?? DEFAULT_TRANSCRIPTION_SETTINGS;
    return (
      transcriptionForm.enabled !== transcription.enabled ||
      transcriptionForm.provider !== transcription.provider ||
      transcriptionForm.model !== transcription.model ||
      transcriptionForm.language !== (transcription.language ?? "") ||
      transcriptionForm.maxDurationSec !== transcription.max_duration_sec ||
      transcriptionForm.maxUploadMb !== transcription.max_upload_mb
    );
  }, [settings, transcriptionForm]);

  const networkSafetyDirty = useMemo(() => {
    if (!settings) return false;
    const currentLocalServiceAccess =
      settings.advanced.webui_allow_local_service_access ?? settings.advanced.allow_local_preview_access ?? true;
    const currentDefaultAccess = visibleWebuiDefaultAccessMode(settings.advanced.webui_default_access_mode);
    return (
      networkSafetyForm.webuiAllowLocalServiceAccess !== currentLocalServiceAccess ||
      networkSafetyForm.webuiDefaultAccessMode !== currentDefaultAccess
    );
  }, [networkSafetyForm, settings]);

  const skillGovernanceDirty = useMemo(() => {
    if (!settings) return false;
    const current = skillGovernanceFormFromPayload(settings);
    return JSON.stringify(skillGovernanceForm) !== JSON.stringify(current);
  }, [settings, skillGovernanceForm]);

  const studentModeDirty = useMemo(() => {
    if (!settings) return false;
    const current = studentModeFormFromPayload(settings);
    return JSON.stringify(studentModeForm) !== JSON.stringify(current);
  }, [settings, studentModeForm]);

  const configuredModelProviderOptions = useMemo(
    () =>
      settings?.providers
        .filter((provider) => provider.configured && provider.model_selectable !== false)
        .map((provider) => ({ name: provider.name, label: provider.label })) ?? [],
    [settings],
  );

  const hasPendingRestart = useMemo(
    () =>
      !!settings?.requires_restart ||
      pendingRestartSections.runtime ||
      pendingRestartSections.browser ||
      pendingRestartSections.image,
    [pendingRestartSections, settings?.requires_restart],
  );

  const restartViaSettingsSurface = useCallback(async () => {
    const isNativeHost = (settings?.surface ?? settings?.runtime_surface) === "native";
    if (
      isNativeHost &&
      settings?.runtime_capabilities?.can_restart_engine &&
      onNativeEngineRestart
    ) {
      setHostEngineApplying(true);
      try {
        const nextToken = await onNativeEngineRestart();
        const payload = await fetchSettings(nextToken);
        applyPayload(payload);
        setPendingRestartSections(EMPTY_PENDING_RESTART_SECTIONS);
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setHostEngineApplying(false);
      }
      return;
    }
    onRestart?.();
  }, [applyPayload, onNativeEngineRestart, onRestart, settings]);

  const maybeRestartHostEngine = useCallback(
    async (payload: RestartAwarePayload) => {
      const surface = payload.surface ?? payload.runtime_surface ?? settings?.surface ?? settings?.runtime_surface;
      const capabilities = payload.runtime_capabilities ?? settings?.runtime_capabilities;
      const isNativeHost = surface === "native";
      if (
        !payload.requires_restart ||
        !isNativeHost ||
        !capabilities?.can_restart_engine ||
        !onNativeEngineRestart
      ) {
        return;
      }
      setHostEngineApplying(true);
      try {
        const nextToken = await onNativeEngineRestart();
        const refreshed = await fetchSettings(nextToken);
        applyPayload(refreshed);
        setPendingRestartSections(EMPTY_PENDING_RESTART_SECTIONS);
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setHostEngineApplying(false);
      }
    },
    [applyPayload, onNativeEngineRestart, settings],
  );

  const saveModelSettings = async () => {
    if (!settings || !modelDirty || saving) return;
    setSaving(true);
    try {
      const selectedPreset = settings.model_presets.find((preset) => preset.name === form.modelPreset);
      let payload: SettingsPayload;
      if (selectedPreset && !selectedPreset.is_default) {
        payload = await updateModelConfiguration(token, {
          name: selectedPreset.name,
          label: form.presetLabel.trim(),
          model: form.model,
          provider: form.provider,
          ...(form.contextWindowTokens !== selectedPreset.context_window_tokens
            ? { contextWindowTokens: form.contextWindowTokens }
            : {}),
        });
      } else {
        const defaultModel = defaultPreset(settings)?.model ?? settings.agent.model;
        const defaultProvider = editableDefaultProvider(settings);
        const defaultContextWindowTokens = normalizeContextWindowTokens(
          defaultPreset(settings)?.context_window_tokens ?? settings.agent.context_window_tokens,
        );
        payload = await updateSettings(token, {
          modelPreset: form.modelPreset,
          ...(form.model !== defaultModel ? { model: form.model } : {}),
          ...(form.provider !== defaultProvider ? { provider: form.provider } : {}),
          ...(form.contextWindowTokens !== defaultContextWindowTokens
            ? { contextWindowTokens: form.contextWindowTokens }
            : {}),
        });
      }
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const openModelConfigurationDialog = () => {
    if (!settings) return;
    const currentProvider = settings.agent.provider;
    const provider =
      configuredModelProviderOptions.find((option) => option.name === currentProvider)?.name ??
      configuredModelProviderOptions[0]?.name ??
      "";
    setModelConfigurationForm({
      label: "",
      provider,
      model: "",
    });
    setModelConfigurationOpen(true);
  };

  const handleCreateModelConfiguration = async () => {
    if (modelConfigurationSaving) return;
    const label = modelConfigurationForm.label.trim();
    const provider = modelConfigurationForm.provider.trim();
    const model = modelConfigurationForm.model.trim();
    if (!label || !provider || !model) return;
    setModelConfigurationSaving(true);
    try {
      const payload = await createModelConfiguration(token, {
        label,
        provider,
        model,
      });
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setModelConfigurationOpen(false);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setModelConfigurationSaving(false);
    }
  };

  const saveRuntimeSettings = async () => {
    if (!settings || !runtimeDirty || saving) return;
    setSaving(true);
    try {
      const payload = await updateSettings(token, {
        timezone: form.timezone,
        botName: form.botName,
        botIcon: form.botIcon,
      });
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await onWorkspaceSettingsChange?.();
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const saveImageGenerationSettings = async () => {
    if (!settings || !imageGenerationDirty || imageGenerationSaving) return;
    setImageGenerationSaving(true);
    try {
      const payload = await updateImageGenerationSettings(token, imageGenerationForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, image: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setImageGenerationSaving(false);
    }
  };

  const saveTranscriptionSettings = async () => {
    if (!settings || !transcriptionDirty || transcriptionSaving) return;
    setTranscriptionSaving(true);
    try {
      const payload = await updateTranscriptionSettings(token, transcriptionForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, browser: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTranscriptionSaving(false);
    }
  };

  const saveNetworkSafetySettings = async () => {
    if (!settings || !networkSafetyDirty || networkSafetySaving) return;
    setNetworkSafetySaving(true);
    try {
      const payload = await updateNetworkSafetySettings(token, networkSafetyForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setNetworkSafetySaving(false);
    }
  };

  const saveSkillGovernanceSettings = async () => {
    if (!settings || !skillGovernanceDirty || skillGovernanceSaving) return;
    setSkillGovernanceSaving(true);
    try {
      const payload = await updateSkillGovernanceSettings(token, skillGovernanceForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSkillGovernanceSaving(false);
    }
  };

  const saveStudentModeSettings = async () => {
    if (!settings || !studentModeDirty || studentModeSaving) return;
    setStudentModeSaving(true);
    try {
      const payload = await updateStudentModeSettings(token, studentModeForm);
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setStudentModeSaving(false);
    }
  };

  const saveProvider = async (providerName: string) => {
    if (providerSaving) return;
    const provider = settings?.providers.find((item) => item.name === providerName);
    if (!provider) return;
    if (provider.auth_type === "oauth") return;
    const providerForm = providerForms[providerName] ?? { apiKey: "", apiBase: "", apiType: "auto" };
    const apiKey = providerForm.apiKey.trim();
    const apiKeyRequired = provider.api_key_required ?? true;
    if (!provider.configured && apiKeyRequired && !apiKey) {
      setError(t("settings.byok.apiKeyRequired"));
      return;
    }
    setProviderSaving(providerName);
    try {
      const payload = await updateProviderSettings(token, {
        provider: providerName,
        apiKey: apiKey || undefined,
        apiBase: providerForm.apiBase.trim(),
        apiType: providerForm.apiType,
      });
      applyPayload(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, image: true }));
      }
      await maybeRestartHostEngine(payload);
      setProviderForms((prev) => ({
        ...prev,
        [providerName]: {
          apiKey: "",
          apiBase: providerForm.apiBase.trim(),
          apiType: providerForm.apiType,
        },
      }));
      setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProviderSaving(null);
    }
  };

  const runProviderOAuth = async (providerName: string, action: "login" | "logout") => {
    if (providerSaving) return;
    setProviderSaving(providerName);
    try {
      const payload =
        action === "login"
          ? await loginProviderOAuth(token, providerName)
          : await logoutProviderOAuth(token, providerName);
      applyPayload(payload);
      setExpandedProvider(providerName);
      setError(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(
        action === "login" && /timed out/i.test(message)
          ? t("settings.byok.oauth.loginTimedOut", {
              defaultValue:
                "Sign-in didn't finish in time. Check whether you completed it in the browser, then try again.",
            })
          : message,
      );
    } finally {
      setProviderSaving(null);
    }
  };

  const saveWebSearch = async () => {
    if (!settings || webSearchSaving) return;
    const provider = settings.web_search.providers.find((item) => item.name === webSearchForm.provider);
    if (!provider) return;
    const apiKey = webSearchForm.apiKey?.trim() ?? "";
    const baseUrl = webSearchForm.baseUrl?.trim() ?? "";
    const hasExistingSecret =
      webSearchProviderAcceptsApiKey(provider) &&
      webSearchForm.provider === settings.web_search.provider &&
      !!settings.web_search.api_key_hint;

    if (webSearchProviderRequiresApiKey(provider) && !apiKey && !hasExistingSecret) {
      setError(t("settings.byok.webSearch.apiKeyRequired"));
      return;
    }
    if (provider.credential === "base_url" && !baseUrl) {
      setError(t("settings.byok.webSearch.baseUrlRequired"));
      return;
    }

    setWebSearchSaving(true);
    try {
      const webFetchRestartRequired =
        (webSearchForm.useJinaReader ?? settings.web.fetch.use_jina_reader) !==
        settings.web.fetch.use_jina_reader;
      const update: WebSearchSettingsUpdate = {
        provider: webSearchForm.provider,
        maxResults: webSearchForm.maxResults,
        timeout: webSearchForm.timeout,
        useJinaReader: webSearchForm.useJinaReader,
      };
      if (
        webSearchProviderAcceptsApiKey(provider) &&
        (apiKey || (provider.credential === "optional_api_key" && webSearchKeyEditing))
      ) {
        update.apiKey = apiKey;
      }
      if (provider.credential === "base_url") update.baseUrl = baseUrl;
      const payload = await updateWebSearchSettings(token, update);
      applyPayload(payload);
      if (payload.requires_restart || webFetchRestartRequired) {
        setPendingRestartSections((prev) => ({ ...prev, browser: true }));
      }
      await maybeRestartHostEngine(payload);
      setWebSearchForm((prev) => ({
        provider: payload.web_search.provider,
        apiKey: "",
        baseUrl: payload.web_search.base_url ?? prev.baseUrl ?? "",
        maxResults: payload.web_search.max_results,
        timeout: payload.web_search.timeout,
        useJinaReader: payload.web.fetch.use_jina_reader,
      }));
      setWebSearchKeyVisible(false);
      setWebSearchKeyEditing(false);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setWebSearchSaving(false);
    }
  };

  const resetProviderDraft = useCallback((providerName: string) => {
    const provider = settings?.providers.find((item) => item.name === providerName);
    if (!provider) return;
    setProviderForms((prev) => ({
      ...prev,
      [providerName]: {
        apiKey: "",
        apiBase: provider.api_base ?? provider.default_api_base ?? "",
        apiType: provider.api_type ?? "auto",
      },
    }));
    setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
    setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
  }, [settings]);

  const handleToggleProvider = useCallback((providerName: string) => {
    if (expandedProvider) resetProviderDraft(expandedProvider);
    setExpandedProvider(expandedProvider === providerName ? null : providerName);
  }, [expandedProvider, resetProviderDraft]);

  const resetWebSearchDraft = useCallback(() => {
    if (!settings) return;
    setWebSearchForm({
      provider: settings.web_search.provider,
      apiKey: "",
      baseUrl: settings.web_search.base_url ?? "",
      maxResults: settings.web_search.max_results,
      timeout: settings.web_search.timeout,
      useJinaReader: settings.web.fetch.use_jina_reader,
    });
    setWebSearchKeyVisible(false);
    setWebSearchKeyEditing(false);
  }, [settings]);

  const handleWebSearchProviderChange = useCallback((provider: string) => {
    if (!settings) return;
    setWebSearchForm((prev) => ({
      provider,
      apiKey: "",
      baseUrl: provider === settings.web_search.provider ? settings.web_search.base_url ?? "" : "",
      maxResults: prev.maxResults ?? settings.web_search.max_results,
      timeout: prev.timeout ?? settings.web_search.timeout,
      useJinaReader: prev.useJinaReader ?? settings.web.fetch.use_jina_reader,
    }));
    setWebSearchKeyVisible(false);
    setWebSearchKeyEditing(false);
  }, [settings]);

  const toggleProviderKeyVisibility = (providerName: string) => {
    const isVisible = visibleProviderKeys[providerName];
    setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: !isVisible }));
  };

  const toggleProviderKeyEditing = (providerName: string) => {
    setEditingProviderKeys((prev) => {
      const nextEditing = !prev[providerName];
      if (!nextEditing) {
        setProviderForms((forms) => ({
          ...forms,
          [providerName]: {
            apiKey: "",
            apiBase: forms[providerName]?.apiBase ?? "",
            apiType: forms[providerName]?.apiType ?? "auto",
          },
        }));
        setVisibleProviderKeys((visible) => ({ ...visible, [providerName]: false }));
      }
      return { ...prev, [providerName]: nextEditing };
    });
  };

  const handleCliAppAction = async (
    action: "install" | "update" | "uninstall" | "test",
    name: string,
  ) => {
    const key = `${action}:${name}`;
    setCliAppsAction(key);
    setCliAppsMessage(null);
    setCliAppsError(null);
    try {
      const payload = await runCliAppAction(token, action, name);
      setCliApps(payload);
      if (action !== "test") {
        notifyCliAppsChanged(payload);
      }
      setCliAppsMessage(payload.last_action?.message ?? null);
      setCliAppsFocusName(action === "uninstall" ? null : name);
    } catch (err) {
      setCliAppsError((err as Error).message);
    } finally {
      setCliAppsAction(null);
    }
  };

  const handleNanobotFeatureAction = async (
    action: "enable" | "disable",
    name: string,
    confirmed = false,
  ) => {
    const feature = nanobotFeatures?.features.find((item) => item.name === name);
    if (action === "enable" && !confirmed && feature && !feature.installed && feature.install_supported) {
      setNanobotFeaturesMessage(null);
      setNanobotFeaturesError(null);
      setNanobotFeatureConfirm(feature);
      return;
    }
    const key = `${action}:${name}`;
    setNanobotFeatureAction(key);
    setNanobotFeatureConfirm(null);
    setNanobotFeaturesMessage(null);
    setNanobotFeaturesError(null);
    try {
      const payload = action === "enable"
        ? await enableNanobotFeature(token, name)
        : await disableNanobotFeature(token, name);
      setNanobotFeatures(payload);
      setNanobotFeaturesMessage(payload.last_action?.message ?? null);
      if (
        payload.requires_restart ||
        payload.features.some((feature) => feature.name === name && feature.requires_restart)
      ) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
    } catch (err) {
      setNanobotFeaturesError((err as Error).message);
    } finally {
      setNanobotFeatureAction(null);
    }
  };

  const handleAutomationAction = async (
    action: AutomationAction,
    job: SessionAutomationJob,
  ) => {
    const key = `${action}:${job.id}`;
    setAutomationAction(key);
    setAutomationsError(null);
    try {
      const payload = await runAutomationAction(token, action, job.id);
      setAutomations(payload);
      if (action === "delete") setAutomationPendingDelete(null);
      if (action === "run") {
        window.setTimeout(() => void refreshAutomations(false), 1200);
        window.setTimeout(() => void refreshAutomations(false), 4000);
      }
    } catch (err) {
      setAutomationsError((err as Error).message);
    } finally {
      setAutomationAction(null);
    }
  };

  const handleAutomationEdit = async (
    job: SessionAutomationJob,
    values: AutomationUpdatePayload,
  ) => {
    const key = `update:${job.id}`;
    setAutomationAction(key);
    setAutomationsError(null);
    try {
      const payload = await updateAutomation(token, job.id, values);
      setAutomations(payload);
      setAutomationPendingEdit(null);
    } catch (err) {
      setAutomationsError((err as Error).message);
    } finally {
      setAutomationAction(null);
    }
  };

  const handleMcpPresetAction = async (
    action: "enable" | "remove" | "test",
    name: string,
    values: Record<string, string> = {},
  ) => {
    const key = `${action}:${name}`;
    setMcpPresetAction(key);
    setMcpMessage(null);
    setMcpError(null);
    try {
      const payload = await runMcpPresetAction(token, action, name, values);
      setMcpPresets(payload);
      setMcpMessage(payload.last_action?.message ?? null);
      if (action !== "test") {
        notifyMcpPresetsChanged(payload);
      }
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      if (action === "enable") {
        setMcpFieldValues((prev) => ({ ...prev, [name]: {} }));
      }
    } catch (err) {
      setMcpError((err as Error).message);
    } finally {
      setMcpPresetAction(null);
    }
  };

  const handleSaveCustomMcp = async () => {
    const name = customMcpForm.name.trim();
    const key = `custom:${name || "new"}`;
    setMcpPresetAction(key);
    setMcpMessage(null);
    setMcpError(null);
    try {
      const payload = await saveCustomMcpServer(token, {
        name,
        transport: customMcpForm.transport,
        command: customMcpForm.command,
        args: customMcpForm.args,
        url: customMcpForm.url,
        env: customMcpForm.env,
        headers: customMcpForm.headers,
        tool_timeout: customMcpForm.toolTimeout,
      });
      setMcpPresets(payload);
      setMcpMessage(payload.last_action?.message ?? null);
      notifyMcpPresetsChanged(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setCustomMcpForm((prev) => ({ ...DEFAULT_CUSTOM_MCP_FORM, transport: prev.transport }));
    } catch (err) {
      setMcpError((err as Error).message);
    } finally {
      setMcpPresetAction(null);
    }
  };

  const handleImportMcpConfig = async () => {
    setMcpPresetAction("import");
    setMcpMessage(null);
    setMcpError(null);
    try {
      const payload = await importMcpConfig(token, mcpConfigImport);
      setMcpPresets(payload);
      setMcpMessage(payload.last_action?.message ?? null);
      notifyMcpPresetsChanged(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
      setMcpConfigImport("");
    } catch (err) {
      setMcpError((err as Error).message);
    } finally {
      setMcpPresetAction(null);
    }
  };

  const handleMcpToolsChange = async (name: string, enabledTools: string[]) => {
    setMcpPresetAction(`tools:${name}`);
    setMcpMessage(null);
    setMcpError(null);
    try {
      const payload = await updateMcpServerTools(token, name, enabledTools);
      setMcpPresets(payload);
      setMcpMessage(payload.last_action?.message ?? null);
      notifyMcpPresetsChanged(payload);
      if (payload.requires_restart) {
        setPendingRestartSections((prev) => ({ ...prev, runtime: true }));
      }
      await maybeRestartHostEngine(payload);
    } catch (err) {
      setMcpError((err as Error).message);
    } finally {
      setMcpPresetAction(null);
    }
  };

  const renderSection = () => {
    if (!settings) return null;
    switch (activeSection) {
      case "overview":
        return (
          <OverviewSettings
            settings={settings}
            requiresRestart={hasPendingRestart}
            showBrandLogos={localPrefs.brandLogos}
            onSelectSection={selectSection}
          />
        );
      case "easy-setup":
        return (
          <EasySetupWizard
            token={token}
            settings={settings}
            form={form}
            setForm={setForm}
            modelDirty={modelDirty}
            savingModel={saving}
            showBrandLogos={localPrefs.brandLogos}
            providerSaving={providerSaving}
            providerForms={providerForms}
            visibleProviderKeys={visibleProviderKeys}
            editingProviderKeys={editingProviderKeys}
            onChangeProviderForm={(provider, value) =>
              setProviderForms((prev) => ({
                ...prev,
                [provider]: {
                  apiKey: prev[provider]?.apiKey ?? "",
                  apiBase: prev[provider]?.apiBase ?? "",
                  apiType: prev[provider]?.apiType ?? "auto",
                  ...value,
                },
              }))
            }
            onToggleProviderKey={toggleProviderKeyVisibility}
            onToggleProviderKeyEditing={toggleProviderKeyEditing}
            onSaveProvider={saveProvider}
            onProviderOAuthLogin={(provider) => runProviderOAuth(provider, "login")}
            onSaveModel={saveModelSettings}
            nanobotFeatures={nanobotFeatures}
            nanobotFeaturesLoading={nanobotFeaturesLoading}
            nanobotFeatureAction={nanobotFeatureAction}
            onNanobotAction={(action, name) => handleNanobotFeatureAction(action, name)}
            skills={skills}
            onSettingsChange={applyPayload}
            onSelectSection={selectSection}
            onBackToChat={onBackToChat}
          />
        );
      case "appearance":
        return (
          <AppearanceSettings
            theme={theme}
            onToggleTheme={onToggleTheme}
            localPrefs={localPrefs}
            onChangeLocalPrefs={setLocalPrefs}
          />
        );
      case "models":
        return (
          <div className="space-y-8">
            <ModelsSettings
              token={token}
              form={form}
              setForm={setForm}
              settings={settings}
              dirty={modelDirty}
              saving={saving}
              showBrandLogos={localPrefs.brandLogos}
              providerSaving={providerSaving}
              onProviderOAuthLogin={(provider) => runProviderOAuth(provider, "login")}
              onSave={saveModelSettings}
              onCreateConfiguration={openModelConfigurationDialog}
            />
            <ProvidersSettings
              settings={settings}
              expandedProvider={expandedProvider}
              providerForms={providerForms}
              visibleProviderKeys={visibleProviderKeys}
              editingProviderKeys={editingProviderKeys}
              providerSaving={providerSaving}
              query={providerQuery}
              showBrandLogos={localPrefs.brandLogos}
              onQueryChange={setProviderQuery}
              onToggleProvider={handleToggleProvider}
              onToggleProviderKey={toggleProviderKeyVisibility}
              onToggleProviderKeyEditing={toggleProviderKeyEditing}
              onChangeProviderForm={(provider, value) =>
                setProviderForms((prev) => ({
                  ...prev,
                  [provider]: {
                    apiKey: prev[provider]?.apiKey ?? "",
                    apiBase: prev[provider]?.apiBase ?? "",
                    apiType: prev[provider]?.apiType ?? "auto",
                    ...value,
                  },
                }))
              }
              onSaveProvider={saveProvider}
              onProviderOAuthLogin={(provider) => runProviderOAuth(provider, "login")}
              onProviderOAuthLogout={(provider) => runProviderOAuth(provider, "logout")}
              onResetProviderDraft={resetProviderDraft}
              imageProviderRestartPending={pendingRestartSections.image}
              onRestart={restartViaSettingsSurface}
              isRestarting={isRestarting || hostEngineApplying}
            />
          </div>
        );
      case "image":
        return (
          <ImageGenerationSettings
            settings={settings}
            form={imageGenerationForm}
            dirty={imageGenerationDirty}
            saving={imageGenerationSaving}
            onChangeForm={setImageGenerationForm}
            onSave={saveImageGenerationSettings}
            onOpenProviders={() => selectSection("models")}
            showBrandLogos={localPrefs.brandLogos}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
            requiresRestartPending={pendingRestartSections.image}
          />
        );
      case "voice":
        return (
          <TranscriptionSettings
            settings={settings}
            form={transcriptionForm}
            dirty={transcriptionDirty}
            saving={transcriptionSaving}
            onChangeForm={setTranscriptionForm}
            onSave={saveTranscriptionSettings}
            onOpenProviders={() => selectSection("models")}
            showBrandLogos={localPrefs.brandLogos}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
            requiresRestartPending={pendingRestartSections.browser}
          />
        );
      case "browser":
        return (
          <WebSettings
            settings={settings}
            form={webSearchForm}
            keyVisible={webSearchKeyVisible}
            keyEditing={webSearchKeyEditing}
            saving={webSearchSaving}
            onChangeForm={setWebSearchForm}
            onChangeProvider={handleWebSearchProviderChange}
            onToggleKey={() => setWebSearchKeyVisible((visible) => !visible)}
            onToggleKeyEditing={() => {
              setWebSearchKeyEditing((editing) => !editing);
              setWebSearchKeyVisible(false);
              setWebSearchForm((prev) => ({ ...prev, apiKey: "" }));
            }}
            onReset={resetWebSearchDraft}
            onSave={saveWebSearch}
            showBrandLogos={localPrefs.brandLogos}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
            requiresRestartPending={pendingRestartSections.browser}
          />
        );
      case "apps":
        return (
          <div className="space-y-6">
          <AppsCatalogSettings
            cliApps={cliApps}
            nanobotFeatures={nanobotFeatures}
            mcpPresets={mcpPresets}
            cliAppsLoading={cliAppsLoading}
            nanobotFeaturesLoading={nanobotFeaturesLoading}
            mcpPresetsLoading={mcpPresetsLoading}
            query={appsQuery}
            filter={appsKindFilter}
            cliActionKey={cliAppsAction}
            nanobotActionKey={nanobotFeatureAction}
            mcpActionKey={mcpPresetAction}
            cliMessage={cliAppsMessage}
            cliError={cliAppsError}
            nanobotMessage={nanobotFeaturesMessage}
            nanobotError={nanobotFeaturesError}
            cliFocusName={cliAppsFocusName}
            mcpMessage={mcpMessage}
            mcpError={mcpError}
            mcpFieldValues={mcpFieldValues}
            customMcpForm={customMcpForm}
            mcpConfigImport={mcpConfigImport}
            showBrandLogos={localPrefs.brandLogos}
            requiresRestartPending={pendingRestartSections.runtime}
            onQueryChange={setAppsQuery}
            onFilterChange={setAppsKindFilter}
            onCliAction={handleCliAppAction}
            onNanobotAction={handleNanobotFeatureAction}
            onMcpAction={handleMcpPresetAction}
            onDismissStatus={() => {
              setCliAppsMessage(null);
              setCliAppsError(null);
              setNanobotFeaturesMessage(null);
              setNanobotFeaturesError(null);
              setMcpMessage(null);
              setMcpError(null);
            }}
            onBackToChat={onBackToChat}
            onMcpFieldChange={(presetName, fieldName, value) => {
              setMcpFieldValues((prev) => ({
                ...prev,
                [presetName]: {
                  ...(prev[presetName] ?? {}),
                  [fieldName]: value,
                },
              }));
            }}
            onCustomMcpFormChange={setCustomMcpForm}
            onMcpConfigImportChange={setMcpConfigImport}
            onSaveCustomMcp={handleSaveCustomMcp}
            onImportMcpConfig={handleImportMcpConfig}
            onMcpToolsChange={handleMcpToolsChange}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
          />
            <div>
              <h3 className="mb-2 text-[13px] font-medium text-foreground">
                {t("settings.apps.programsHeading", { defaultValue: "Programs" })}
              </h3>
              <InstalledToolsSettings installedTools={installedTools} />
            </div>
          </div>
        );
      case "automations":
        return (
          <AutomationsSettings
            payload={automations}
            loading={automationsLoading}
            query={automationsQuery}
            filter={automationsFilter}
            sort={automationsSort}
            actionKey={automationAction}
            error={automationsError}
            onQueryChange={setAutomationsQuery}
            onFilterChange={setAutomationsFilter}
            onSortChange={setAutomationsSort}
            onAction={handleAutomationAction}
            onRequestEdit={setAutomationPendingEdit}
            onRequestDelete={setAutomationPendingDelete}
          />
        );
      case "skills":
        return <SkillsCatalogSettings skills={skills} />;
      case "tools":
        return <AgentToolsSettings settings={settings} onSettingsChange={applyPayload} />;
      case "agent-management":
        return <AgentManagementSettings settings={settings} />;
      case "runtime":
        return (
          <RuntimeSettings
            form={form}
            setForm={setForm}
            settings={settings}
            dirty={runtimeDirty}
            saving={saving}
            onSave={saveRuntimeSettings}
            onRestart={restartViaSettingsSurface}
            isRestarting={isRestarting || hostEngineApplying}
            requiresRestartPending={pendingRestartSections.runtime}
          />
        );
      case "advanced":
        return (
          <div className="space-y-6">
            <AdvancedSettings
              form={networkSafetyForm}
              dirty={networkSafetyDirty}
              saving={networkSafetySaving}
              isNativeHostSurface={(settings.surface ?? settings.runtime_surface) === "native"}
              onChangeForm={setNetworkSafetyForm}
              onSave={saveNetworkSafetySettings}
              onRestart={restartViaSettingsSurface}
              isRestarting={isRestarting || hostEngineApplying}
              requiresRestartPending={pendingRestartSections.runtime}
            />
            <div className="grid gap-6 xl:grid-cols-2">
              <SkillGovernanceQuickPanel
                form={skillGovernanceForm}
                dirty={skillGovernanceDirty}
                saving={skillGovernanceSaving}
                onChange={setSkillGovernanceForm}
                onSave={saveSkillGovernanceSettings}
              />
              <StudentModeQuickPanel
                form={studentModeForm}
                dirty={studentModeDirty}
                saving={studentModeSaving}
                onChange={setStudentModeForm}
                onSave={saveStudentModeSettings}
              />
            </div>
          </div>
        );
      default:
        return null;
    }
  };

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col overflow-hidden md:flex-row",
        showSidebar
          ? "bg-[radial-gradient(circle_at_50%_0%,hsl(var(--muted))_0%,hsl(var(--background))_42%)]"
          : "bg-background",
      )}
    >
      {showSidebar ? (
        <SettingsSidebar
          activeSection={activeSection}
          onSelectSection={selectSection}
          onBackToChat={onBackToChat}
          onLogout={onLogout}
          hostChromeInset={hostChromeInset}
          showAdvanced={localPrefs.showAdvancedSettings}
          onToggleAdvanced={(showAdvancedSettings) =>
            setLocalPrefs((prev) => ({ ...prev, showAdvancedSettings }))
          }
        />
      ) : null}

      <NewModelConfigurationDialog
        open={modelConfigurationOpen}
        draft={modelConfigurationForm}
        providers={configuredModelProviderOptions}
        saving={modelConfigurationSaving}
        showProviderLogos={localPrefs.brandLogos}
        onOpenChange={setModelConfigurationOpen}
        onChangeDraft={setModelConfigurationForm}
        onSave={handleCreateModelConfiguration}
      />

      <NanobotFeatureInstallDialog
        feature={nanobotFeatureConfirm}
        installing={nanobotFeatureAction === `enable:${nanobotFeatureConfirm?.name ?? ""}`}
        onOpenChange={(open) => {
          if (!open) setNanobotFeatureConfirm(null);
        }}
        onConfirm={(feature) => handleNanobotFeatureAction("enable", feature.name, true)}
      />

      <AutomationDeleteDialog
        job={automationPendingDelete}
        deleting={automationAction === `delete:${automationPendingDelete?.id ?? ""}`}
        onOpenChange={(open) => {
          if (!open) setAutomationPendingDelete(null);
        }}
        onConfirm={(job) => handleAutomationAction("delete", job)}
      />

      <AutomationEditDialog
        job={automationPendingEdit}
        saving={automationAction === `update:${automationPendingEdit?.id ?? ""}`}
        onOpenChange={(open) => {
          if (!open) setAutomationPendingEdit(null);
        }}
        onSave={handleAutomationEdit}
      />

      <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <div
          className={cn(
            "mx-auto w-full px-4 py-6 sm:px-8 sm:py-8 lg:py-12",
            activeSection === "skills" || activeSection === "tools" || activeSection === "automations"
              ? "max-w-[1720px]"
              : "max-w-[920px]",
            hostChromeInset && "pt-[4.25rem] sm:pt-[4.25rem] lg:pt-[4.75rem]",
          )}
        >
          <div className="mb-7">
            {!showSidebar ? (
              <button
                type="button"
                onClick={onBackToChat}
                className="mb-4 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground lg:hidden"
              >
                <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
                {t("settings.backToChat")}
              </button>
            ) : null}
            {showSidebar ? (
              <p className="mb-2 text-[12px] font-normal text-muted-foreground">
                {t("settings.sidebar.title")}
              </p>
            ) : null}
            <h1 className="text-[24px] font-normal leading-tight tracking-normal text-foreground sm:text-[28px]">
              {text(`settings.nav.${activeSection}`, titleForSection(activeSection))}
            </h1>
            {!showSidebar && PAGE_INTRO_FALLBACK[activeSection] ? (
              <p className="mt-1.5 text-[13.5px] text-muted-foreground">
                {text(`settings.pageIntros.${activeSection}`, PAGE_INTRO_FALLBACK[activeSection]!)}
              </p>
            ) : null}
          </div>

          {loading ? (
            <div className="flex h-48 items-center justify-center rounded-[24px] border border-border/50 bg-card/75 text-sm text-muted-foreground shadow-[0_20px_70px_rgba(15,23,42,0.07)]">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {t("settings.status.loading")}
            </div>
          ) : error && !settings ? (
            <SettingsGroup>
              <SettingsRow title={t("settings.status.loadError")}>
                <span className="max-w-[520px] text-sm text-muted-foreground">{error}</span>
              </SettingsRow>
            </SettingsGroup>
          ) : settings ? (
            <div className="space-y-5">
              {error ? (
                <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
                  {error}
                </div>
              ) : null}
              {renderSection()}
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}

type SettingsNavTier = "basic" | "advanced";

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

function visibleWebuiDefaultAccessMode(mode: string | null | undefined): WebuiDefaultAccessMode {
  return mode === "full" ? "full" : "default";
}

const PAGE_INTRO_FALLBACK: Partial<Record<SettingsSectionKey, string>> = {
  apps: "Manage messengers, external services (MCP), and connected programs in one place.",
  skills: "Choose how your agent works — turn on the skills you need, or create a new one.",
  tools: "Tools are your agent's hands. Turn nanobot's built-in abilities on or off here.",
  automations: "Ask your agent in plain language and scheduled automations will show up here.",
  "agent-management": "Create dedicated agents suited to specific roles.",
};

function titleForSection(section: SettingsSectionKey): string {
  return SETTINGS_NAV_ITEMS.find((item) => item.key === section)?.fallback ?? "Settings";
}

