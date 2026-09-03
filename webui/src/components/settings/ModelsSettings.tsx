import { type Dispatch, type SetStateAction } from "react";
import { Check, ChevronDown, Loader2, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { providerDisplayLabel } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";

import {
  defaultPreset,
  editableDefaultProvider,
  normalizeContextWindowTokens,
  settingsProviderConfigured,
  type ModelConfigurationDraft,
} from "@/components/settings/settings-helpers";
import {
  SegmentedControl,
  SettingsFooter,
  SettingsGroup,
  SettingsRow,
} from "@/components/settings/settings-primitives";
import {
  ModelIdPicker,
  ProviderPicker,
  ProviderPickerIcon,
} from "@/components/settings/settings-provider-picker";
import type { AgentSettingsDraft } from "@/components/settings/SettingsView";

const CONTEXT_WINDOW_TOKEN_OPTIONS = [65_536, 200_000, 262_144] as const;

function uniqueProviders(
  providers: SettingsPayload["providers"],
): SettingsPayload["providers"] {
  const seen = new Set<string>();
  return providers.filter((provider) => {
    if (seen.has(provider.name)) return false;
    seen.add(provider.name);
    return true;
  });
}

export function NewModelConfigurationDialog({
  open,
  draft,
  providers,
  saving,
  showProviderLogos,
  onOpenChange,
  onChangeDraft,
  onSave,
}: {
  open: boolean;
  draft: ModelConfigurationDraft;
  providers: Array<{ name: string; label: string }>;
  saving: boolean;
  showProviderLogos: boolean;
  onOpenChange: (open: boolean) => void;
  onChangeDraft: Dispatch<SetStateAction<ModelConfigurationDraft>>;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const canSave = Boolean(draft.label.trim() && draft.provider.trim() && draft.model.trim());

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[520px] rounded-[28px] border-border/55 bg-card/95 p-0 shadow-[0_28px_90px_rgba(15,23,42,0.20)] backdrop-blur-xl dark:border-white/10">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSave();
          }}
        >
          <DialogHeader className="border-b border-border/45 px-5 py-4 text-left">
            <DialogTitle className="text-[18px] font-semibold tracking-[-0.01em]">
              {tx("settings.models.newConfiguration", "New model configuration")}
            </DialogTitle>
            <DialogDescription className="text-[12.5px] leading-5">
              {tx("settings.models.newConfigurationHelp", "Save a provider and model as a one-click option.")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 px-5 py-5">
            <label className="block">
              <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                {tx("settings.models.configurationName", "Configuration name")}
              </span>
              <Input
                autoFocus
                value={draft.label}
                placeholder={tx("settings.models.configurationNamePlaceholder", "Fast writing")}
                onChange={(event) =>
                  onChangeDraft((prev) => ({ ...prev, label: event.target.value }))
                }
                className="h-10 rounded-full px-4 text-[14px]"
              />
            </label>

            <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
              <label className="block">
                <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                  {tx("settings.rows.model", "Model")}
                </span>
                <Input
                  value={draft.model}
                  placeholder="openai/gpt-4.1"
                  onChange={(event) =>
                    onChangeDraft((prev) => ({ ...prev, model: event.target.value }))
                  }
                  className="h-10 rounded-full px-4 text-[14px]"
                />
              </label>
              <div className="block">
                <span className="mb-1.5 block text-[12px] font-medium text-muted-foreground">
                  {tx("settings.rows.provider", "Provider")}
                </span>
                <ProviderPicker
                  providers={providers}
                  value={draft.provider}
                  emptyLabel={tx("settings.byok.noConfiguredProviders", "No configured providers")}
                  showProviderLogos={showProviderLogos}
                  onChange={(provider) =>
                    onChangeDraft((prev) => ({ ...prev, provider }))
                  }
                />
              </div>
            </div>
          </div>

          <DialogFooter className="border-t border-border/45 px-5 py-4 sm:space-x-2">
            <Button
              type="button"
              variant="ghost"
              className="rounded-full"
              disabled={saving}
              onClick={() => onOpenChange(false)}
            >
              {tx("settings.actions.cancel", "Cancel")}
            </Button>
            <Button
              type="submit"
              variant="outline"
              className="rounded-full"
              disabled={!canSave || saving || providers.length === 0}
            >
              {saving ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {saving ? tx("settings.actions.saving", "Saving...") : tx("settings.actions.save", "Save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ModelsSettings({
  token,
  form,
  setForm,
  settings,
  dirty,
  saving,
  showBrandLogos,
  providerSaving,
  onProviderOAuthLogin,
  onSave,
  onCreateConfiguration,
}: {
  token: string;
  form: AgentSettingsDraft;
  setForm: Dispatch<SetStateAction<AgentSettingsDraft>>;
  settings: SettingsPayload;
  dirty: boolean;
  saving: boolean;
  showBrandLogos: boolean;
  providerSaving: string | null;
  onProviderOAuthLogin: (provider: string) => void;
  onSave: () => void;
  onCreateConfiguration: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const configuredProviders = settings.providers.filter((provider) => provider.configured);
  const showAutoProvider = defaultPreset(settings)?.provider === "auto" || form.provider === "auto";
  const selectableProviders = uniqueProviders(configuredProviders);
  const providerOptions = showAutoProvider
    ? [{ name: "auto", label: tx("settings.values.auto", "Auto") }, ...selectableProviders]
    : selectableProviders;
  const providerValue = providerOptions.some((provider) => provider.name === form.provider)
    ? form.provider
    : "";
  const selectedPreset =
    settings.model_presets.find((preset) => preset.name === form.modelPreset) ?? null;
  const selectedProvider = settings.providers.find((provider) => provider.name === form.provider);
  const selectedProviderNeedsSignIn =
    selectedProvider?.auth_type === "oauth" && !selectedProvider.configured;
  const selectedProviderSigningIn = providerSaving === selectedProvider?.name;
  const selectedProviderConfigured = settingsProviderConfigured(settings, form.provider);
  const modelFieldsMissing =
    !form.model.trim() ||
    !form.provider.trim() ||
    Boolean(selectedPreset && !selectedPreset.is_default && !form.presetLabel.trim());
  return (
    <div className="space-y-7">
      <section>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.currentModel", "Current configuration")}
            description={tx("settings.help.currentModel", "Used for new replies.")}
          >
            <ModelPresetPicker
              presets={settings.model_presets}
              value={form.modelPreset}
              settings={settings}
              draftModel={form.model}
              draftProvider={form.provider}
              providerConfigured={selectedProviderConfigured}
              showProviderLogos={showBrandLogos}
              onChange={(modelPreset) => {
                const nextPreset = settings.model_presets.find((preset) => preset.name === modelPreset);
                setForm((prev) => ({
                  ...prev,
                  modelPreset,
                  model: nextPreset?.model ?? prev.model,
                  provider: nextPreset?.is_default
                    ? editableDefaultProvider(settings)
                    : nextPreset?.provider ?? prev.provider,
                  presetLabel: nextPreset?.label ?? modelPreset,
                  contextWindowTokens: normalizeContextWindowTokens(
                    nextPreset?.context_window_tokens ?? prev.contextWindowTokens,
                  ),
                }));
              }}
              onCreateConfiguration={onCreateConfiguration}
            />
          </SettingsRow>
          {selectedPreset && !selectedPreset.is_default ? (
            <SettingsRow
              title={tx("settings.models.configurationName", "Configuration name")}
              description={tx("settings.models.configurationNameHelp", "Rename this saved model configuration.")}
            >
              <Input
                value={form.presetLabel}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, presetLabel: event.target.value }))
                }
                className="h-8 w-[min(280px,70vw)] rounded-full text-[13px]"
              />
            </SettingsRow>
          ) : null}
          <SettingsRow
            title={t("settings.rows.provider")}
            description={t("settings.help.provider")}
          >
            <ProviderPicker
              providers={providerOptions}
              value={providerValue}
              emptyLabel={t("settings.byok.noConfiguredProviders")}
              showProviderLogos={showBrandLogos}
              onChange={(provider) =>
                setForm((prev) => ({
                  ...prev,
                  provider,
                  model: provider === prev.provider ? prev.model : "",
                }))
              }
            />
          </SettingsRow>
          {selectedProviderNeedsSignIn ? (
            <SettingsRow
              title={tx("settings.oauth.signInRequired", "Sign in required")}
              description={tx(
                "settings.oauth.signInBeforeSaving",
                "Sign in before saving this OAuth provider as the active model provider.",
              )}
            >
              <Button
                size="sm"
                variant="outline"
                onClick={() => selectedProvider && onProviderOAuthLogin(selectedProvider.name)}
                disabled={!selectedProvider?.oauth_login_supported || selectedProviderSigningIn}
                className="rounded-full"
              >
                {selectedProviderSigningIn ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                ) : null}
                {selectedProviderSigningIn
                  ? tx("settings.oauth.signingIn", "Signing in...")
                  : tx("settings.oauth.signIn", "Sign in")}
              </Button>
            </SettingsRow>
          ) : null}
          {selectedProviderSigningIn ? (
            <p className="px-3 pb-1 text-[12px] leading-5 text-muted-foreground">
              {tx(
                "settings.oauth.signInWaiting",
                "A sign-in window has opened. This will continue automatically once you finish signing in.",
              )}
            </p>
          ) : null}
          <SettingsRow
            title={t("settings.rows.model")}
            description={t("settings.help.model")}
          >
            <ModelIdPicker
              token={token}
              settings={settings}
              provider={form.provider}
              value={form.model}
              showProviderLogos={showBrandLogos}
              onChange={(model) => setForm((prev) => ({ ...prev, model }))}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.contextWindow", "Context window")}
            description={tx(
              "settings.help.contextWindow",
              "Choose the default context budget for this model configuration.",
            )}
          >
            <SegmentedControl
              value={String(form.contextWindowTokens)}
              options={CONTEXT_WINDOW_TOKEN_OPTIONS.map((tokens) => ({
                value: String(tokens),
                label:
                  tokens === 262_144 ? "256K" : tokens === 200_000 ? "200K" : "64K",
              }))}
              onChange={(value) =>
                setForm((prev) => ({
                  ...prev,
                  contextWindowTokens: normalizeContextWindowTokens(Number(value)),
                }))
              }
            />
          </SettingsRow>
          <SettingsFooter
            dirty={dirty}
            saving={saving}
            saved={false}
            disabled={selectedProviderNeedsSignIn || modelFieldsMissing}
            message={
              selectedProviderNeedsSignIn
                ? tx("settings.oauth.signInBeforeSaving", "Sign in before saving this OAuth provider as the active model provider.")
                : undefined
            }
            onSave={onSave}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}


