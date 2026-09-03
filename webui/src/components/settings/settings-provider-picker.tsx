import { useEffect, useState, type ReactNode } from "react";
import {
  Bot,
  Brain,
  Check,
  ChevronDown,
  CircleAlert,
  Cloud,
  Cpu,
  Database,
  Gem,
  Grid3X3,
  Hexagon,
  Layers,
  Loader2,
  Moon,
  Orbit,
  Pencil,
  Search,
  Sparkles,
  Triangle,
  Waves,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { fetchProviderModels } from "@/lib/api";
import { providerBrand } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import type { ProviderModelsPayload, SettingsPayload } from "@/lib/types";

import { settingsProviderConfigured, settingsProviderRow } from "@/components/settings/settings-helpers";

export const DEFERRED_MODEL_LIST_PROVIDERS = new Set([
  "aihubmix",
  "atomic_chat",
  "byteplus",
  "byteplus_coding_plan",
  "huggingface",
  "lm_studio",
  "novita",
  "ollama",
  "openrouter",
  "ovms",
  "siliconflow",
  "vllm",
  "volcengine",
  "volcengine_coding_plan",
]);
export const DEFERRED_MODEL_LIST_QUERY_MIN_LENGTH = 2;
export const PROVIDER_ICONS: Record<string, LucideIcon> = {
  custom: Hexagon,
  openrouter: Sparkles,
  skywork: Sparkles,
  aihubmix: Triangle,
  anthropic: Brain,
  openai: Bot,
  deepseek: Waves,
  zhipu: Grid3X3,
  dashscope: Cloud,
  moonshot: Moon,
  minimax: Zap,
  minimax_anthropic: Brain,
  groq: Cpu,
  huggingface: Layers,
  gemini: Gem,
  mistral: Orbit,
  siliconflow: Layers,
  volcengine: Cloud,
  volcengine_coding_plan: Cloud,
  byteplus: Cloud,
  byteplus_coding_plan: Cloud,
  qianfan: Database,
  ant_ling: Sparkles,
  azure_openai: Cloud,
  bedrock: Database,
  bocha: Search,
  brave: Search,
  duckduckgo: Search,
  exa: Search,
  jina: Search,
  kagi: Search,
  olostep: Search,
  searxng: Search,
  tavily: Search,
  vllm: Cpu,
  ollama: Cpu,
  lm_studio: Cpu,
  atomic_chat: Cpu,
  ovms: Cpu,
  nvidia: Zap,
};
export function ProviderPicker({
  providers,
  value,
  emptyLabel,
  showProviderLogos = false,
  onChange,
}: {
  providers: Array<{ name: string; label: string }>;
  value: string;
  emptyLabel: string;
  showProviderLogos?: boolean;
  onChange: (provider: string) => void;
}) {
  const selectedProvider = providers.find((provider) => provider.name === value) ?? null;
  const disabled = providers.length === 0;

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn(
            "h-8 w-[210px] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
            disabled && "text-muted-foreground",
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            {selectedProvider && showProviderLogos ? (
              <ProviderPickerIcon
                provider={selectedProvider.name}
                showBrandLogos={showProviderLogos}
              />
            ) : null}
            <span className="truncate">{selectedProvider?.label ?? emptyLabel}</span>
          </span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[18rem] w-[240px] overflow-y-auto scrollbar-thin scrollbar-track-transparent"
      >
        {providers.map((provider) => {
          const selected = provider.name === value;
          return (
            <DropdownMenuItem
              key={provider.name}
              onSelect={() => onChange(provider.name)}
              className={cn(
                "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2.5 py-2 text-[13px]",
                "focus:bg-muted/85 focus:text-foreground",
                selected && "bg-muted/80 text-foreground focus:bg-muted",
              )}
            >
              <span className="flex min-w-0 items-center gap-2">
                {showProviderLogos ? (
                  <ProviderPickerIcon
                    provider={provider.name}
                    showBrandLogos={showProviderLogos}
                  />
                ) : null}
                <span className="truncate">{provider.label}</span>
              </span>
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ModelIdPicker({
  token,
  settings,
  provider,
  value,
  showProviderLogos,
  onChange,
}: {
  token: string;
  settings: SettingsPayload;
  provider: string;
  value: string;
  showProviderLogos: boolean;
  onChange: (model: string) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [payload, setPayload] = useState<ProviderModelsPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const effectiveProvider =
    provider === "auto" ? settings.agent.resolved_provider ?? provider : provider;
  const hasConcreteProvider = Boolean(effectiveProvider && effectiveProvider !== "auto");
  const providerRow = settingsProviderRow(settings, effectiveProvider);
  const providerConfigured = settingsProviderConfigured(settings, effectiveProvider);
  const providerRequiresConfiguration = hasConcreteProvider && !providerConfigured;
  const providerUsesManualModelIds =
    hasConcreteProvider && providerConfigured && providerRow?.auth_type === "oauth";
  const canFetchModels =
    hasConcreteProvider && providerConfigured && !providerUsesManualModelIds;
  const normalizedQuery = query.trim().toLowerCase();
  const providerModels = payload?.models ?? [];
  const visibleModels = providerModels
    .filter((model) => {
      if (!normalizedQuery) return true;
      return [model.id, model.label ?? "", model.owned_by ?? ""]
        .some((field) => field.toLowerCase().includes(normalizedQuery));
    })
    .slice(0, 80);
  const isCatalog = payload?.catalog_kind === "catalog";
  const defersModelList = DEFERRED_MODEL_LIST_PROVIDERS.has(effectiveProvider);
  const hasDeferredSearchQuery =
    normalizedQuery.length >= DEFERRED_MODEL_LIST_QUERY_MIN_LENGTH;
  const shouldFetchModels =
    canFetchModels && (!defersModelList || hasDeferredSearchQuery);
  const waitingForModelSearch =
    open && canFetchModels && defersModelList && !hasDeferredSearchQuery;
  const hasModelList = payload?.status === "available";
  const showModels = Boolean(hasModelList && payload && (!isCatalog || normalizedQuery));
  const customCandidate = query.trim();
  const allowCustomModel = !providerRequiresConfiguration;
  const exactQueryMatch = providerModels.some((model) => model.id === customCandidate);
  const providerModelCount = payload?.model_count ?? providerModels.length;
  const modelUnconfigured = !value.trim() || !providerConfigured;

  useEffect(() => {
    if (!open) return;
    setQuery(providerUsesManualModelIds || !hasConcreteProvider ? value : "");
  }, [open, effectiveProvider, hasConcreteProvider, providerUsesManualModelIds, value]);

  useEffect(() => {
    if (!open || !shouldFetchModels) {
      setPayload(null);
      setError(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setPayload(null);
    setError(null);
    setLoading(true);
    fetchProviderModels(token, effectiveProvider)
      .then((nextPayload) => {
        if (!cancelled) setPayload(nextPayload);
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [effectiveProvider, open, shouldFetchModels, token]);

  const selectModel = (model: string) => {
    onChange(model);
    setOpen(false);
  };

  const renderModelRow = (
    model: ProviderModelsPayload["models"][number],
    options: { selected?: boolean } = {},
  ) => (
    <DropdownMenuItem
      key={model.id}
      onSelect={() => selectModel(model.id)}
      className={cn(
        "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-2 py-1.5 text-[12px]",
        "focus:bg-muted/85 focus:text-foreground",
        options.selected && "bg-muted/80 text-foreground focus:bg-muted",
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <ProviderPickerIcon
          provider={effectiveProvider}
          showBrandLogos={showProviderLogos}
          unconfigured={!providerConfigured}
        />
        <span className="min-w-0 truncate font-medium text-foreground">
          {model.label ?? model.id}
        </span>
      </span>
      <span className="ml-2 flex shrink-0 items-center gap-2 text-[11px] text-muted-foreground">
        {model.context_window ? <span>{formatContextWindow(model.context_window)}</span> : null}
        {options.selected ? <Check className="h-3.5 w-3.5 text-foreground" aria-hidden /> : null}
      </span>
    </DropdownMenuItem>
  );

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn(
            "h-9 w-[min(360px,70vw)] justify-between rounded-full border-input bg-background px-3 text-[12px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <span className="flex min-w-0 items-center gap-2">
            <ProviderPickerIcon
              provider={effectiveProvider}
              showBrandLogos={showProviderLogos}
              unconfigured={modelUnconfigured}
            />
            <span
              className={cn(
                "min-w-0 truncate font-medium",
                value ? "text-foreground" : "text-muted-foreground",
              )}
            >
              {value || tx("settings.models.selectModel", "Select model")}
            </span>
          </span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-[360px] max-w-[calc(100vw-2rem)] p-1.5"
      >
        <div className="p-1 pb-1.5">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => event.stopPropagation()}
              placeholder={tx("settings.models.searchModels", "Search or type model ID")}
              className="h-8 rounded-full pl-8 pr-3 text-[12px]"
            />
          </div>
        </div>

        {providerRequiresConfiguration ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.providerNotConfigured", "Configure this provider before loading models.")}
          </div>
        ) : providerUsesManualModelIds ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.unsupportedModelList", "Type a model ID manually.")}
          </div>
        ) : !canFetchModels ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.autoProviderCustomOnly", "Auto provider mode uses custom model IDs.")}
          </div>
        ) : waitingForModelSearch ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.searchCatalog", "Search provider catalog to choose a model.")}
          </div>
        ) : loading ? (
          <div className="flex items-center gap-2 px-2 py-1.5 text-[11px] text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            {tx("settings.models.loadingModels", "Loading models...")}
          </div>
        ) : error || payload?.status === "error" ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {payload?.message || error || tx("settings.models.loadFailed", "Model list unavailable.")}
          </div>
        ) : payload?.status === "not_configured" ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.providerNotConfigured", "Configure this provider before loading models.")}
          </div>
        ) : payload?.status === "unsupported" || payload?.status === "missing_api_base" ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {payload.message || tx("settings.models.unsupportedModelList", "Type a model ID manually.")}
          </div>
        ) : isCatalog && !normalizedQuery ? (
          <div className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {tx("settings.models.searchCatalog", "Search provider catalog to choose a model.")}
            {providerModelCount ? ` ${providerModelCount} ${tx("settings.models.modelsAvailable", "available")}.` : ""}
          </div>
        ) : null}

        {showModels && visibleModels.length ? (
          <div className="max-h-[16rem] overflow-y-auto pr-0.5 scrollbar-thin scrollbar-track-transparent">
            {visibleModels.map((model) =>
              renderModelRow(model, { selected: model.id === value }),
            )}
          </div>
        ) : showModels ? (
          <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
            {tx("settings.models.noModelResults", "No matching models.")}
          </div>
        ) : null}

        {allowCustomModel && customCandidate && !exactQueryMatch && customCandidate !== value ? (
          <>
            {showModels ? <DropdownMenuSeparator /> : null}
            <DropdownMenuItem
              onSelect={() => selectModel(customCandidate)}
              className="flex cursor-default items-center gap-2 rounded-[12px] px-2 py-1.5 text-[12px] focus:bg-muted/85"
            >
              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted/80 text-muted-foreground">
                <Pencil className="h-3 w-3" aria-hidden />
              </span>
              <span className="min-w-0 truncate">
                {tx("settings.models.useCustomModel", "Use")}{" "}
                <span className="font-medium text-foreground">“{customCandidate}”</span>
              </span>
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function formatContextWindow(tokens: number): string {
  if (tokens >= 1_000_000) {
    const value = tokens / 1_000_000;
    return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}M`;
  }
  if (tokens >= 1_000) {
    const value = tokens / 1_000;
    return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}K`;
  }
  return String(tokens);
}

export function ProviderPickerIcon({
  provider,
  showBrandLogos,
  unconfigured = false,
}: {
  provider: string;
  showBrandLogos: boolean;
  unconfigured?: boolean;
}) {
  const [logoIndex, setLogoIndex] = useState(0);
  const brand = providerBrand(provider);
  const Icon = PROVIDER_ICONS[provider] ?? Hexagon;
  const logoUrl = brand?.logoUrls[logoIndex];

  useEffect(() => setLogoIndex(0), [provider]);

  if (unconfigured) {
    return (
      <span
        data-testid="provider-picker-unconfigured-icon"
        className="grid h-5 w-5 shrink-0 place-items-center text-amber-700 dark:text-amber-200"
        aria-hidden
      >
        <CircleAlert className="h-4 w-4" strokeWidth={1.8} />
      </span>
    );
  }

  if (showBrandLogos && logoUrl) {
    return (
      <span
        data-testid={`provider-picker-logo-${provider}`}
        className="grid h-5 w-5 shrink-0 place-items-center overflow-hidden rounded-md border border-border/35 bg-background shadow-[inset_0_0_0_1px_rgba(0,0,0,0.02)]"
        style={{ boxShadow: `inset 0 0 0 1px ${brand.color}22` }}
        aria-hidden
      >
        <img
          src={logoUrl}
          alt=""
          className="h-3.5 w-3.5 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      </span>
    );
  }

  if (showBrandLogos && brand) {
    return (
      <span
        data-testid={`provider-picker-logo-fallback-${provider}`}
        className="grid h-5 w-5 shrink-0 place-items-center rounded-md text-[7.5px] font-semibold text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.18)]"
        style={{ backgroundColor: brand.color }}
        aria-hidden
      >
        {brand.initials}
      </span>
    );
  }

  return (
    <span
      className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"
      aria-hidden
    >
      <Icon className="h-3 w-3" strokeWidth={2} />
    </span>
  );
}

export function ProviderSection({
  title,
  count,
  empty,
  children,
}: {
  title: string;
  count: number;
  empty: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3">
      <ByokSectionHeader title={title} count={count} />
      <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.07)] backdrop-blur-xl dark:border-white/10 dark:shadow-[0_18px_65px_rgba(0,0,0,0.22)]">
        {count > 0 ? (
          <div className="divide-y divide-border/45">{children}</div>
        ) : (
          <ByokEmptyState>{empty}</ByokEmptyState>
        )}
      </div>
    </section>
  );
}

export function ByokSectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center justify-between px-1">
      <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
        {title}
      </h2>
      <span className="rounded-full bg-muted px-2 py-0.5 text-[11.5px] font-medium text-muted-foreground">
        {count}
      </span>
    </div>
  );
}

export function ByokEmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-[18px] border border-dashed border-border/65 bg-card/45 px-4 py-5 text-[13px] text-muted-foreground">
      {children}
    </div>
  );
}

export function ThirdPartyBrandNotice() {
  const { t } = useTranslation();
  return (
    <p className="px-1 text-[11.5px] leading-5 text-muted-foreground/75">
      {t("settings.legal.thirdPartyBrands", {
        defaultValue:
          "Product names, logos, and brands are property of their respective owners. Use is for identification only and does not imply endorsement.",
      })}
    </p>
  );
}

