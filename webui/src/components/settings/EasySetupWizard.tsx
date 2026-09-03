import { useState, type Dispatch, type SetStateAction } from "react";
import { Check, ExternalLink, Eye, EyeOff, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type {
  NanobotFeatureInfo,
  NanobotFeaturesPayload,
  SettingsPayload,
  SkillSummary,
} from "@/lib/types";

import { AgentToolsSettings } from "@/components/settings/AgentToolsSettings";
import { agentProviderIsConfigured } from "@/components/settings/settings-helpers";
import { SettingsGroup, SettingsRow } from "@/components/settings/settings-primitives";
import {
  ModelIdPicker,
  ProviderPicker,
  ProviderPickerIcon,
} from "@/components/settings/settings-provider-picker";
import type {
  AgentSettingsDraft,
  ProviderForm,
  SettingsSectionKey,
} from "@/components/settings/SettingsView";

export function EasySetupWizard({
  token,
  settings,
  form,
  setForm,
  modelDirty,
  savingModel,
  showBrandLogos,
  providerSaving,
  providerForms,
  visibleProviderKeys,
  editingProviderKeys,
  onChangeProviderForm,
  onToggleProviderKey,
  onToggleProviderKeyEditing,
  onSaveProvider,
  onProviderOAuthLogin,
  onSaveModel,
  nanobotFeatures,
  nanobotFeaturesLoading,
  nanobotFeatureAction,
  onNanobotAction,
  skills,
  onSettingsChange,
  onSelectSection,
  onBackToChat,
}: {
  token: string;
  settings: SettingsPayload;
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  modelDirty: boolean;
  savingModel: boolean;
  showBrandLogos: boolean;
  providerSaving: string | null;
  providerForms: Record<string, ProviderForm>;
  visibleProviderKeys: Record<string, boolean>;
  editingProviderKeys: Record<string, boolean>;
  onChangeProviderForm: (provider: string, value: Partial<ProviderForm>) => void;
  onToggleProviderKey: (provider: string) => void;
  onToggleProviderKeyEditing: (provider: string) => void;
  onSaveProvider: (provider: string) => void;
  onProviderOAuthLogin: (provider: string) => void;
  onSaveModel: () => void;
  nanobotFeatures: NanobotFeaturesPayload | null;
  nanobotFeaturesLoading: boolean;
  nanobotFeatureAction: string | null;
  onNanobotAction: (action: "enable" | "disable", name: string) => void;
  skills?: SkillSummary[];
  onSettingsChange?: (payload: SettingsPayload) => void;
  onSelectSection: (section: SettingsSectionKey) => void;
  onBackToChat: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const [step, setStep] = useState(1);

  const primaryProviders = ["openai", "anthropic", "google", "ollama"];
  const providerOptions = settings.providers.filter((provider) => primaryProviders.includes(provider.name));
  const currentProvider = settings.providers.find((provider) => provider.name === form.provider) ?? null;
  const providerConfigured = agentProviderIsConfigured(settings);
  const channelFeatures = (nanobotFeatures?.features ?? []).filter((f) => f.type === "channel");
  const anyChannelEnabled = channelFeatures.some((f) => f.enabled);
  const availableSkillsCount = (skills ?? []).filter((s) => s.available !== false).length;

  const steps = [
    { n: 1, label: tx("settings.easySetup.wizard.stepModel", "Model"), done: providerConfigured },
    { n: 2, label: tx("settings.easySetup.wizard.stepMessenger", "Messenger"), done: anyChannelEnabled },
    { n: 3, label: tx("settings.easySetup.wizard.stepTools", "Tools & skills"), done: step > 3 },
    { n: 4, label: tx("settings.easySetup.wizard.stepDone", "Done"), done: false },
  ];

  const canAdvance = step !== 1 || providerConfigured;

  return (
    <div className="nanobot-easy-wizard mx-auto max-w-[680px] pb-4">
      <div className="mb-7 text-center">
        <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-primary">nanobot-easy</p>
        <h2 className="mt-2 text-[22px] font-medium leading-tight text-foreground">
          {tx("settings.easySetup.wizard.title", "A few things before you start")}
        </h2>
        <p className="mt-2 text-[13px] leading-6 text-muted-foreground">
          {tx("settings.easySetup.wizard.subtitle", "You can change any of this later in Settings.")}
        </p>
      </div>

      <WizardStepper steps={steps} current={step} onJump={setStep} />

      <div className="mt-6 rounded-[20px] border border-border/50 bg-card p-6 shadow-[0_10px_40px_rgba(15,23,42,0.06)]">
        {step === 1 ? (
          <ModelStep
            token={token}
            settings={settings}
            form={form}
            setForm={setForm}
            modelDirty={modelDirty}
            savingModel={savingModel}
            showBrandLogos={showBrandLogos}
            providerOptions={providerOptions}
            currentProvider={currentProvider}
            providerSaving={providerSaving}
            providerForms={providerForms}
            visibleProviderKeys={visibleProviderKeys}
            editingProviderKeys={editingProviderKeys}
            onChangeProviderForm={onChangeProviderForm}
            onToggleProviderKey={onToggleProviderKey}
            onToggleProviderKeyEditing={onToggleProviderKeyEditing}
            onSaveProvider={onSaveProvider}
            onProviderOAuthLogin={onProviderOAuthLogin}
            onSaveModel={onSaveModel}
            tx={tx}
          />
        ) : null}
        {step === 2 ? (
          <MessengerStep
            channelFeatures={channelFeatures}
            loading={nanobotFeaturesLoading}
            actionKey={nanobotFeatureAction}
            onAction={onNanobotAction}
            tx={tx}
          />
        ) : null}
        {step === 3 ? (
          <ToolsStep settings={settings} onSettingsChange={onSettingsChange} availableSkillsCount={availableSkillsCount} onSelectSection={onSelectSection} tx={tx} />
        ) : null}
        {step === 4 ? (
          <DoneStep
            providerConfigured={providerConfigured}
            currentProvider={currentProvider}
            anyChannelEnabled={anyChannelEnabled}
            tx={tx}
          />
        ) : null}
      </div>

      <div className="mt-4 flex items-center justify-between">
        <Button
          variant="ghost"
          className="rounded-full"
          onClick={() => setStep((s) => Math.max(1, s - 1))}
          disabled={step === 1}
        >
          {tx("settings.easySetup.wizard.back", "Back")}
        </Button>
        {step < 4 ? (
          <div className="flex items-center gap-2">
            {step !== 1 ? (
              <Button
                variant="outline"
                className="rounded-full"
                onClick={() => setStep((s) => Math.min(4, s + 1))}
              >
                {tx("settings.easySetup.wizard.skip", "Skip")}
              </Button>
            ) : null}
            <Button
              className="rounded-full"
              onClick={() => setStep((s) => Math.min(4, s + 1))}
              disabled={!canAdvance}
            >
              {tx("settings.easySetup.wizard.next", "Next")}
            </Button>
          </div>
        ) : (
          <Button className="rounded-full" onClick={onBackToChat} disabled={!providerConfigured}>
            {tx("settings.easySetup.startChat", "시작하기")}
          </Button>
        )}
      </div>
      {step === 1 && !providerConfigured ? (
        <p className="mt-2 text-center text-[12px] font-medium text-[hsl(var(--ne-warn))]">
          {tx("settings.easySetup.wizard.needModel", "Connect a model first to continue")}
        </p>
      ) : null}
    </div>
  );
}

export function WizardStepper({
  steps,
  current,
  onJump,
}: {
  steps: Array<{ n: number; label: string; done: boolean }>;
  current: number;
  onJump: (n: number) => void;
}) {
  return (
    <div className="flex items-start">
      {steps.map((s, i) => (
        <div key={s.n} className={cn("flex items-center", i < steps.length - 1 ? "flex-1" : "")}>
          <button
            type="button"
            onClick={() => onJump(s.n)}
            className="flex flex-col items-center gap-1.5"
          >
            <span
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-full border-2 text-[12px] font-semibold transition-colors",
                s.n === current
                  ? "border-primary bg-primary text-primary-foreground"
                  : s.done
                    ? "border-primary/55 bg-primary/10 text-primary"
                    : "border-border bg-background text-muted-foreground",
              )}
            >
              {s.done && s.n !== current ? <Check className="h-4 w-4" aria-hidden /> : s.n}
            </span>
            <span
              className={cn(
                "whitespace-nowrap text-[11px] font-medium",
                s.n === current ? "text-foreground" : "text-muted-foreground",
              )}
            >
              {s.label}
            </span>
          </button>
          {i < steps.length - 1 ? (
            <span
              className={cn(
                "mx-2 mt-4 h-[2px] flex-1 rounded-full transition-colors",
                s.done ? "bg-primary/50" : "bg-border",
              )}
            />
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function ModelStep({
  token,
  settings,
  form,
  setForm,
  modelDirty,
  savingModel,
  showBrandLogos,
  providerOptions,
  currentProvider,
  providerSaving,
  providerForms,
  visibleProviderKeys,
  editingProviderKeys,
  onChangeProviderForm,
  onToggleProviderKey,
  onToggleProviderKeyEditing,
  onSaveProvider,
  onProviderOAuthLogin,
  onSaveModel,
  tx,
}: {
  token: string;
  settings: SettingsPayload;
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  modelDirty: boolean;
  savingModel: boolean;
  showBrandLogos: boolean;
  providerOptions: SettingsPayload["providers"];
  currentProvider: SettingsPayload["providers"][number] | null;
  providerSaving: string | null;
  providerForms: Record<string, ProviderForm>;
  visibleProviderKeys: Record<string, boolean>;
  editingProviderKeys: Record<string, boolean>;
  onChangeProviderForm: (provider: string, value: Partial<ProviderForm>) => void;
  onToggleProviderKey: (provider: string) => void;
  onToggleProviderKeyEditing: (provider: string) => void;
  onSaveProvider: (provider: string) => void;
  onProviderOAuthLogin: (provider: string) => void;
  onSaveModel: () => void;
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string;
}) {
  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-[15px] font-medium text-foreground">
          {tx("settings.easySetup.wizard.modelHeading", "Which model do you want to use?")}
        </h3>
        <p className="mt-1 text-[13px] text-muted-foreground">
          {tx("settings.easySetup.wizard.modelSub", "Pick one of the common options, then add an API key or sign in.")}
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {providerOptions.map((provider) => (
          <button
            type="button"
            key={provider.name}
            onClick={() => setForm((prev) => ({ ...prev, provider: provider.name }))}
            className={cn(
              "flex min-h-[84px] items-start gap-3 rounded-[18px] border p-4 text-left transition-colors",
              form.provider === provider.name
                ? "border-primary/55 bg-primary/5"
                : "border-border/55 bg-background hover:bg-muted/45",
            )}
          >
            <ProviderPickerIcon provider={provider.name} showBrandLogos={showBrandLogos} />
            <span className="min-w-0">
              <span className="block text-[14px] font-medium text-foreground">{provider.label}</span>
              <span className="mt-1 block text-[12px] leading-5 text-muted-foreground">
                {provider.auth_type === "oauth"
                  ? tx("settings.easySetup.oauthProvider", "OAuth login")
                  : provider.default_api_base
                    ? provider.default_api_base
                    : provider.configured
                      ? tx("settings.easySetup.configured", "Configured")
                      : (provider.api_key_hint ?? tx("settings.easySetup.apiKey", "API key"))}
              </span>
            </span>
          </button>
        ))}
      </div>
      <SettingsGroup>
        <SettingsRow
          title={tx("settings.easySetup.activeModel", "Default model")}
          description={tx("settings.easySetup.activeModelHelp", "Choose the model ID used by the default preset.")}
        >
          <div className="flex flex-wrap justify-end gap-2">
            <ProviderPicker
              providers={settings.providers.map((provider) => ({ name: provider.name, label: provider.label }))}
              value={form.provider}
              emptyLabel={tx("settings.models.provider", "Provider")}
              showProviderLogos={showBrandLogos}
              onChange={(provider) => setForm((prev) => ({ ...prev, provider }))}
            />
            <ModelIdPicker
              token={token}
              settings={settings}
              provider={form.provider}
              value={form.model}
              showProviderLogos={showBrandLogos}
              onChange={(model) => setForm((prev) => ({ ...prev, model }))}
            />
            <Button size="sm" className="rounded-full" onClick={onSaveModel} disabled={!modelDirty || savingModel}>
              {savingModel ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
              {tx("settings.actions.save", "Save")}
            </Button>
          </div>
        </SettingsRow>
      </SettingsGroup>
      {currentProvider ? (
        <ProviderQuickConnectRow
          provider={currentProvider}
          form={
            providerForms[currentProvider.name] ?? {
              apiKey: "",
              apiBase: currentProvider.api_base ?? currentProvider.default_api_base ?? "",
              apiType: currentProvider.api_type ?? "auto",
            }
          }
          saving={providerSaving === currentProvider.name}
          keyVisible={!!visibleProviderKeys[currentProvider.name]}
          keyEditing={!!editingProviderKeys[currentProvider.name]}
          showBrandLogos={showBrandLogos}
          onChange={(value) => onChangeProviderForm(currentProvider.name, value)}
          onToggleKey={() => onToggleProviderKey(currentProvider.name)}
          onToggleKeyEditing={() => onToggleProviderKeyEditing(currentProvider.name)}
          onSave={() => onSaveProvider(currentProvider.name)}
          onOAuthLogin={() => onProviderOAuthLogin(currentProvider.name)}
        />
      ) : null}
    </div>
  );
}

export function MessengerStep({
  channelFeatures,
  loading,
  actionKey,
  onAction,
  tx,
}: {
  channelFeatures: NanobotFeatureInfo[];
  loading: boolean;
  actionKey: string | null;
  onAction: (action: "enable" | "disable", name: string) => void;
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string;
}) {
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-[15px] font-medium text-foreground">
          {tx("settings.easySetup.wizard.messengerHeading", "Connect a messenger")}
        </h3>
        <p className="mt-1 text-[13px] text-muted-foreground">
          {tx("settings.easySetup.wizard.messengerSub", "This is optional — you can connect one later.")}
        </p>
      </div>
      {loading ? (
        <div className="flex h-24 items-center justify-center text-[13px] text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
          {tx("common.loading", "Loading...")}
        </div>
      ) : channelFeatures.length ? (
        <div className="space-y-2">
          {channelFeatures.map((feature) => {
            const busy = actionKey === `enable:${feature.name}` || actionKey === `disable:${feature.name}`;
            return (
              <div
                key={feature.name}
                className="flex items-center justify-between gap-3 rounded-[14px] border border-border/60 bg-background px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="text-[13.5px] font-medium text-foreground">{feature.display_name}</p>
                  <p className="text-[12px] text-muted-foreground">
                    {feature.enabled
                      ? tx("settings.easySetup.wizard.channelEnabled", "Listening for messages")
                      : tx("settings.easySetup.wizard.channelDisabled", "Not connected yet")}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant={feature.enabled ? "outline" : "default"}
                  className="rounded-full shrink-0"
                  disabled={busy}
                  onClick={() => onAction(feature.enabled ? "disable" : "enable", feature.name)}
                >
                  {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                  {feature.enabled
                    ? tx("settings.easySetup.wizard.disconnect", "Disconnect")
                    : tx("settings.easySetup.wizard.connect", "Connect")}
                </Button>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="text-[13px] text-muted-foreground">
          {tx("settings.easySetup.wizard.noChannels", "No messengers are available.")}
        </p>
      )}
    </div>
  );
}

export function ToolsStep({
  settings,
  onSettingsChange,
  availableSkillsCount,
  onSelectSection,
  tx,
}: {
  settings: SettingsPayload;
  onSettingsChange?: (payload: SettingsPayload) => void;
  availableSkillsCount: number;
  onSelectSection: (section: SettingsSectionKey) => void;
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string;
}) {
  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-[15px] font-medium text-foreground">
          {tx("settings.easySetup.wizard.toolsHeading", "Turn on tools")}
        </h3>
        <p className="mt-1 text-[13px] text-muted-foreground">
          {tx("settings.easySetup.wizard.toolsSub", "The defaults are usually enough. You can change these anytime.")}
        </p>
      </div>
      <AgentToolsSettings settings={settings} onSettingsChange={onSettingsChange} />
      <button
        type="button"
        onClick={() => onSelectSection("skills")}
        className="flex w-full items-center justify-between rounded-[14px] border border-border/60 bg-background px-4 py-3 text-left transition-colors hover:bg-muted/40"
      >
        <span className="text-[12.5px] text-muted-foreground">
          {tx("settings.easySetup.wizard.skillsCount", "{{count}} skills available", {
            count: availableSkillsCount,
          })}
        </span>
        <span className="text-[12px] font-medium text-primary">
          {tx("settings.easySetup.wizard.skillsOpen", "Open Skills →")}
        </span>
      </button>
    </div>
  );
}

export function DoneStep({
  providerConfigured,
  currentProvider,
  anyChannelEnabled,
  tx,
}: {
  providerConfigured: boolean;
  currentProvider: SettingsPayload["providers"][number] | null;
  anyChannelEnabled: boolean;
  tx: (key: string, fallback: string, values?: Record<string, unknown>) => string;
}) {
  return (
    <div className="space-y-5 text-center">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-[22px] text-primary">
        <Check className="h-6 w-6" aria-hidden />
      </div>
      <h3 className="text-[16px] font-medium text-foreground">
        {tx("settings.easySetup.wizard.doneHeading", "You're all set!")}
      </h3>
      <div className="mx-auto max-w-[380px] space-y-3 rounded-[14px] border border-border/60 bg-background p-4 text-left text-[12.5px]">
        <SetupWire
          label={tx("settings.easySetup.wizard.stepModel", "Model")}
          live={providerConfigured}
          value={currentProvider?.label ?? tx("settings.easySetup.wizard.notConnected", "Not connected")}
        />
        <SetupWire
          label={tx("settings.easySetup.wizard.stepMessenger", "Messenger")}
          live={anyChannelEnabled}
          value={
            anyChannelEnabled
              ? tx("settings.easySetup.wizard.channelEnabled", "Listening for messages")
              : tx("settings.easySetup.wizard.laterOk", "You can set this up later")
          }
        />
      </div>
    </div>
  );
}


export function SetupWire({ label, live, value }: { label: string; live: boolean; value: string }) {
  return (
    <div className="flex items-start gap-2">
      <span className={cn("mt-1 h-2.5 w-2.5 rounded-full border", live ? "border-emerald-600 bg-emerald-600" : "border-border bg-background")} />
      <span className="min-w-0">
        <span className="block font-medium text-foreground">{label}</span>
        <span className={cn("block truncate font-mono text-[11px]", live ? "text-emerald-700 dark:text-emerald-400" : "text-muted-foreground")}>{value}</span>
      </span>
    </div>
  );
}

export function ProviderQuickConnectRow({
  provider,
  form,
  saving,
  keyVisible,
  keyEditing,
  showBrandLogos,
  onChange,
  onToggleKey,
  onToggleKeyEditing,
  onSave,
  onOAuthLogin,
}: {
  provider: SettingsPayload["providers"][number];
  form: ProviderForm;
  saving: boolean;
  keyVisible: boolean;
  keyEditing: boolean;
  showBrandLogos: boolean;
  onChange: (value: Partial<ProviderForm>) => void;
  onToggleKey: () => void;
  onToggleKeyEditing: () => void;
  onSave: () => void;
  onOAuthLogin: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  if (provider.auth_type === "oauth") {
    return (
      <SettingsGroup>
        <SettingsRow
          title={provider.label}
          description={provider.oauth_account ? provider.oauth_account : tx("settings.easySetup.oauthHelp", "Connect this provider with the browser OAuth flow.")}
        >
          <Button size="sm" className="rounded-full" onClick={onOAuthLogin} disabled={saving}>
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ExternalLink className="mr-1.5 h-3.5 w-3.5" />}
            {provider.configured ? tx("settings.actions.reconnect", "Reconnect") : tx("settings.actions.connect", "Connect")}
          </Button>
        </SettingsRow>
      </SettingsGroup>
    );
  }
  const needsKey = provider.api_key_required ?? true;
  const canSave = provider.configured || !needsKey || form.apiKey.trim().length > 0 || form.apiBase.trim().length > 0;
  return (
    <SettingsGroup>
      <SettingsRow
        title={provider.label}
        description={provider.configured ? tx("settings.easySetup.providerConfigured", "Credentials are saved locally in config.json.") : tx("settings.easySetup.providerHelp", "Paste an API key or local compatible endpoint.")}
      >
        <div className="grid w-full max-w-[520px] gap-2 sm:grid-cols-[1fr_auto]">
          <div className="flex items-center gap-2 rounded-full border border-input bg-background px-3">
            <ProviderPickerIcon provider={provider.name} showBrandLogos={showBrandLogos} />
            <Input
              type={keyVisible ? "text" : "password"}
              value={form.apiKey}
              onChange={(event) => onChange({ apiKey: event.target.value })}
              placeholder={provider.configured && !keyEditing ? "••••••••••••" : provider.api_key_hint ?? "API key"}
              className="h-8 border-0 bg-transparent px-0 text-[13px] shadow-none focus-visible:ring-0"
              disabled={provider.configured && !keyEditing}
            />
            <button type="button" className="text-muted-foreground" onClick={onToggleKey} aria-label="Toggle API key visibility">
              {keyVisible ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            </button>
          </div>
          <Button size="sm" className="rounded-full" onClick={onSave} disabled={!canSave || saving}>
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
            {provider.configured && !keyEditing ? tx("settings.actions.saved", "Saved") : tx("settings.actions.save", "Save")}
          </Button>
          {provider.default_api_base || provider.api_base ? (
            <Input
              value={form.apiBase}
              onChange={(event) => onChange({ apiBase: event.target.value })}
              placeholder={provider.default_api_base ?? "http://localhost:11434/v1"}
              className="h-8 rounded-full text-[13px] sm:col-span-2"
            />
          ) : null}
          {provider.configured ? (
            <button type="button" className="justify-self-start text-[12px] text-muted-foreground underline underline-offset-4 sm:col-span-2" onClick={onToggleKeyEditing}>
              {keyEditing ? tx("settings.actions.cancel", "Cancel") : tx("settings.easySetup.replaceKey", "Replace saved key")}
            </button>
          ) : null}
        </div>
      </SettingsRow>
    </SettingsGroup>
  );
}