function modelPresetProviderKey(
  preset: SettingsPayload["model_presets"][number],
  settings: SettingsPayload,
  options: { draftProvider?: string } = {},
): string {
  const provider = options.draftProvider ?? preset.provider;
  if (provider === "auto") {
    return settings.agent.resolved_provider || settings.agent.provider || preset.provider;
  }
  return provider;
}

export function ModelPresetPicker({
  presets,
  value,
  settings,
  draftModel,
  draftProvider,
  providerConfigured,
  showProviderLogos,
  onChange,
  onCreateConfiguration,
}: {
  presets: SettingsPayload["model_presets"];
  value: string;
  settings: SettingsPayload;
  draftModel: string;
  draftProvider: string;
  providerConfigured: boolean;
  showProviderLogos: boolean;
  onChange: (preset: string) => void;
  onCreateConfiguration: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const selectedPreset = presets.find((preset) => preset.name === value) ?? presets[0] ?? null;

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild disabled={!presets.length}>
        <Button
          type="button"
          variant="outline"
          aria-label={tx("settings.rows.currentModel", "Current configuration")}
          disabled={!presets.length}
          className={cn(
            "h-12 w-[min(430px,72vw)] justify-between rounded-full border-input bg-background px-3.5 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          {selectedPreset ? (
            <ModelPresetOptionContent
              preset={selectedPreset}
              settings={settings}
              draftModel={draftModel}
              draftProvider={draftProvider}
              forceUnconfigured={selectedPreset?.is_default ? !providerConfigured : undefined}
              showProviderLogos={showProviderLogos}
              compact
            />
          ) : (
            <span className="truncate text-muted-foreground">
              {tx("settings.models.selectModel", "Select model")}
            </span>
          )}
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[20rem] w-[430px] max-w-[calc(100vw-2rem)] overflow-y-auto scrollbar-thin scrollbar-track-transparent"
      >
        {presets.map((preset) => {
          const selected = preset.name === value;
          return (
            <DropdownMenuItem
              key={preset.name}
              onSelect={() => onChange(preset.name)}
              className={cn(
                "flex cursor-default items-center justify-between gap-3 rounded-[12px] px-2.5 py-2 text-[13px]",
                "focus:bg-muted/85 focus:text-foreground",
                selected && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <ModelPresetOptionContent
                preset={preset}
                settings={settings}
                draftModel={draftModel}
                draftProvider={draftProvider}
                showProviderLogos={showProviderLogos}
              />
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
        <div className="mt-1 border-t border-border/55 pt-1">
          <DropdownMenuItem
            onSelect={() => {
              window.setTimeout(onCreateConfiguration, 0);
            }}
            className={cn(
              "flex cursor-default items-center gap-2 rounded-[12px] px-2.5 py-2 text-[13px] font-medium",
              "text-foreground focus:bg-muted/85 focus:text-foreground",
            )}
          >
            <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground">
              <Plus className="h-3.5 w-3.5" aria-hidden />
            </span>
            <span>{tx("settings.models.addConfiguration", "Add configuration")}</span>
          </DropdownMenuItem>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ModelPresetOptionContent({
  preset,
  settings,
  draftModel,
  draftProvider,
  forceUnconfigured,
  showProviderLogos,
  compact = false,
}: {
  preset: SettingsPayload["model_presets"][number];
  settings: SettingsPayload;
  draftModel: string;
  draftProvider: string;
  forceUnconfigured?: boolean;
  showProviderLogos: boolean;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const provider = modelPresetProviderKey(preset, settings, {
    draftProvider: preset.is_default ? draftProvider : undefined,
  });
  const model = preset.is_default ? draftModel : preset.model;
  const providerName = providerDisplayLabel(settings.providers, provider);
  const providerConfigured =
    forceUnconfigured === undefined
      ? settingsProviderConfigured(settings, provider)
      : !forceUnconfigured;
  const title = providerConfigured ? model || preset.label : tx("settings.values.notConfigured", "Not configured");
  const caption = providerConfigured
    ? `${providerName}${preset.label ? ` · ${preset.label}` : ""}`
    : providerName || model || preset.label
      ? [providerName, model || preset.label].filter(Boolean).join(" · ")
      : tx("settings.byok.noConfiguredProviders", "No configured providers");
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <ProviderPickerIcon
        provider={provider}
        showBrandLogos={showProviderLogos}
        unconfigured={!providerConfigured}
      />
      <span className="min-w-0 text-left leading-tight">
        <span
          className={cn(
            "block truncate font-medium",
            providerConfigured ? "text-foreground" : "text-amber-800 dark:text-amber-200",
          )}
        >
          {title}
        </span>
        <span
          className={cn(
            "mt-0.5 block truncate text-muted-foreground",
            compact ? "text-[11.5px]" : "text-[12px]",
          )}
        >
          {caption}
        </span>
      </span>
    </span>
  );
}

