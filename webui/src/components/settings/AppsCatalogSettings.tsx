import { useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import {
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  Loader2,
  PlayCircle,
  Plus,
  RotateCcw,
  Search,
  Server,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { InstalledToolsPanel } from "@/components/settings/SkillsCatalogSettings";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { logoFallbackUrls } from "@/lib/provider-brand";
import { cn } from "@/lib/utils";
import type {
  CliAppInfo,
  CliAppsPayload,
  InstalledExternalTool,
  McpPresetInfo,
  McpPresetsPayload,
  NanobotFeatureInfo,
  NanobotFeaturesPayload,
} from "@/lib/types";

import {
  AppsActionButton,
  SegmentedControl,
  SettingsSectionTitle,
} from "@/components/settings/settings-primitives";
import { ThirdPartyBrandNotice } from "@/components/settings/settings-provider-picker";
import type { AppsKindFilter, CustomMcpForm, CustomMcpTransport } from "@/components/settings/SettingsView";

type AppsCatalogItem =
  | { id: string; kind: "nanobot"; feature: NanobotFeatureInfo }
  | { id: string; kind: "cli"; app: CliAppInfo }
  | { id: string; kind: "mcp"; preset: McpPresetInfo };

export function InstalledToolsSettings({ installedTools }: { installedTools: InstalledExternalTool[] }) {
  return (
    <div className="space-y-4">
      <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
        App Tools are external programs installed outside nanobot, including GitHub, Homebrew,
        npm, pip, or workspace-local installs. They are separate from Agent Tools such as exec,
        read_file, and run_cli_app. Start, stop, update, and removal requests stay in chat so
        they can route through the matching setup or usage skill.
      </p>
      <InstalledToolsPanel tools={installedTools} />
    </div>
  );
}

export function AppsCatalogSettings({
  cliApps,
  nanobotFeatures,
  mcpPresets,
  cliAppsLoading,
  nanobotFeaturesLoading,
  mcpPresetsLoading,
  query,
  filter,
  cliActionKey,
  nanobotActionKey,
  mcpActionKey,
  cliMessage,
  cliError,
  nanobotMessage,
  nanobotError,
  cliFocusName,
  mcpMessage,
  mcpError,
  mcpFieldValues,
  customMcpForm,
  mcpConfigImport,
  showBrandLogos,
  requiresRestartPending,
  onQueryChange,
  onFilterChange,
  onCliAction,
  onNanobotAction,
  onMcpAction,
  onDismissStatus,
  onBackToChat,
  onMcpFieldChange,
  onCustomMcpFormChange,
  onMcpConfigImportChange,
  onSaveCustomMcp,
  onImportMcpConfig,
  onMcpToolsChange,
  onRestart,
  isRestarting,
}: {
  cliApps: CliAppsPayload | null;
  nanobotFeatures: NanobotFeaturesPayload | null;
  mcpPresets: McpPresetsPayload | null;
  cliAppsLoading: boolean;
  nanobotFeaturesLoading: boolean;
  mcpPresetsLoading: boolean;
  query: string;
  filter: AppsKindFilter;
  cliActionKey: string | null;
  nanobotActionKey: string | null;
  mcpActionKey: string | null;
  cliMessage: string | null;
  cliError: string | null;
  nanobotMessage: string | null;
  nanobotError: string | null;
  cliFocusName: string | null;
  mcpMessage: string | null;
  mcpError: string | null;
  mcpFieldValues: Record<string, Record<string, string>>;
  customMcpForm: CustomMcpForm;
  mcpConfigImport: string;
  showBrandLogos: boolean;
  requiresRestartPending: boolean;
  onQueryChange: (value: string) => void;
  onFilterChange: (value: AppsKindFilter) => void;
  onCliAction: (action: "install" | "update" | "uninstall" | "test", name: string) => void;
  onNanobotAction: (action: "enable" | "disable", name: string) => void;
  onMcpAction: (action: "enable" | "remove" | "test", name: string, values?: Record<string, string>) => void;
  onDismissStatus: () => void;
  onBackToChat: () => void;
  onMcpFieldChange: (presetName: string, fieldName: string, value: string) => void;
  onCustomMcpFormChange: Dispatch<SetStateAction<CustomMcpForm>>;
  onMcpConfigImportChange: (value: string) => void;
  onSaveCustomMcp: () => void;
  onImportMcpConfig: () => void;
  onMcpToolsChange: (name: string, enabledTools: string[]) => void;
  onRestart?: () => void;
  isRestarting?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const filterOptions = [
    { value: "all", label: tx("settings.apps.filterAll", "All") },
    { value: "nanobot", label: tx("settings.apps.filterPlugins", "Plugins") },
    { value: "cli", label: tx("settings.apps.filterCli", "App CLIs") },
    { value: "mcp", label: tx("settings.apps.filterMcp", "MCP services") },
  ];
  const normalizedQuery = query.trim().toLowerCase();
  const items: AppsCatalogItem[] = [
    ...(nanobotFeatures?.features ?? []).map((feature) => ({
      id: `nanobot:${feature.name}`,
      kind: "nanobot" as const,
      feature,
    })),
    ...(cliApps?.apps ?? []).map((app) => ({ id: `cli:${app.name}`, kind: "cli" as const, app })),
    ...(mcpPresets?.presets ?? []).map((preset) => ({
      id: `mcp:${preset.name}`,
      kind: "mcp" as const,
      preset,
    })),
  ]
    .filter((item) => filter === "all" || item.kind === filter)
    .filter((item) => !normalizedQuery || appsSearchText(item).includes(normalizedQuery))
    .sort((left, right) => {
      const rank = Number(!appsReady(left)) - Number(!appsReady(right));
      return rank || appsTitle(left).localeCompare(appsTitle(right));
    });
  const focusedApp = cliFocusName
    ? (cliApps?.apps ?? []).find((app) => app.name === cliFocusName && app.installed)
    : null;
  const loading =
    (cliAppsLoading || nanobotFeaturesLoading || mcpPresetsLoading) &&
    !cliApps &&
    !nanobotFeatures &&
    !mcpPresets;
  const statusMessage =
    cliError ||
    nanobotError ||
    mcpError ||
    (!focusedApp ? cliMessage || nanobotMessage || mcpMessage : null);
  const statusIsError = Boolean(cliError || nanobotError || mcpError);
  const caption = t("settings.apps.caption", {
    plugins: nanobotFeatures?.enabled_count ?? 0,
    cli: cliApps?.installed_count ?? 0,
    mcp: mcpPresets?.installed_count ?? 0,
    defaultValue: "{{plugins}} Plugin · {{cli}} CLI · {{mcp}} MCP",
  });

  return (
    <div className="space-y-7">
      <section className="space-y-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
            {tx(
              "settings.apps.description",
              "Enable plugins, local app adapters, and connected tool servers.",
            )}
          </p>
          <span className="text-[12px] font-medium text-muted-foreground">{caption}</span>
        </div>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
              placeholder={tx("settings.apps.searchPlaceholder", "Search Apps")}
              className="h-12 rounded-[14px] border-border/70 bg-card/90 pl-11 text-[15px] shadow-sm"
            />
          </div>
          <SegmentedControl
            value={filter}
            options={filterOptions}
            onChange={(value) => onFilterChange(value as AppsKindFilter)}
          />
        </div>
      </section>

      {statusMessage ? (
        <div
          className={cn(
            "flex items-center justify-between gap-3 rounded-[12px] border py-2.5 pl-4 pr-2 text-[13px]",
            statusIsError
              ? "border-destructive/20 bg-destructive/5 text-destructive"
              : "border-border/55 bg-muted/35 text-muted-foreground",
          )}
        >
          <span className="min-w-0">{statusMessage}</span>
          <button
            type="button"
            aria-label={tx("settings.actions.dismiss", "Dismiss")}
            title={tx("settings.actions.dismiss", "Dismiss")}
            onClick={onDismissStatus}
            className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors",
              statusIsError
                ? "text-destructive/70 hover:bg-destructive/10 hover:text-destructive"
                : "text-muted-foreground/70 hover:bg-muted hover:text-foreground",
            )}
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      ) : null}

      {focusedApp ? (
        <CliAppReadyPanel app={focusedApp} showBrandLogos={showBrandLogos} onBackToChat={onBackToChat} />
      ) : null}

      {requiresRestartPending ? (
        <div className="flex flex-col gap-3 rounded-[12px] border border-amber-500/20 bg-amber-500/8 px-4 py-3 text-[12.5px] text-amber-800 dark:text-amber-200 sm:flex-row sm:items-center sm:justify-between">
          <span>{tx("settings.apps.restartRequired", "Restart nanobot to apply updated apps and features.")}</span>
          {onRestart ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onRestart}
              disabled={isRestarting}
              className="h-8 rounded-full bg-background/80 px-3 text-[12px] font-semibold"
            >
              {isRestarting ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              {isRestarting ? t("app.system.restarting") : t("app.system.restart")}
            </Button>
          ) : null}
        </div>
      ) : null}

      <section>
        <div className="flex items-center justify-between border-b border-border/45 pb-3">
          <SettingsSectionTitle>{tx("settings.apps.featured", "Catalog")}</SettingsSectionTitle>
          <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
            {items.length}
          </span>
        </div>
        {loading ? (
          <div className="flex h-36 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
            {tx("settings.apps.loading", "Loading Apps...")}
          </div>
        ) : items.length ? (
          <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
            {items.map((item) =>
              item.kind === "nanobot" ? (
                <NanobotFeatureCatalogRow
                  key={item.id}
                  feature={item.feature}
                  actionKey={nanobotActionKey}
                  onAction={onNanobotAction}
                />
              ) : item.kind === "cli" ? (
                <CliAppsCatalogRow
                  key={item.id}
                  app={item.app}
                  actionKey={cliActionKey}
                  showBrandLogos={showBrandLogos}
                  onAction={onCliAction}
                />
              ) : (
                <McpAppsCatalogRow
                  key={item.id}
                  preset={item.preset}
                  values={mcpFieldValues[item.preset.name] ?? {}}
                  actionKey={mcpActionKey}
                  showBrandLogos={showBrandLogos}
                  onFieldChange={onMcpFieldChange}
                  onAction={onMcpAction}
                  onToolsChange={onMcpToolsChange}
                />
              ),
            )}
          </div>
        ) : (
          <div className="px-3 py-12 text-center text-sm text-muted-foreground">
            {tx("settings.apps.empty", "No apps match this filter.")}
          </div>
        )}
      </section>

      {filter === "all" || filter === "mcp" ? (
        <McpCustomServerPanel
          form={customMcpForm}
          configImport={mcpConfigImport}
          actionKey={mcpActionKey}
          onFormChange={onCustomMcpFormChange}
          onConfigImportChange={onMcpConfigImportChange}
          onSave={onSaveCustomMcp}
          onImportConfig={onImportMcpConfig}
        />
      ) : null}

      <ThirdPartyBrandNotice />
    </div>
  );
}

