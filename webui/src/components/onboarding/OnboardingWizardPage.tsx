import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useClient } from "@/providers/ClientProvider";
import {
  fetchSettings,
  updateProviderSettings,
  updateModelConfiguration,
  loginProviderOAuth,
  updateAgentToolsSettings,
  fetchNanobotFeatures,
  enableNanobotFeature,
  disableNanobotFeature,
  fetchProviderModels,
} from "@/lib/api";
import type { SettingsPayload, NanobotFeatureInfo, NanobotFeaturesPayload } from "@/lib/types";
import "./onboarding-wizard.css";

// The "로컬 모델" card is a category, not one real provider — each local
// backend is its own registered provider name in nanobot/providers/registry.py.
const LOCAL_BACKENDS: Array<{ id: string; name: string; base: string }> = [
  { id: "ollama", name: "Ollama", base: "http://localhost:11434/v1" },
  { id: "lm_studio", name: "LM Studio", base: "http://localhost:1234/v1" },
  { id: "vllm", name: "vLLM", base: "" },
  { id: "sglang", name: "SGLang", base: "http://localhost:30000/v1" },
  { id: "ovms", name: "OpenVINO Model Server", base: "http://localhost:8000/v3" },
  { id: "atomic_chat", name: "Atomic Chat", base: "http://localhost:1337/v1" },
];
const LOCAL_BACKEND_IDS = LOCAL_BACKENDS.map((b) => b.id);

const PRIMARY_PROVIDERS = ["openai", "anthropic", "google"];
const PROVIDER_MARK: Record<string, { icon: string; bg: string; fg: string }> = {
  openai: { icon: "●", bg: "#E4F5F0", fg: "#0E8A6C" },
  anthropic: { icon: "◆", bg: "#FBEAE3", fg: "#C15F3C" },
  google: { icon: "▲", bg: "#E8F0FE", fg: "#3D6FD6" },
  ollama: { icon: "▣", bg: "#EFEAFB", fg: "#6D4FC4" },
};