export function NanobotFeatureCatalogRow({
  feature,
  actionKey,
  onAction,
}: {
  feature: NanobotFeatureInfo;
  actionKey: string | null;
  onAction: (action: "enable" | "disable", name: string) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const enableBusy = actionKey === `enable:${feature.name}`;
  const disableBusy = actionKey === `disable:${feature.name}`;
  const description = nanobotFeatureStatusLabel(feature, tx);
  const missingSupport = feature.enabled && !feature.installed;
  const installSupportLabel = tx("settings.nanobotFeatures.installSupport", "Install support");
  const enabledLabel =
    feature.type === "channel" && feature.name === "websocket"
      ? tx("settings.nanobotFeatures.websocketRequired", "Required for WebUI")
      : tx("settings.nanobotFeatures.enabled", "Enabled");
  const enableLabel = feature.installed
    ? tx("settings.nanobotFeatures.enable", "Enable")
    : installSupportLabel;

  return (
    <article className="group flex min-w-0 items-center gap-3 rounded-[14px] px-3 py-3 transition-colors hover:bg-muted/45">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] border border-border/55 bg-card text-muted-foreground shadow-sm">
        <Bot className="h-4 w-4" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <h3 className="truncate text-[14px] font-semibold leading-5 text-foreground">
            {feature.display_name}
          </h3>
          <AppsTypeBadge>
            {feature.type === "channel"
              ? tx("settings.apps.channelLabel", "Channel")
              : tx("settings.apps.featureLabel", "Feature")}
          </AppsTypeBadge>
        </div>
        <p className="mt-0.5 truncate text-[12.5px] leading-5 text-muted-foreground">{description}</p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {missingSupport && feature.install_supported ? (
          <AppsActionButton
            ariaLabel={installSupportLabel}
            busy={enableBusy}
            onClick={() => onAction("enable", feature.name)}
          >
            <Plus className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        ) : feature.enabled && feature.type === "channel" && feature.name !== "websocket" ? (
          <AppsActionButton
            ariaLabel={tx("settings.nanobotFeatures.disable", "Disable")}
            busy={disableBusy}
            tone="danger"
            onClick={() => onAction("disable", feature.name)}
          >
            <X className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        ) : feature.enabled ? (
          <AppsActionButton
            ariaLabel={enabledLabel}
            disabled
            tone="installed"
          >
            <Check className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        ) : feature.install_supported ? (
          <AppsActionButton
            ariaLabel={enableLabel}
            busy={enableBusy}
            onClick={() => onAction("enable", feature.name)}
          >
            <Plus className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        ) : (
          <AppsActionButton ariaLabel={tx("settings.cliApps.unavailable", "Unavailable")} disabled>
            <Plus className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        )}
      </div>
    </article>
  );
}

export function CliAppsCatalogRow({
  app,
  actionKey,
  showBrandLogos,
  onAction,
}: {
  app: CliAppInfo;
  actionKey: string | null;
  showBrandLogos: boolean;
  onAction: (action: "install" | "update" | "uninstall" | "test", name: string) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const installBusy = actionKey === `install:${app.name}`;
  const updateBusy = actionKey === `update:${app.name}`;
  const uninstallBusy = actionKey === `uninstall:${app.name}`;
  const testBusy = actionKey === `test:${app.name}`;
  const busy = installBusy || updateBusy || uninstallBusy || testBusy;
  const description = app.description || app.requires || app.entry_point || app.name;

  return (
    <article className="group flex min-w-0 items-center gap-3 rounded-[14px] px-3 py-3 transition-colors hover:bg-muted/45">
      <CliAppLogo app={app} showBrandLogos={showBrandLogos} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <h3 className="truncate text-[14px] font-semibold leading-5 text-foreground">{app.display_name}</h3>
          <AppsTypeBadge>{tx("settings.apps.cliLabel", "CLI")}</AppsTypeBadge>
        </div>
        <p className="mt-0.5 truncate text-[12.5px] leading-5 text-muted-foreground">{description}</p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {app.installed ? (
          <>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <AppsActionButton
                  ariaLabel={tx("settings.cliApps.statusInstalled", "CLI installed")}
                  busy={testBusy || updateBusy}
                  disabled={busy}
                  tone="installed"
                >
                  <Check className="h-4 w-4" aria-hidden />
                </AppsActionButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem disabled={busy} onClick={() => onAction("test", app.name)}>
                  <PlayCircle className="mr-2 h-3.5 w-3.5" aria-hidden />
                  {tx("settings.cliApps.test", "Test CLI")}
                </DropdownMenuItem>
                <DropdownMenuItem disabled={busy} onClick={() => onAction("update", app.name)}>
                  <RotateCcw className="mr-2 h-3.5 w-3.5" aria-hidden />
                  {tx("settings.cliApps.update", "Update CLI")}
                </DropdownMenuItem>
                <DropdownMenuItem disabled={busy} onClick={() => onAction("uninstall", app.name)}>
                  <Trash2 className="mr-2 h-3.5 w-3.5" aria-hidden />
                  {tx("settings.cliApps.uninstall", "Uninstall CLI")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <AppsActionButton
              ariaLabel={tx("settings.cliApps.uninstall", "Uninstall CLI")}
              busy={uninstallBusy}
              disabled={busy && !uninstallBusy}
              tone="danger"
              onClick={() => onAction("uninstall", app.name)}
            >
              <Trash2 className="h-4 w-4" aria-hidden />
            </AppsActionButton>
          </>
        ) : app.install_supported ? (
          <AppsActionButton
            ariaLabel={tx("settings.cliApps.install", "Install CLI")}
            busy={installBusy}
            onClick={() => onAction("install", app.name)}
          >
            <Plus className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        ) : (
          <AppsActionButton ariaLabel={tx("settings.cliApps.unavailable", "Unavailable")} disabled>
            <Plus className="h-4 w-4" aria-hidden />
          </AppsActionButton>
        )}
      </div>
    </article>
  );
}

export function McpAppsCatalogRow({
  preset,
  values,
  actionKey,
  showBrandLogos,
  onFieldChange,
  onAction,
  onToolsChange,
}: {
  preset: McpPresetInfo;
  values: Record<string, string>;
  actionKey: string | null;
  showBrandLogos: boolean;
  onFieldChange: (presetName: string, fieldName: string, value: string) => void;
  onAction: (action: "enable" | "remove" | "test", name: string, values?: Record<string, string>) => void;
  onToolsChange: (name: string, enabledTools: string[]) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [setupOpen, setSetupOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const enableBusy = actionKey === `enable:${preset.name}`;
  const removeBusy = actionKey === `remove:${preset.name}`;
  const testBusy = actionKey === `test:${preset.name}`;
  const toolsBusy = actionKey === `tools:${preset.name}`;
  const busy = enableBusy || removeBusy || testBusy || toolsBusy;
  const missingFields = preset.required_fields.filter((field) => field.required && !field.configured);
  const hasFields = preset.required_fields.length > 0;
  const needsSetupInput = missingFields.length > 0;
  const readyInstalled = preset.installed && preset.configured;
  const canEnable =
    preset.install_supported &&
    (missingFields.length === 0 || missingFields.every((field) => Boolean(values[field.name]?.trim())));
  const toolNames = preset.tool_names ?? [];
  const enabledTools = preset.enabled_tools ?? ["*"];
  const allowAllTools = enabledTools.includes("*");
  const enabledSet = new Set(allowAllTools ? toolNames : enabledTools);
  const description = preset.description || preset.note || preset.requires || preset.name;
  const statusLabel = mcpPresetStatusLabel(preset.status, tx);

  useEffect(() => {
    if (preset.configured || !preset.install_supported) setSetupOpen(false);
  }, [preset.configured, preset.install_supported]);

  const enableOrOpenSetup = () => {
    if (needsSetupInput || (preset.installed && !preset.configured && hasFields)) {
      setSetupOpen(true);
      return;
    }
    onAction("enable", preset.name, values);
  };
  const submitSetup = () => {
    if (!canEnable) return;
    onAction("enable", preset.name, values);
  };
  const setTools = (next: string[]) => onToolsChange(preset.name, next);
  const toggleTool = (toolName: string) => {
    const next = new Set(allowAllTools ? toolNames : enabledTools);
    if (next.has(toolName)) next.delete(toolName);
    else next.add(toolName);
    const nextValues = Array.from(next);
    setTools(nextValues.length === toolNames.length ? ["*"] : nextValues);
  };

  return (
    <article className="rounded-[14px] transition-colors hover:bg-muted/45">
      <div className="group flex min-w-0 items-center gap-3 px-3 py-3">
        <McpPresetLogo preset={preset} showBrandLogos={showBrandLogos} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-baseline gap-2">
            <h3 className="truncate text-[14px] font-semibold leading-5 text-foreground">{preset.display_name}</h3>
            <AppsTypeBadge>{tx("settings.apps.mcpLabel", "MCP")}</AppsTypeBadge>
          </div>
          <p className="mt-0.5 truncate text-[12.5px] leading-5 text-muted-foreground">{description}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {readyInstalled ? (
            <>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <AppsActionButton
                    ariaLabel={statusLabel}
                    busy={testBusy || toolsBusy}
                    disabled={busy}
                    tone="installed"
                  >
                    <Check className="h-4 w-4" aria-hidden />
                  </AppsActionButton>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem disabled={busy} onClick={() => onAction("test", preset.name)}>
                    <PlayCircle className="mr-2 h-3.5 w-3.5" aria-hidden />
                    {tx("settings.mcp.test", "Test")}
                  </DropdownMenuItem>
                  {toolNames.length ? (
                    <DropdownMenuItem disabled={busy} onClick={() => setToolsOpen((open) => !open)}>
                      <SlidersHorizontal className="mr-2 h-3.5 w-3.5" aria-hidden />
                      {tx("settings.mcp.toolScope", "Tools")}
                    </DropdownMenuItem>
                  ) : null}
                  <DropdownMenuItem disabled={busy} onClick={() => onAction("remove", preset.name)}>
                    <Trash2 className="mr-2 h-3.5 w-3.5" aria-hidden />
                    {tx("settings.mcp.remove", "Remove")}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <AppsActionButton
                ariaLabel={tx("settings.mcp.remove", "Remove")}
                busy={removeBusy}
                disabled={busy && !removeBusy}
                tone="danger"
                onClick={() => onAction("remove", preset.name)}
              >
                <Trash2 className="h-4 w-4" aria-hidden />
              </AppsActionButton>
            </>
          ) : preset.installed && !preset.configured ? (
            <AppsActionButton
              ariaLabel={hasFields ? tx("settings.mcp.configure", "Configure") : tx("settings.mcp.enable", "Enable")}
              busy={enableBusy}
              onClick={() => {
                if (hasFields) setSetupOpen(true);
                else onAction("enable", preset.name, values);
              }}
            >
              <Plus className="h-4 w-4" aria-hidden />
            </AppsActionButton>
          ) : preset.install_supported ? (
            <AppsActionButton
              ariaLabel={needsSetupInput ? tx("settings.mcp.setup", "Set up") : tx("settings.mcp.enable", "Enable")}
              busy={enableBusy}
              onClick={enableOrOpenSetup}
            >
              <Plus className="h-4 w-4" aria-hidden />
            </AppsActionButton>
          ) : (
            <AppsActionButton ariaLabel={tx("settings.mcp.comingSoon", "Coming soon")} disabled>
              <Plus className="h-4 w-4" aria-hidden />
            </AppsActionButton>
          )}
        </div>
      </div>

      {setupOpen && preset.install_supported && hasFields ? (
        <div className="mx-3 mb-3 rounded-[14px] border border-border/45 bg-card/85 p-3 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-[12.5px] font-semibold text-foreground">
                {t("settings.mcp.connectTitle", {
                  name: preset.display_name,
                  defaultValue: "Connect {{name}}",
                })}
              </div>
              <p className="mt-0.5 text-[11.5px] text-muted-foreground">
                {tx("settings.mcp.connectHint", "Add the key from your account settings.")}
              </p>
            </div>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => setSetupOpen(false)}
              className="h-7 rounded-full px-2.5 text-[11.5px] font-semibold text-muted-foreground"
            >
              {tx("actions.cancel", "Cancel")}
            </Button>
          </div>
          <div className="mt-3 grid gap-2">
            {preset.required_fields.map((field) => (
              <label key={field.name} className="min-w-0">
                <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">
                  {field.label}
                  {field.configured ? (
                    <span className="ml-1 font-normal text-emerald-600 dark:text-emerald-300">
                      {tx("settings.mcp.configured", "configured")}
                    </span>
                  ) : null}
                </span>
                <Input
                  type={field.secret ? "password" : "text"}
                  value={values[field.name] ?? ""}
                  onChange={(event) => onFieldChange(preset.name, field.name, event.target.value)}
                  placeholder={
                    field.configured
                      ? tx("settings.mcp.keepExisting", "Leave blank to keep existing")
                      : field.placeholder
                  }
                  className="h-9 rounded-full bg-background/80 text-[12.5px]"
                />
              </label>
            ))}
          </div>
          <div className="mt-3 flex justify-end">
            <Button
              type="button"
              size="sm"
              disabled={busy || !canEnable}
              onClick={submitSetup}
              className="h-8 rounded-full px-3 text-[12px] font-semibold"
            >
              {enableBusy ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              {preset.installed
                ? tx("settings.mcp.updateSetup", "Update setup")
                : tx("settings.mcp.saveAndEnable", "Save and enable")}
            </Button>
          </div>
        </div>
      ) : null}

      {toolsOpen && readyInstalled && toolNames.length ? (
        <div className="mx-3 mb-3 rounded-[14px] border border-border/45 bg-card/85 p-3 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-[11.5px] font-medium text-muted-foreground">
              {tx("settings.mcp.toolScope", "Tools")}
            </div>
            <div className="flex items-center gap-1">
              <Button
                type="button"
                size="sm"
                variant={allowAllTools ? "default" : "outline"}
                disabled={toolsBusy}
                onClick={() => setTools(["*"])}
                className="h-7 rounded-full px-2.5 text-[11.5px] font-semibold"
              >
                {tx("settings.mcp.allTools", "All")}
              </Button>
              <Button
                type="button"
                size="sm"
                variant={!allowAllTools && enabledSet.size === 0 ? "default" : "outline"}
                disabled={toolsBusy}
                onClick={() => setTools([])}
                className="h-7 rounded-full px-2.5 text-[11.5px] font-semibold"
              >
                {tx("settings.mcp.noTools", "None")}
              </Button>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {toolNames.map((toolName) => {
              const selected = enabledSet.has(toolName);
              return (
                <button
                  key={toolName}
                  type="button"
                  disabled={toolsBusy}
                  onClick={() => toggleTool(toolName)}
                  className={cn(
                    "max-w-full rounded-full border px-2.5 py-1 font-mono text-[11px] transition-colors",
                    selected
                      ? "border-blue-500/25 bg-blue-500/10 text-blue-700 dark:text-blue-300"
                      : "border-border/55 bg-muted/30 text-muted-foreground hover:bg-muted/60",
                  )}
                >
                  <span className="block max-w-[220px] truncate">{toolName}</span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </article>
  );
}

export function AppsTypeBadge({ children }: { children: ReactNode }) {
  return (
    <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-none tracking-[0.06em] text-muted-foreground">
      {children}
    </span>
  );
}

function appsTitle(item: AppsCatalogItem): string {
  if (item.kind === "nanobot") return item.feature.display_name;
  return item.kind === "cli" ? item.app.display_name : item.preset.display_name;
}

function appsReady(item: AppsCatalogItem): boolean {
  if (item.kind === "nanobot") return item.feature.enabled;
  return item.kind === "cli" ? item.app.installed : item.preset.installed && item.preset.configured;
}

function appsSearchText(item: AppsCatalogItem): string {
  if (item.kind === "nanobot") {
    const feature = item.feature;
    return [
      feature.display_name,
      feature.name,
      feature.type,
      feature.status,
    ]
      .join(" ")
      .toLowerCase();
  }
  if (item.kind === "cli") {
    const app = item.app;
    return [
      app.display_name,
      app.name,
      app.category,
      app.description,
      app.requires,
      app.entry_point,
      app.source,
    ]
      .join(" ")
      .toLowerCase();
  }
  const preset = item.preset;
  return [
    preset.display_name,
    preset.name,
    preset.category,
    preset.description,
    preset.requires,
    preset.note,
    preset.transport,
    preset.source ?? "",
  ]
    .join(" ")
      .toLowerCase();
}

function nanobotFeatureStatusLabel(
  feature: NanobotFeatureInfo,
  tx: (key: string, fallback: string) => string,
): string {
  if (feature.ready && feature.type === "channel" && feature.name === "websocket") {
    return tx("settings.nanobotFeatures.websocketRequired", "Required for WebUI");
  }
  if (feature.ready) return tx("settings.nanobotFeatures.ready", "Ready");
  if (!feature.installed) return tx("settings.nanobotFeatures.missingDependency", "Support missing");
  if (feature.type === "channel") return tx("settings.nanobotFeatures.channelDisabled", "Channel is disabled");
  return tx("settings.nanobotFeatures.notEnabled", "Not enabled");
}

export function McpCustomServerPanel({
  form,
  configImport,
  actionKey,
  onFormChange,
  onConfigImportChange,
  onSave,
  onImportConfig,
}: {
  form: CustomMcpForm;
  configImport: string;
  actionKey: string | null;
  onFormChange: Dispatch<SetStateAction<CustomMcpForm>>;
  onConfigImportChange: (value: string) => void;
  onSave: () => void;
  onImportConfig: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [activeMode, setActiveMode] = useState<"custom" | "import" | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const customBusy = actionKey?.startsWith("custom:") ?? false;
  const importBusy = actionKey === "import" || actionKey === "import-cursor";
  const remote = form.transport !== "stdio";
  const canSave = Boolean(form.name.trim()) && (remote ? Boolean(form.url.trim()) : Boolean(form.command.trim()));
  const update = <K extends keyof CustomMcpForm>(key: K, value: CustomMcpForm[K]) => {
    onFormChange((prev) => ({ ...prev, [key]: value }));
  };
  const transports: Array<{ value: CustomMcpTransport; label: string }> = [
    { value: "stdio", label: "stdio" },
    { value: "streamableHttp", label: "HTTP" },
    { value: "sse", label: "SSE" },
  ];

  return (
    <section className="overflow-hidden rounded-[16px] border border-border/45 bg-card/72 shadow-[0_10px_30px_rgba(15,23,42,0.045)]">
      <div className="flex flex-col gap-3 px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-[11px] bg-muted text-muted-foreground">
            <Server className="h-4 w-4" aria-hidden />
          </span>
          <div className="min-w-0">
            <h3 className="text-[13px] font-semibold leading-5 text-foreground">
              {tx("settings.mcp.moreOptions", "More MCP options")}
            </h3>
            <p className="truncate text-[12px] text-muted-foreground">
              {tx("settings.mcp.moreOptionsSubtitle", "Add a custom server or import mcp.json.")}
            </p>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:flex sm:shrink-0">
          <Button
            type="button"
            size="sm"
            variant={activeMode === "custom" ? "default" : "outline"}
            onClick={() => setActiveMode((mode) => (mode === "custom" ? null : "custom"))}
            className="h-8 rounded-full px-3 text-[12px] font-semibold"
          >
            <Server className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {tx("settings.mcp.customAction", "Custom")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant={activeMode === "import" ? "default" : "outline"}
            onClick={() => setActiveMode((mode) => (mode === "import" ? null : "import"))}
            className="h-8 rounded-full px-3 text-[12px] font-semibold"
          >
            <Database className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {tx("settings.mcp.importAction", "Import")}
          </Button>
        </div>
      </div>

      {activeMode === "custom" ? (
        <div className="border-t border-border/35 bg-muted/18 px-3 py-3">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end">
            <label className="min-w-0 flex-1">
              <span className="mb-1.5 block text-[11.5px] font-medium text-muted-foreground">
                {tx("settings.mcp.serverName", "Server name")}
              </span>
              <Input
                value={form.name}
                onChange={(event) => update("name", event.target.value)}
                placeholder="docs"
                className="h-9 rounded-full bg-background/80 text-[12.5px]"
              />
            </label>
            <div className="min-w-[228px]">
              <span className="mb-1.5 block text-[11.5px] font-medium text-muted-foreground">
                {tx("settings.mcp.transport", "Transport")}
              </span>
              <SegmentedControl
                value={form.transport}
                options={transports}
                onChange={(value) => update("transport", value as CustomMcpTransport)}
              />
            </div>
            {remote ? (
              <label className="min-w-0 flex-[1.4]">
                <span className="mb-1.5 block text-[11.5px] font-medium text-muted-foreground">
                  {tx("settings.mcp.serverUrl", "URL")}
                </span>
                <Input
                  value={form.url}
                  onChange={(event) => update("url", event.target.value)}
                  placeholder={form.transport === "sse" ? "https://example.com/sse" : "https://example.com/mcp"}
                  className="h-9 rounded-full bg-background/80 text-[12.5px]"
                />
              </label>
            ) : (
              <label className="min-w-0 flex-[1.4]">
                <span className="mb-1.5 block text-[11.5px] font-medium text-muted-foreground">
                  {tx("settings.mcp.command", "Command")}
                </span>
                <Input
                  value={form.command}
                  onChange={(event) => update("command", event.target.value)}
                  placeholder="npx"
                  className="h-9 rounded-full bg-background/80 text-[12.5px]"
                />
              </label>
            )}
            <Button
              type="button"
              size="sm"
              onClick={onSave}
              disabled={!canSave || customBusy}
              className="h-9 shrink-0 rounded-full px-4 text-[12.5px] font-semibold"
            >
              {customBusy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
              {tx("settings.mcp.saveCustom", "Save MCP")}
            </Button>
          </div>

          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => setAdvancedOpen((open) => !open)}
            className="mt-2 h-8 rounded-full px-2 text-[12px] font-medium text-muted-foreground hover:text-foreground"
          >
            <ChevronDown
              className={cn("mr-1.5 h-3.5 w-3.5 transition-transform", advancedOpen ? "rotate-180" : "")}
              aria-hidden
            />
            {advancedOpen
              ? tx("settings.mcp.hideAdvanced", "Hide advanced")
              : tx("settings.mcp.advancedOptions", "Advanced options")}
          </Button>

          {advancedOpen ? (
            <div className="mt-2 grid gap-2 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_180px]">
              {!remote ? (
                <label className="min-w-0">
                  <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">
                    {tx("settings.mcp.args", "Args JSON")}
                  </span>
                  <Textarea
                    value={form.args}
                    onChange={(event) => update("args", event.target.value)}
                    placeholder={'["-y", "docs-mcp"]'}
                    className="min-h-[68px] resize-y rounded-[12px] bg-background/80 font-mono text-[12px]"
                  />
                </label>
              ) : (
                <label className="min-w-0">
                  <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">
                    {tx("settings.mcp.headers", "Headers JSON")}
                  </span>
                  <Textarea
                    value={form.headers}
                    onChange={(event) => update("headers", event.target.value)}
                    placeholder={'{"Authorization":"Bearer ..."}'}
                    className="min-h-[68px] resize-y rounded-[12px] bg-background/80 font-mono text-[12px]"
                  />
                </label>
              )}
              <label className="min-w-0">
                <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">
                  {tx("settings.mcp.env", "Env JSON")}
                </span>
                <Textarea
                  value={form.env}
                  onChange={(event) => update("env", event.target.value)}
                  placeholder={'{"API_KEY":"..."}'}
                  className="min-h-[68px] resize-y rounded-[12px] bg-background/80 font-mono text-[12px]"
                />
              </label>
              <label className="min-w-0">
                <span className="mb-1 block text-[11.5px] font-medium text-muted-foreground">
                  {tx("settings.mcp.timeout", "Tool timeout")}
                </span>
                <Input
                  value={form.toolTimeout}
                  onChange={(event) => update("toolTimeout", event.target.value)}
                  inputMode="numeric"
                  className="h-9 rounded-full bg-background/80 text-[12.5px]"
                />
              </label>
            </div>
          ) : null}
        </div>
      ) : null}

      {activeMode === "import" ? (
        <div className="border-t border-border/35 bg-muted/18 px-3 py-3">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-end">
            <label className="min-w-0 flex-1">
              <span className="mb-1.5 block text-[11.5px] font-medium text-muted-foreground">
                {tx("settings.mcp.configImport", "Import mcp.json")}
              </span>
              <Textarea
                value={configImport}
                onChange={(event) => onConfigImportChange(event.target.value)}
                placeholder={'{"mcpServers":{"docs":{"command":"npx","args":["-y","docs-mcp"]}}}'}
                className="min-h-[84px] resize-y rounded-[12px] bg-background/80 font-mono text-[12px]"
              />
            </label>
            <Button
              type="button"
              size="sm"
              onClick={onImportConfig}
              disabled={!configImport.trim() || importBusy}
              className="h-9 shrink-0 rounded-full px-4 text-[12.5px] font-semibold"
            >
              {importBusy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <Database className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
              {tx("settings.mcp.importConfig", "Import")}
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function mcpPresetStatusLabel(status: string, tx: (key: string, fallback: string) => string): string {
  switch (status) {
    case "configured":
      return tx("settings.mcp.statusConfigured", "Configured");
    case "missing_credentials":
      return tx("settings.mcp.statusMissingCredentials", "Needs key");
    case "missing_dependency":
      return tx("settings.mcp.statusMissingDependency", "Needs dependency");
    case "coming_soon":
      return tx("settings.mcp.statusComingSoon", "Coming soon");
    default:
      return tx("settings.mcp.statusNotInstalled", "Not enabled");
  }
}

export function McpPresetLogo({ preset, showBrandLogos }: { preset: McpPresetInfo; showBrandLogos: boolean }) {
  const [logoIndex, setLogoIndex] = useState(0);
  const bg = preset.brand_color || "hsl(var(--muted))";
  const logoUrls = useMemo(() => logoFallbackUrls(preset.logo_url), [preset.logo_url]);
  const logoUrl = logoUrls[logoIndex];
  const initials = preset.display_name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || preset.name.slice(0, 2).toUpperCase();

  useEffect(() => setLogoIndex(0), [preset.logo_url]);

  if (showBrandLogos && logoUrl) {
    return (
      <span
        className="grid h-11 w-11 shrink-0 place-items-center rounded-[8px] border border-border/45 bg-background"
        style={{ boxShadow: `inset 0 0 0 1px ${preset.brand_color ?? "transparent"}22` }}
      >
        <img
          src={logoUrl}
          alt=""
          className="h-6 w-6 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      </span>
    );
  }
  return (
    <span
      className="grid h-11 w-11 shrink-0 place-items-center rounded-[8px] text-[13px] font-semibold text-white"
      style={{ backgroundColor: bg }}
    >
      {initials}
    </span>
  );
}

export function CliAppReadyPanel({
  app,
  showBrandLogos,
  onBackToChat,
}: {
  app: CliAppInfo;
  showBrandLogos: boolean;
  onBackToChat: () => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const prompt = t("settings.cliApps.readyPrompt", {
    name: app.name,
    defaultValue: "Use @{{name}} to inspect what this CLI can do.",
  });
  const copyPrompt = () => {
    if (!navigator.clipboard) return;
    void navigator.clipboard.writeText(prompt).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    });
  };

  return (
    <section
      className={cn(
        "rounded-[12px] border border-border/55 bg-card/88 px-4 py-3",
        "shadow-[0_8px_26px_rgba(15,23,42,0.055)]",
      )}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <CliAppLogo app={app} showBrandLogos={showBrandLogos} />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h3 className="truncate text-[14px] font-semibold leading-5 text-foreground">
              {app.display_name}
            </h3>
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[10.5px] font-medium text-muted-foreground">
              <Check className="h-3 w-3 text-emerald-600 dark:text-emerald-300" aria-hidden />
              {t("settings.cliApps.readyStatus", { defaultValue: "Ready" })}
            </span>
          </div>
          <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5 text-[12px] text-muted-foreground">
            <span className="font-mono">@{app.name}</span>
            <span aria-hidden>·</span>
            <span className="truncate font-mono">{app.entry_point || app.name}</span>
            <span aria-hidden>·</span>
            <span>{app.category}</span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={copyPrompt}
            className="h-8 rounded-full px-3 text-[12px] font-medium text-muted-foreground hover:bg-muted/65 hover:text-foreground"
          >
            {copied ? <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden /> : null}
            {copied
              ? t("settings.cliApps.readyCopied", { defaultValue: "Copied" })
              : t("settings.cliApps.readyTry", { name: app.name, defaultValue: "Try @{{name}}" })}
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={onBackToChat}
            className="h-8 rounded-full px-3 text-[12px] font-semibold"
          >
            {t("settings.cliApps.openChat", { defaultValue: "Open chat" })}
            <ChevronRight className="ml-1.5 h-3.5 w-3.5" aria-hidden />
          </Button>
        </div>
      </div>
    </section>
  );
}

export function CliAppLogo({ app, showBrandLogos }: { app: CliAppInfo; showBrandLogos: boolean }) {
  const [logoIndex, setLogoIndex] = useState(0);
  const bg = app.brand_color || "hsl(var(--muted))";
  const logoUrls = useMemo(() => logoFallbackUrls(app.logo_url), [app.logo_url]);
  const logoUrl = logoUrls[logoIndex];
  const initials = app.display_name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || app.name.slice(0, 2).toUpperCase();

  useEffect(() => setLogoIndex(0), [app.logo_url]);

  if (showBrandLogos && logoUrl) {
    return (
      <span
        className="grid h-11 w-11 shrink-0 place-items-center rounded-[8px] border border-border/45 bg-background"
        style={{ boxShadow: `inset 0 0 0 1px ${app.brand_color ?? "transparent"}22` }}
      >
        <img
          src={logoUrl}
          alt=""
          className="h-6 w-6 object-contain"
          onError={() => setLogoIndex((index) => index + 1)}
        />
      </span>
    );
  }
  return (
    <span
      className="grid h-11 w-11 shrink-0 place-items-center rounded-[8px] text-[13px] font-semibold text-white"
      style={{ backgroundColor: bg }}
    >
      {initials}
    </span>
  );
}