export function OnboardingWizardPage({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const { token } = useClient();

  const [step, setStep] = useState(1);
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedProviderName, setSelectedProviderName] = useState<string | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [apiBaseInput, setApiBaseInput] = useState("");
  const [providerBusy, setProviderBusy] = useState(false);
  const [otherModal, setOtherModal] = useState<"provider" | "channel" | null>(null);
  const [otherQuery, setOtherQuery] = useState("");
  const [codexMode, setCodexMode] = useState(false);
  const [showLocalPicker, setShowLocalPicker] = useState(false);
  const [availableModels, setAvailableModels] = useState<{ id: string; label?: string }[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);

  const [nanobotFeatures, setNanobotFeatures] = useState<NanobotFeaturesPayload | null>(null);
  const [featuresLoading, setFeaturesLoading] = useState(true);
  const [featureBusyKey, setFeatureBusyKey] = useState<string | null>(null);
  const [selectedChannelName, setSelectedChannelName] = useState<string | null>(null);

  const [preset, setPreset] = useState<string | null>(null);

  const reload = async () => {
    try {
      const payload = await fetchSettings(token);
      setSettings(payload);
      if (!selectedProviderName) {
        const resolved =
          payload.agent.provider === "auto" ? payload.agent.resolved_provider : payload.agent.provider;
        setSelectedProviderName(resolved ?? "openai");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let cancelled = false;
    setFeaturesLoading(true);
    fetchNanobotFeatures(token)
      .then((payload) => {
        if (!cancelled) setNanobotFeatures(payload);
      })
      .catch(() => {
        if (!cancelled) setNanobotFeatures(null);
      })
      .finally(() => {
        if (!cancelled) setFeaturesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const providers = settings?.providers ?? [];
  const primaryProviders = providers.filter((p) => PRIMARY_PROVIDERS.includes(p.name));
  const selectedProvider = providers.find((p) => p.name === selectedProviderName) ?? null;
  const effectiveProviderName = codexMode ? "openai_codex" : selectedProviderName;
  const effectiveProvider = providers.find((p) => p.name === effectiveProviderName) ?? null;
  const providerConfigured = Boolean(effectiveProvider?.configured);

  const channels = (nanobotFeatures?.features ?? []).filter((f) => f.type === "channel");
  const primaryChannelNames = ["telegram", "whatsapp", "slack"];
  const primaryChannels = channels.filter((c) => primaryChannelNames.includes(c.name));
  const selectedChannel = channels.find((c) => c.name === selectedChannelName) ?? null;
  const anyChannelEnabled = channels.some((c) => c.enabled);

  const wireOn = [
    providerConfigured,
    anyChannelEnabled,
    step > 3,
  ];
  const wireStat = [
    providerConfigured ? (settings?.agent.model ?? selectedProvider?.label ?? "") : tx("onboarding.wire.notConnected", "연결 안 됨"),
    anyChannelEnabled
      ? channels.find((c) => c.enabled)?.display_name ?? ""
      : tx("onboarding.wire.notConnected", "연결 안 됨"),
    step > 3 ? tx("onboarding.wire.ready", "설정됨") : tx("onboarding.wire.notPicked", "선택 안 됨"),
  ];

  const goStep = (n: number) => {
    setStep(n);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const pickProvider = (name: string) => {
    setSelectedProviderName(name);
    setShowLocalPicker(false);
    setCodexMode(false);
    setApiKeyInput("");
    setAvailableModels([]);
    setSelectedModelId(null);
    const p = providers.find((x) => x.name === name);
    setApiBaseInput(p?.api_base ?? p?.default_api_base ?? "");
  };

  const pickLocalCard = () => {
    setShowLocalPicker(true);
    setCodexMode(false);
    const current = LOCAL_BACKEND_IDS.includes(selectedProviderName ?? "") ? selectedProviderName! : "ollama";
    pickLocalBackendInner(current);
  };
  const pickLocalBackendInner = (id: string) => {
    setSelectedProviderName(id);
    setApiKeyInput("");
    setAvailableModels([]);
    setSelectedModelId(null);
    const backend = LOCAL_BACKENDS.find((b) => b.id === id);
    const p = providers.find((x) => x.name === id);
    setApiBaseInput(p?.api_base || backend?.base || "");
  };
  const pickLocalBackend = (id: string) => pickLocalBackendInner(id);

  const startCodex = () => {
    setCodexMode(true);
    setError(null);
  };
  const backToApiKey = () => {
    setCodexMode(false);
    setError(null);
  };

  const loadModels = async (providerName: string) => {
    setModelsLoading(true);
    try {
      const payload = await fetchProviderModels(token, providerName);
      const models = payload.models.map((m) => ({ id: m.id, label: m.label ?? m.id }));
      setAvailableModels(models);
      if (models.length) {
        setSelectedModelId(models[0].id);
        if (settings) {
          const defaultPresetName = settings.model_presets?.find((p) => p.is_default)?.name ?? "default";
          await updateModelConfiguration(token, {
            name: defaultPresetName,
            provider: providerName,
            model: models[0].id,
          })
            .then(setSettings)
            .catch(() => undefined);
        }
      }
    } catch {
      setAvailableModels([]);
    } finally {
      setModelsLoading(false);
    }
  };

  const pickModel = async (modelId: string) => {
    setSelectedModelId(modelId);
    if (!settings || !effectiveProvider) return;
    const defaultPresetName = settings.model_presets?.find((p) => p.is_default)?.name ?? "default";
    try {
      const payload = await updateModelConfiguration(token, {
        name: defaultPresetName,
        provider: effectiveProvider.name,
        model: modelId,
      });
      setSettings(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const saveProvider = async () => {
    if (!effectiveProvider) return;
    setProviderBusy(true);
    setError(null);
    try {
      const needsKey = effectiveProvider.api_key_required ?? true;
      if (effectiveProvider.auth_type === "oauth") {
        const payload = await loginProviderOAuth(token, effectiveProvider.name);
        setSettings(payload);
      } else {
        const payload = await updateProviderSettings(token, {
          provider: effectiveProvider.name,
          apiKey: needsKey && apiKeyInput.trim() ? apiKeyInput.trim() : undefined,
          apiBase: apiBaseInput.trim() ? apiBaseInput.trim() : undefined,
        });
        setSettings(payload);
      }
      if (settings) {
        const defaultPresetName = settings.model_presets?.find((p) => p.is_default)?.name ?? "default";
        await updateModelConfiguration(token, {
          name: defaultPresetName,
          provider: effectiveProvider.name,
          model: settings.agent.model,
        }).catch(() => undefined);
      }
      // Real connections need a real model name (this is what the user
      // flagged as missing): after a successful connect, ask the provider
      // what it actually has available and let the user pick from that.
      await loadModels(effectiveProvider.name);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : tx("onboarding.step1.genericError", "연결에 실패했습니다. 다시 시도해주세요."),
      );
    } finally {
      setProviderBusy(false);
    }
  };

  const toggleChannel = async (feature: NanobotFeatureInfo) => {
    const key = `${feature.enabled ? "disable" : "enable"}:${feature.name}`;
    setFeatureBusyKey(key);
    try {
      const payload = feature.enabled
        ? await disableNanobotFeature(token, feature.name)
        : await enableNanobotFeature(token, feature.name);
      setNanobotFeatures(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setFeatureBusyKey(null);
    }
  };

  const [tools, setTools] = useState({
    web_enabled: true,
    file_enabled: true,
    exec_enabled: false,
    cli_apps_enabled: false,
    image_generation_enabled: false,
  });
  useEffect(() => {
    if (settings?.agent_tools) {
      setTools({
        web_enabled: settings.agent_tools.web_enabled,
        file_enabled: settings.agent_tools.file_enabled,
        exec_enabled: settings.agent_tools.exec_enabled,
        cli_apps_enabled: settings.agent_tools.cli_apps_enabled,
        image_generation_enabled: settings.agent_tools.image_generation_enabled,
      });
    }
  }, [settings?.agent_tools]);
  const toolsOnCount = Object.values(tools).filter(Boolean).length;

  const flipTool = async (key: keyof typeof tools) => {
    const next = { ...tools, [key]: !tools[key] };
    setTools(next);
    setPreset(null);
    try {
      const payload = await updateAgentToolsSettings(token, {
        webEnabled: next.web_enabled,
        fileEnabled: next.file_enabled,
        execEnabled: next.exec_enabled,
        cliAppsEnabled: next.cli_apps_enabled,
        imageGenerationEnabled: next.image_generation_enabled,
      });
      setSettings(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const applyPreset = (name: string | null) => {
    setPreset(name);
    const presets: Record<string, typeof tools> = {
      study: { web_enabled: true, file_enabled: true, exec_enabled: false, cli_apps_enabled: false, image_generation_enabled: false },
      work: { web_enabled: true, file_enabled: true, exec_enabled: false, cli_apps_enabled: false, image_generation_enabled: false },
      dev: { web_enabled: true, file_enabled: true, exec_enabled: false, cli_apps_enabled: true, image_generation_enabled: false },
    };
    if (name && presets[name]) {
      setTools(presets[name]);
      void updateAgentToolsSettings(token, {
        webEnabled: presets[name].web_enabled,
        fileEnabled: presets[name].file_enabled,
        execEnabled: presets[name].exec_enabled,
        cliAppsEnabled: presets[name].cli_apps_enabled,
        imageGenerationEnabled: presets[name].image_generation_enabled,
      }).then(setSettings).catch(() => undefined);
    }
  };

  const otherProviders = useMemo(
    () =>
      providers.filter(
        (p) => !PRIMARY_PROVIDERS.includes(p.name) && p.name !== "openai_codex" && !LOCAL_BACKEND_IDS.includes(p.name),
      ),
    [providers],
  );
  const otherChannels = useMemo(
    () => channels.filter((c) => !primaryChannelNames.includes(c.name)),
    [channels],
  );

  if (loading) {
    return (
      <div className="ne-wizard">
        <div className="ne-shell" style={{ alignItems: "center", justifyContent: "center", minHeight: "100%" }}>
          {tx("common.loading", "Loading...")}
        </div>
      </div>
    );
  }

  return (
    <div className="ne-wizard">
      <div className="ne-shell">
        <header className="ne-header">
          <div className="ne-brand">
            <span className="ne-cat">🐈</span> nanobot-easy
          </div>
          <div className="ne-stepcount">
            <b>{Math.min(step, 3)}</b> / 3 · {tx("onboarding.header", "첫 실행 설정")}
          </div>
        </header>
        <div className="ne-body">
          <aside className="ne-aside">
            <span className="ne-lbl">{tx("onboarding.wire.label", "연결 상태")}</span>
            <svg viewBox="0 0 262 108" role="img" aria-label={tx("onboarding.wire.title", "모델·메신저·도구가 에이전트에 연결된 상태")}>
              <path className={`ne-wire${wireOn[0] ? " on" : ""}`} d="M100,18 C142,18 158,55 196,55" />
              <path className={`ne-wire${wireOn[1] ? " on" : ""}`} d="M100,55 L196,55" />
              <path className={`ne-wire${wireOn[2] ? " on" : ""}`} d="M100,92 C142,92 158,55 196,55" />

              <circle className={`ne-ndot${wireOn[0] ? " on" : ""}`} cx="14" cy="18" r="4.5" />
              <text className={`ne-nlabel${wireOn[0] ? " on" : ""}`} x="26" y="15">{tx("onboarding.wire.model", "모델")}</text>
              <text className={`ne-nstat${wireOn[0] ? " on" : ""}`} x="26" y="27">{wireStat[0]}</text>

              <circle className={`ne-ndot${wireOn[1] ? " on" : ""}`} cx="14" cy="55" r="4.5" />
              <text className={`ne-nlabel${wireOn[1] ? " on" : ""}`} x="26" y="52">{tx("onboarding.wire.messenger", "메신저")}</text>
              <text className={`ne-nstat${wireOn[1] ? " on" : ""}`} x="26" y="64">{wireStat[1]}</text>

              <circle className={`ne-ndot${wireOn[2] ? " on" : ""}`} cx="14" cy="92" r="4.5" />
              <text className={`ne-nlabel${wireOn[2] ? " on" : ""}`} x="26" y="89">{tx("onboarding.wire.tools", "도구 · 스킬")}</text>
              <text className={`ne-nstat${wireOn[2] ? " on" : ""}`} x="26" y="101">{wireStat[2]}</text>

              <circle className={`ne-hub${wireOn[0] ? " live" : ""}`} cx="214" cy="55" r="16" />
              <text x="214" y="60" fontSize="13" textAnchor="middle">🐈</text>
            </svg>
            <p className={`ne-hub-caption${wireOn[0] ? " live" : ""}`}>🐈 {tx("onboarding.wire.agent", "에이전트")}</p>
            <p className="ne-aside-note">
              {tx(
                "onboarding.wire.note",
                "세 가지가 모두 이어지면 어디서든 에이전트와 대화할 수 있습니다. 메신저는 나중에 연결해도 됩니다.",
              )}
            </p>
          </aside>

          <main className="ne-main">
            {error ? (
              <p className="ne-hint" style={{ color: "var(--ne-warn)" }}>{error}</p>
            ) : null}
            {step === 1 ? (
              <StepModel
                tx={tx}
                primaryProviders={primaryProviders}
                selectedProviderName={selectedProviderName}
                effectiveProvider={effectiveProvider}
                onPick={pickProvider}
                apiKeyInput={apiKeyInput}
                setApiKeyInput={setApiKeyInput}
                apiBaseInput={apiBaseInput}
                setApiBaseInput={setApiBaseInput}
                providerBusy={providerBusy}
                onSave={saveProvider}
                onOpenOther={() => setOtherModal("provider")}
                onNext={() => goStep(2)}
                providerConfigured={providerConfigured}
                otherSelected={
                  selectedProviderName
                    ? !PRIMARY_PROVIDERS.includes(selectedProviderName) && !showLocalPicker
                    : false
                }
                showLocalPicker={showLocalPicker}
                onPickLocalCard={pickLocalCard}
                onPickLocalBackend={pickLocalBackend}
                codexMode={codexMode}
                onStartCodex={startCodex}
                onBackToApiKey={backToApiKey}
                availableModels={availableModels}
                modelsLoading={modelsLoading}
                selectedModelId={selectedModelId}
                onPickModel={pickModel}
                error={error}
              />
            ) : null}
            {step === 2 ? (
              <StepMessenger
                tx={tx}
                primaryChannels={primaryChannels}
                selectedChannel={selectedChannel}
                loading={featuresLoading}
                busyKey={featureBusyKey}
                onPick={(name) => setSelectedChannelName(name)}
                onToggle={toggleChannel}
                onOpenOther={() => setOtherModal("channel")}
                onPrev={() => goStep(1)}
                onNext={() => goStep(3)}
                otherSelected={selectedChannel ? !primaryChannelNames.includes(selectedChannel.name) : false}
              />
            ) : null}
            {step === 3 ? (
              <StepTools
                tx={tx}
                tools={tools}
                preset={preset}
                onApplyPreset={applyPreset}
                onFlip={flipTool}
                toolsOnCount={toolsOnCount}
                onPrev={() => goStep(2)}
                onNext={() => goStep(4)}
              />
            ) : null}
            {step === 4 ? (
              <StepDone
                tx={tx}
                settings={settings}
                providerConfigured={providerConfigured}
                selectedProvider={selectedProvider}
                anyChannelEnabled={anyChannelEnabled}
                selectedChannelLabel={channels.find((c) => c.enabled)?.display_name}
                toolsOnCount={toolsOnCount}
                onPrev={() => goStep(3)}
                onOpenSettings={onDone}
                onStartChat={onDone}
              />
            ) : null}
          </main>
        </div>
      </div>

      {otherModal ? (
        <OtherModal
          tx={tx}
          kind={otherModal}
          query={otherQuery}
          onQueryChange={setOtherQuery}
          otherProviders={otherProviders}
          otherChannels={otherChannels}
          onPickProvider={(name) => {
            pickProvider(name);
            setOtherModal(null);
            setOtherQuery("");
          }}
          onPickChannel={(name) => {
            setSelectedChannelName(name);
            setOtherModal(null);
            setOtherQuery("");
          }}
          onClose={() => {
            setOtherModal(null);
            setOtherQuery("");
          }}
        />
      ) : null}
    </div>
  );
}

type Tx = (key: string, fallback: string, values?: Record<string, unknown>) => string;

function StepModel({
  tx,
  primaryProviders,
  selectedProviderName,
  effectiveProvider,
  onPick,
  apiKeyInput,
  setApiKeyInput,
  apiBaseInput,
  setApiBaseInput,
  providerBusy,
  onSave,
  onOpenOther,
  onNext,
  providerConfigured,
  otherSelected,
  showLocalPicker,
  onPickLocalCard,
  onPickLocalBackend,
  codexMode,
  onStartCodex,
  onBackToApiKey,
  availableModels,
  modelsLoading,
  selectedModelId,
  onPickModel,
  error,
}: {
  tx: Tx;
  primaryProviders: SettingsPayload["providers"];
  selectedProviderName: string | null;
  effectiveProvider: SettingsPayload["providers"][number] | null;
  onPick: (name: string) => void;
  apiKeyInput: string;
  setApiKeyInput: (v: string) => void;
  apiBaseInput: string;
  setApiBaseInput: (v: string) => void;
  providerBusy: boolean;
  onSave: () => void;
  onOpenOther: () => void;
  onNext: () => void;
  providerConfigured: boolean;
  otherSelected: boolean;
  showLocalPicker: boolean;
  onPickLocalCard: () => void;
  onPickLocalBackend: (id: string) => void;
  codexMode: boolean;
  onStartCodex: () => void;
  onBackToApiKey: () => void;
  availableModels: { id: string; label?: string }[];
  modelsLoading: boolean;
  selectedModelId: string | null;
  onPickModel: (id: string) => void;
  error: string | null;
}) {
  const p = effectiveProvider;
  const isOpenAiCard = selectedProviderName === "openai" && !showLocalPicker;
  const isLocalCard = showLocalPicker;
  const localBackend = LOCAL_BACKENDS.find((b) => b.id === selectedProviderName);

  return (
    <>
      <span className="ne-lbl ne-eyebrow">{tx("onboarding.step1.eyebrow", "1단계 — 모델")}</span>
      <h1>{tx("onboarding.step1.title", "어떤 모델로 대화할까요?")}</h1>
      <p className="ne-sub">
        {tx(
          "onboarding.step1.sub",
          "에이전트가 생각할 때 쓰는 두뇌입니다. 하나만 연결하면 시작할 수 있고, 나중에 언제든 바꿀 수 있습니다.",
        )}
      </p>
      <div className="ne-sect">
        <div className="ne-grid">
          {primaryProviders.map((provider) => {
            const mark = PROVIDER_MARK[provider.name] ?? { icon: "●", bg: "#F0F0F0", fg: "#666" };
            return (
              <button
                type="button"
                key={provider.name}
                className="ne-opt"
                aria-pressed={!otherSelected && !showLocalPicker && selectedProviderName === provider.name}
                onClick={() => onPick(provider.name)}
              >
                <span className="ne-mark" style={{ background: mark.bg, color: mark.fg }}>{mark.icon}</span>
                <span>
                  <span className="ne-opt-t">{provider.label}</span>
                  <span className="ne-opt-d">
                    {provider.configured ? tx("onboarding.configured", "연결됨") : (provider.api_key_hint ?? tx("onboarding.apiKey", "API 키"))}
                  </span>
                </span>
              </button>
            );
          })}
          <button type="button" className="ne-opt" aria-pressed={showLocalPicker} onClick={onPickLocalCard}>
            <span className="ne-mark" style={{ background: "#EFEAFB", color: "#6D4FC4" }}>▣</span>
            <span>
              <span className="ne-opt-t">{tx("onboarding.step1.local", "로컬 모델")}</span>
              <span className="ne-opt-d">{tx("onboarding.step1.localDesc", "Ollama · LM Studio 등")}</span>
            </span>
          </button>
        </div>
        <button type="button" className="ne-opt ne-other-opt" aria-pressed={otherSelected} onClick={onOpenOther}>
          <span className="ne-mark">⋯</span>
          <span>
            <span className="ne-opt-t">{otherSelected && p ? p.label : tx("onboarding.step1.other", "다른 연결")}</span>
            <span className="ne-opt-d">
              {otherSelected ? tx("onboarding.step1.otherChange", "눌러서 변경") : tx("onboarding.step1.otherHint", "DeepSeek · OpenRouter · GitHub Copilot 등 전체 목록")}
            </span>
          </span>
        </button>
      </div>

      {isOpenAiCard && !codexMode ? (
        <div className="ne-subpanel">
          <div className="ne-field" style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", background: "#fff", border: "1px solid var(--ne-line)", borderRadius: "var(--ne-r-lg)", padding: "13px 14px", marginTop: 0 }}>
            <div style={{ flex: 1, minWidth: 180 }}>
              <span className="ne-item-t">
                {tx("onboarding.step1.codexName", "OpenAI Codex")}{" "}
                <span style={{ fontFamily: "var(--ne-mono)", fontSize: 9.5, letterSpacing: ".05em", color: "var(--ne-muted)", border: "1px solid var(--ne-line-2)", borderRadius: 5, padding: "1.5px 6px", marginLeft: 6 }}>
                  {tx("onboarding.step1.codexTag", "OpenAI OAuth")}
                </span>
              </span>
              <div className="ne-item-d">{tx("onboarding.step1.codexDesc", "ChatGPT 구독 계정으로 로그인합니다. API 키가 필요 없습니다.")}</div>
            </div>
            <button type="button" className="ne-btn ne-oauth-btn" onClick={onStartCodex}>
              <span className="ne-oauth-mark">↗</span> {tx("onboarding.step1.oauthConnect", "OpenAI로 로그인해서 연결")}
            </button>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "16px 0" }}>
            <span style={{ flex: 1, height: 1, background: "var(--ne-line)" }} />
            <span style={{ fontSize: 11.5, color: "var(--ne-muted)", fontFamily: "var(--ne-mono)" }}>{tx("onboarding.step1.or", "또는")}</span>
            <span style={{ flex: 1, height: 1, background: "var(--ne-line)" }} />
          </div>
          <ApiKeyField
            tx={tx}
            label={tx("onboarding.apiKey", "API 키")}
            placeholder={p?.api_key_hint ?? "sk-"}
            value={apiKeyInput}
            onChange={setApiKeyInput}
            onSave={onSave}
            busy={providerBusy}
            connected={providerConfigured}
            connectedLabel={p?.label}
            error={error}
          />
        </div>
      ) : null}

      {isOpenAiCard && codexMode ? (
        <div className="ne-subpanel">
          <div className="ne-field">
            <button type="button" className="ne-btn ne-oauth-btn" onClick={onSave} disabled={providerBusy}>
              <span className="ne-oauth-mark">↗</span>{" "}
              {providerBusy ? tx("onboarding.step1.signingIn", "로그인 창을 여는 중…") : tx("onboarding.step1.oauthConnect", "OpenAI로 로그인해서 연결")}
            </button>
            <p className="ne-hint">{tx("onboarding.step1.oauthHint", "API 키를 붙여넣지 않고, 이미 있는 계정으로 로그인합니다. 브라우저 창이 열리면 로그인을 마쳐주세요.")}</p>
            {error ? <p className="ne-hint" style={{ color: "var(--ne-warn)" }}>{error}</p> : null}
            {providerConfigured ? (
              <div className="ne-strip">
                <span className="ne-tick">✓</span>
                <span className="ne-strip-main">{tx("onboarding.connected", "연결됨")}</span>
                <span className="ne-strip-meta">{p?.oauth_account ?? p?.label}</span>
              </div>
            ) : null}
            <button type="button" className="ne-linkbtn" style={{ paddingLeft: 0, marginTop: 2 }} onClick={onBackToApiKey}>
              {tx("onboarding.step1.backToKey", "API 키로 대신 연결")}
            </button>
          </div>
        </div>
      ) : null}

      {isLocalCard ? (
        <div className="ne-subpanel">
          <ApiKeyField
            tx={tx}
            label={tx("onboarding.step1.serverAddress", "서버 주소")}
            placeholder={localBackend?.base || "http://localhost:11434/v1"}
            value={apiBaseInput}
            onChange={setApiBaseInput}
            onSave={onSave}
            busy={providerBusy}
            connected={providerConfigured}
            connectedLabel={localBackend?.name ?? p?.label}
            hint={tx("onboarding.step1.urlHint", "컴퓨터에서 실행 중인 모델 서버 주소를 넣으세요. 키는 필요 없습니다.")}
            error={error}
          />
          <div className="ne-chips" style={{ marginTop: 14 }}>
            {LOCAL_BACKENDS.map((b) => (
              <button
                type="button"
                key={b.id}
                className="ne-chip"
                aria-pressed={selectedProviderName === b.id}
                onClick={() => onPickLocalBackend(b.id)}
              >
                {b.name}
              </button>
            ))}
          </div>
          {providerConfigured ? (
            <ModelPicker
              tx={tx}
              loading={modelsLoading}
              models={availableModels}
              selectedModelId={selectedModelId}
              onPick={onPickModel}
            />
          ) : null}
        </div>
      ) : null}

      {!isOpenAiCard && !isLocalCard && p ? (
        <div className="ne-subpanel">
          {p.auth_type === "oauth" ? (
            <div className="ne-field">
              <button type="button" className="ne-btn ne-oauth-btn" onClick={onSave} disabled={providerBusy}>
                <span className="ne-oauth-mark">↗</span>{" "}
                {providerBusy ? tx("onboarding.step1.signingIn", "로그인 창을 여는 중…") : tx("onboarding.step1.oauthConnect", "로그인해서 연결")}
              </button>
              <p className="ne-hint">{tx("onboarding.step1.oauthHint", "API 키를 붙여넣지 않고, 이미 있는 계정으로 로그인합니다. 브라우저 창이 열리면 로그인을 마쳐주세요.")}</p>
              {error ? <p className="ne-hint" style={{ color: "var(--ne-warn)" }}>{error}</p> : null}
              {providerConfigured ? (
                <div className="ne-strip">
                  <span className="ne-tick">✓</span>
                  <span className="ne-strip-main">{tx("onboarding.connected", "연결됨")}</span>
                  <span className="ne-strip-meta">{p.oauth_account ?? p.label}</span>
                </div>
              ) : null}
            </div>
          ) : (
            <>
              <ApiKeyField
                tx={tx}
                label={tx("onboarding.apiKey", "API 키")}
                placeholder={p.api_key_hint ?? "sk-"}
                value={apiKeyInput}
                onChange={setApiKeyInput}
                onSave={onSave}
                busy={providerBusy}
                connected={providerConfigured}
                connectedLabel={p.label}
                error={error}
              />
              {providerConfigured ? (
                <ModelPicker
                  tx={tx}
                  loading={modelsLoading}
                  models={availableModels}
                  selectedModelId={selectedModelId}
                  onPick={onPickModel}
                />
              ) : null}
            </>
          )}
        </div>
      ) : null}

      <div className="ne-nav">
        {!providerConfigured ? (
          <span className="ne-nav-hint">
            {p || isLocalCard ? tx("onboarding.step1.hintTest", "연결 확인을 눌러 완료하면 다음으로 넘어갑니다") : tx("onboarding.step1.hintPick", "먼저 모델을 하나 선택하세요")}
          </span>
        ) : null}
        <div className="ne-spacer" />
        <button type="button" className="ne-btn" onClick={onNext} disabled={!providerConfigured}>
          {tx("onboarding.next", "다음")}
        </button>
      </div>
    </>
  );
}

function ApiKeyField({
  tx,
  label,
  placeholder,
  value,
  onChange,
  onSave,
  busy,
  connected,
  connectedLabel,
  hint,
  error,
}: {
  tx: Tx;
  label: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  onSave: () => void;
  busy: boolean;
  connected: boolean;
  connectedLabel?: string;
  hint?: string;
  error?: string | null;
}) {
  return (
    <div className="ne-field" style={{ marginTop: 0 }}>
      <label htmlFor="ne-provider-key">{label}</label>
      <div className="ne-row">
        <input
          id="ne-provider-key"
          type="text"
          placeholder={placeholder}
          value={connected ? "••••••••••••••••••••" : value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onSave();
            }
          }}
        />
        <button type="button" className="ne-btn" onClick={onSave} disabled={busy}>
          {tx("onboarding.step1.testConnect", "연결 확인")}
        </button>
      </div>
      <p className="ne-hint">{hint ?? tx("onboarding.step1.keyHint", "키는 이 컴퓨터의 config.json에만 저장되고 외부로 보내지 않습니다.")}</p>
      {error ? <p className="ne-hint" style={{ color: "var(--ne-warn)" }}>{error}</p> : null}
      {busy ? (
        <div className="ne-testing">
          <span className="ne-spin" /> {tx("onboarding.step1.testing", "연결을 확인하는 중…")}
        </div>
      ) : connected ? (
        <div className="ne-strip">
          <span className="ne-tick">✓</span>
          <span className="ne-strip-main">{tx("onboarding.connected", "연결됨")}</span>
          <span className="ne-strip-meta">{connectedLabel}</span>
        </div>
      ) : null}
    </div>
  );
}

function ModelPicker({
  tx,
  loading,
  models,
  selectedModelId,
  onPick,
}: {
  tx: Tx;
  loading: boolean;
  models: { id: string; label?: string }[];
  selectedModelId: string | null;
  onPick: (id: string) => void;
}) {
  if (loading) {
    return <p className="ne-hint" style={{ marginTop: 12 }}>{tx("onboarding.step1.loadingModels", "사용 가능한 모델을 확인하는 중…")}</p>;
  }
  if (!models.length) return null;
  return (
    <div className="ne-field">
      <label htmlFor="ne-model-picker">{tx("onboarding.step1.pickModel", "모델 선택")}</label>
      <select
        id="ne-model-picker"
        value={selectedModelId ?? ""}
        onChange={(e) => onPick(e.target.value)}
        style={{
          width: "100%",
          padding: "9px 12px",
          border: "1px solid var(--ne-line-2)",
          borderRadius: "var(--ne-r)",
          font: "inherit",
          fontFamily: "var(--ne-mono)",
          fontSize: 12.5,
          background: "#fff",
          color: "var(--ne-ink)",
        }}
      >
        {models.map((m) => (
          <option key={m.id} value={m.id}>{m.label ?? m.id}</option>
        ))}
      </select>
      <p className="ne-hint">{tx("onboarding.step1.pickModelHint", "이 서버에서 실제로 쓸 수 있는 모델 목록입니다.")}</p>
    </div>
  );
}

function StepMessenger({
  tx,
  primaryChannels,
  selectedChannel,
  loading,
  busyKey,
  onPick,
  onToggle,
  onOpenOther,
  onPrev,
  onNext,
  otherSelected,
}: {
  tx: Tx;
  primaryChannels: NanobotFeatureInfo[];
  selectedChannel: NanobotFeatureInfo | null;
  loading: boolean;
  busyKey: string | null;
  onPick: (name: string) => void;
  onToggle: (feature: NanobotFeatureInfo) => void;
  onOpenOther: () => void;
  onPrev: () => void;
  onNext: () => void;
  otherSelected: boolean;
}) {
  const c = selectedChannel;
  const skippedOrNone = !c;
  return (
    <>
      <span className="ne-lbl ne-eyebrow">{tx("onboarding.step2.eyebrow", "2단계 — 메신저")}</span>
      <h1>{tx("onboarding.step2.title", "휴대폰에서도 쓰시겠어요?")}</h1>
      <p className="ne-sub">
        {tx(
          "onboarding.step2.sub",
          "메신저를 연결하면 평소 쓰는 앱에서 에이전트를 부를 수 있습니다. 웹 화면만 쓸 거라면 건너뛰어도 됩니다.",
        )}
      </p>
      <div className="ne-sect">
        {loading ? (
          <p className="ne-hint">{tx("common.loading", "Loading...")}</p>
        ) : (
          <div className="ne-grid">
            {primaryChannels.map((feature) => (
              <button
                type="button"
                key={feature.name}
                className="ne-opt"
                aria-pressed={!otherSelected && c?.name === feature.name}
                onClick={() => onPick(feature.name)}
              >
                <span className="ne-mark">{feature.enabled ? "✓" : "○"}</span>
                <span>
                  <span className="ne-opt-t">{feature.display_name}</span>
                  <span className="ne-opt-d">
                    {feature.enabled ? tx("onboarding.connected", "연결됨") : tx("onboarding.step2.notYet", "아직 연결 안 됨")}
                  </span>
                </span>
              </button>
            ))}
          </div>
        )}
        <button type="button" className="ne-opt ne-other-opt" aria-pressed={otherSelected} onClick={onOpenOther}>
          <span className="ne-mark">⋯</span>
          <span>
            <span className="ne-opt-t">{otherSelected && c ? c.display_name : tx("onboarding.step2.other", "다른 메신저")}</span>
            <span className="ne-opt-d">
              {otherSelected ? tx("onboarding.step1.otherChange", "눌러서 변경") : tx("onboarding.step2.otherHint", "Discord · WeChat · 이메일 등 전체 목록")}
            </span>
          </span>
        </button>
      </div>
      {c ? (
        <div className="ne-subpanel">
          <div className="ne-field">
            <p className="ne-hint">
              {tx(
                "onboarding.step2.tokenNote",
                "토큰이 필요한 메신저는 아직 이 화면에서 직접 입력할 수 없어요 — 연결 버튼을 누르면 채팅에서 마저 도와드립니다.",
              )}
            </p>
            <button
              type="button"
              className="ne-btn"
              onClick={() => onToggle(c)}
              disabled={busyKey === `enable:${c.name}` || busyKey === `disable:${c.name}`}
            >
              {c.enabled ? tx("onboarding.step2.disconnect", "연결 해제") : tx("onboarding.step2.connect", "연결")}
            </button>
          </div>
        </div>
      ) : null}
      {skippedOrNone ? (
        <p className="ne-hint" style={{ marginTop: 16 }}>
          {tx("onboarding.step2.webOnly", "웹 화면에서 바로 대화할 수 있습니다. 메신저는 나중에 설정에서 추가하세요.")}
        </p>
      ) : null}
      <div className="ne-nav">
        <button type="button" className="ne-btn ne-ghost" onClick={onPrev}>{tx("onboarding.back", "이전")}</button>
        <div className="ne-spacer" />
        <button type="button" className="ne-linkbtn" onClick={onNext}>{tx("onboarding.skip", "건너뛰기")}</button>
        <button type="button" className="ne-btn" onClick={onNext}>{tx("onboarding.next", "다음")}</button>
      </div>
    </>
  );
}

function StepTools({
  tx,
  tools,
  preset,
  onApplyPreset,
  onFlip,
  toolsOnCount,
  onPrev,
  onNext,
}: {
  tx: Tx;
  tools: Record<string, boolean>;
  preset: string | null;
  onApplyPreset: (name: string | null) => void;
  onFlip: (key: "web_enabled" | "file_enabled" | "exec_enabled" | "cli_apps_enabled" | "image_generation_enabled") => void;
  toolsOnCount: number;
  onPrev: () => void;
  onNext: () => void;
}) {
  const rows: Array<[
    "web_enabled" | "file_enabled" | "exec_enabled" | "cli_apps_enabled" | "image_generation_enabled",
    string,
    string,
    boolean,
  ]> = [
    ["web_enabled", tx("onboarding.tools.web", "웹 검색"), tx("onboarding.tools.webDesc", "최신 정보를 찾아 출처와 함께 답합니다"), false],
    ["file_enabled", tx("onboarding.tools.file", "파일 다루기"), tx("onboarding.tools.fileDesc", "작업 폴더의 문서를 읽고 씁니다"), false],
    ["image_generation_enabled", tx("onboarding.tools.image", "이미지 생성"), tx("onboarding.tools.imageDesc", "설명을 그림으로 만듭니다"), false],
    ["cli_apps_enabled", tx("onboarding.tools.cliApps", "외부 프로그램 실행"), tx("onboarding.tools.cliAppsDesc", "연결한 프로그램을 실제로 실행합니다"), false],
    ["exec_enabled", tx("onboarding.tools.exec", "명령 실행"), tx("onboarding.tools.execDesc", "컴퓨터에서 명령을 직접 실행합니다"), true],
  ];
  return (
    <>
      <span className="ne-lbl ne-eyebrow">{tx("onboarding.step3.eyebrow", "3단계 — 도구와 스킬")}</span>
      <h1>{tx("onboarding.step3.title", "무엇을 할 수 있게 할까요?")}</h1>
      <p className="ne-sub">{tx("onboarding.step3.sub", "도구는 에이전트의 손입니다. 하는 일에 맞는 묶음을 고르면 알아서 켜집니다.")}</p>
      <div className="ne-sect">
        <div className="ne-chips">
          <button type="button" className="ne-chip" aria-pressed={preset === "study"} onClick={() => onApplyPreset("study")}>{tx("onboarding.preset.study", "공부용")}</button>
          <button type="button" className="ne-chip" aria-pressed={preset === "work"} onClick={() => onApplyPreset("work")}>{tx("onboarding.preset.work", "업무용")}</button>
          <button type="button" className="ne-chip" aria-pressed={preset === "dev"} onClick={() => onApplyPreset("dev")}>{tx("onboarding.preset.dev", "개발용")}</button>
          <button type="button" className="ne-chip" aria-pressed={preset === null} onClick={() => onApplyPreset(null)}>{tx("onboarding.preset.custom", "직접 고르기")}</button>
        </div>
      </div>
      <div className="ne-grouphead">
        <span className="ne-lbl">{tx("onboarding.step3.toolsLabel", "도구")}</span>
        <span className="ne-hint">{tx("onboarding.step3.toolsHint", "되돌릴 수 없는 작업은 승인을 거칩니다")}</span>
      </div>
      <div className="ne-list">
        {rows.map(([key, title, desc, locked]) => (
          <div className="ne-item" key={key}>
            <span className="ne-item-body">
              <span className="ne-item-t">{title}</span>
              <span className="ne-item-d">{desc}</span>
            </span>
            {locked ? <span className="ne-badge">{tx("onboarding.needsApproval", "승인 필요")}</span> : null}
            <button
              type="button"
              className="ne-sw"
              role="switch"
              aria-checked={tools[key]}
              aria-label={title}
              disabled={locked}
              onClick={() => onFlip(key)}
            />
          </div>
        ))}
      </div>
      <div className="ne-nav">
        <button type="button" className="ne-btn ne-ghost" onClick={onPrev}>{tx("onboarding.back", "이전")}</button>
        <div className="ne-spacer" />
        <button type="button" className="ne-btn" onClick={onNext} disabled={toolsOnCount === 0}>
          {tx("onboarding.step3.finish", "설정 마치기")}
        </button>
      </div>
    </>
  );
}

function StepDone({
  tx,
  settings,
  providerConfigured,
  selectedProvider,
  anyChannelEnabled,
  selectedChannelLabel,
  toolsOnCount,
  onPrev,
  onOpenSettings,
  onStartChat,
}: {
  tx: Tx;
  settings: SettingsPayload | null;
  providerConfigured: boolean;
  selectedProvider: SettingsPayload["providers"][number] | null;
  anyChannelEnabled: boolean;
  selectedChannelLabel?: string;
  toolsOnCount: number;
  onPrev: () => void;
  onOpenSettings: () => void;
  onStartChat: () => void;
}) {
  return (
    <>
      <div className="ne-done-head">
        <div className="ne-done-ring">✓</div>
        <h1 style={{ textAlign: "center" }}>{tx("onboarding.step4.title", "준비됐습니다")}</h1>
        <p className="ne-sub" style={{ margin: "7px auto 0", textAlign: "center" }}>
          {tx("onboarding.step4.sub", "지금부터 대화할 수 있습니다. 아래 내용은 설정에서 언제든 바꿀 수 있습니다.")}
        </p>
      </div>
      <div className="ne-summary">
        <div className="ne-srow">
          <span className="ne-k">{tx("onboarding.wire.model", "모델")}</span>
          <span className="ne-v">{providerConfigured ? (settings?.agent.model ?? selectedProvider?.label) : tx("onboarding.wire.notConnected", "연결 안 됨")}</span>
        </div>
        <div className="ne-srow">
          <span className="ne-k">{tx("onboarding.wire.messenger", "메신저")}</span>
          <span className={`ne-v${anyChannelEnabled ? "" : " off"}`}>{anyChannelEnabled ? selectedChannelLabel : tx("onboarding.step4.notUsed", "사용 안 함")}</span>
        </div>
        <div className="ne-srow">
          <span className="ne-k">{tx("onboarding.step4.toolsRow", "도구")}</span>
          <span className="ne-v">{tx("onboarding.step4.count", "{{count}}개 사용", { count: toolsOnCount })}</span>
        </div>
        <div className="ne-srow">
          <span className="ne-k">{tx("onboarding.step4.savedAt", "저장 위치")}</span>
          <span className="ne-v" style={{ fontFamily: "var(--ne-mono)", fontSize: 12, fontWeight: 400 }}>.local/config.json</span>
        </div>
      </div>
      <div className="ne-nav">
        <button type="button" className="ne-btn ne-ghost" onClick={onPrev}>{tx("onboarding.back", "이전")}</button>
        <div className="ne-spacer" />
        <button type="button" className="ne-btn ne-ghost" onClick={onOpenSettings}>{tx("onboarding.step4.moreSettings", "설정 더 보기")}</button>
        <button type="button" className="ne-btn" onClick={onStartChat}>{tx("onboarding.step4.startChat", "대화 시작하기")}</button>
      </div>
    </>
  );
}

function OtherModal({
  tx,
  kind,
  query,
  onQueryChange,
  otherProviders,
  otherChannels,
  onPickProvider,
  onPickChannel,
  onClose,
}: {
  tx: Tx;
  kind: "provider" | "channel";
  query: string;
  onQueryChange: (v: string) => void;
  otherProviders: SettingsPayload["providers"];
  otherChannels: NanobotFeatureInfo[];
  onPickProvider: (name: string) => void;
  onPickChannel: (name: string) => void;
  onClose: () => void;
}) {
  const q = query.trim().toLowerCase();
  const title = kind === "provider" ? tx("onboarding.modal.providerTitle", "연결할 모델 고르기") : tx("onboarding.modal.channelTitle", "연결할 메신저 고르기");
  const placeholder = kind === "provider" ? tx("onboarding.modal.providerPh", "모델·제공자 이름으로 찾기") : tx("onboarding.modal.channelPh", "메신저 이름으로 찾기");
  const items =
    kind === "provider"
      ? otherProviders.filter((p) => !q || p.label.toLowerCase().includes(q))
      : otherChannels.filter((c) => !q || c.display_name.toLowerCase().includes(q));

  return (
    <div className="ne-wizard">
      <div className="ne-modal-veil open" onClick={(e) => e.target === e.currentTarget && onClose()}>
        <div className="ne-modal" role="dialog" aria-modal="true">
          <div className="ne-modal-head">
            <div>
              <span className="ne-lbl">{tx("onboarding.modal.fullList", "전체 목록")}</span>
              <h2>{title}</h2>
            </div>
            <button type="button" className="ne-modal-x" onClick={onClose} aria-label={tx("common.close", "닫기")}>✕</button>
          </div>
          <input
            className="ne-modal-search"
            type="text"
            placeholder={placeholder}
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            autoFocus
          />
          <div className="ne-modal-list">
            {items.length ? (
              kind === "provider"
                ? (items as SettingsPayload["providers"]).map((p) => (
                    <button key={p.name} type="button" className="ne-og-row" onClick={() => onPickProvider(p.name)}>
                      {p.label}
                      <span className="ne-go">{p.configured ? tx("onboarding.connected", "연결됨") : "→"}</span>
                    </button>
                  ))
                : (items as NanobotFeatureInfo[]).map((c) => (
                    <button key={c.name} type="button" className="ne-og-row" onClick={() => onPickChannel(c.name)}>
                      {c.display_name}
                      <span className="ne-go">{c.enabled ? tx("onboarding.connected", "연결됨") : "→"}</span>
                    </button>
                  ))
            ) : (
              <div className="ne-modal-empty">{tx("onboarding.modal.empty", "일치하는 항목이 없습니다.")}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
