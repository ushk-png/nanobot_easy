import { ChevronLeft, LogOut } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { ToggleButton } from "@/components/settings/settings-primitives";
import { SETTINGS_NAV_ITEMS, type SettingsSectionKey } from "@/components/settings/SettingsView";

export function SettingsSidebar({
  activeSection,
  onSelectSection,
  onBackToChat,
  onLogout,
  hostChromeInset,
  showAdvanced,
  onToggleAdvanced,
}: {
  activeSection: SettingsSectionKey;
  onSelectSection: (section: SettingsSectionKey) => void;
  onBackToChat: () => void;
  onLogout?: () => void;
  hostChromeInset?: boolean;
  showAdvanced: boolean;
  onToggleAdvanced: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  // The active section can be an advanced one reached via direct link (e.g.
  // the onboarding wizard's "open skills" link) even when the toggle is off
  // -- keep it visible in that case instead of silently dropping the
  // highlighted/selected item from the list.
  const visibleNavItems = SETTINGS_NAV_ITEMS.filter(
    (item) => item.tier === "basic" || showAdvanced || item.key === activeSection,
  );
  return (
    <aside
      className={cn(
        "flex w-full shrink-0 flex-col border-b border-border/55 bg-card/62 px-3 pb-2 shadow-[inset_0_-1px_0_rgba(255,255,255,0.55)] backdrop-blur-xl dark:bg-card/45 dark:shadow-none md:w-[17rem] md:border-b-0 md:border-r md:px-3 md:pb-4 md:shadow-[inset_-1px_0_0_rgba(255,255,255,0.55)]",
        hostChromeInset ? "pt-[4.25rem] md:pt-[4.25rem]" : "pt-4 md:pt-4",
      )}
    >
      <button
        type="button"
        onClick={onBackToChat}
        className="mb-2 inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground md:mb-3"
      >
        <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
        {t("settings.backToChat")}
      </button>
      <div className="mb-3 px-1 md:mb-4 md:px-2">
        <h2 className="text-[18px] font-normal tracking-normal text-foreground">
          {t("settings.sidebar.title")}
        </h2>
      </div>

      <nav
        aria-label={t("settings.sidebar.ariaLabel")}
        className="-mx-1 flex gap-2 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:mx-0 md:block md:space-y-1 md:overflow-visible md:px-0 md:pb-0"
      >
        {visibleNavItems.map(({ key, icon: Icon, fallback }) => {
          const active = key === activeSection;
          return (
            <button
              key={key}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => onSelectSection(key)}
              className={cn(
                "flex h-9 w-auto shrink-0 items-center gap-2 rounded-full px-3 text-left text-[13px] font-medium transition-colors md:w-full md:rounded-[10px] md:px-2.5",
                active
                  ? "bg-muted/90 text-foreground shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)]"
                  : "text-muted-foreground/78 hover:bg-muted/45 hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" strokeWidth={2} aria-hidden />
              <span className="truncate">{t(`settings.nav.${key}`, { defaultValue: fallback })}</span>
            </button>
          );
        })}
      </nav>

      <div className="mt-2 flex items-center justify-between gap-2 px-1.5 md:mt-3 md:px-2">
        <span className="truncate text-[12.5px] font-medium text-muted-foreground/78">
          {tx("settings.sidebar.showAdvanced", "Show advanced settings")}
        </span>
        <ToggleButton
          checked={showAdvanced}
          onChange={onToggleAdvanced}
          ariaLabel={tx("settings.sidebar.showAdvanced", "Show advanced settings")}
          label={showAdvanced ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
        />
      </div>

      <div className="hidden md:mt-auto md:block md:pt-4">
        {onLogout && !hostChromeInset ? (
          <Button
            type="button"
            variant="ghost"
            onClick={onLogout}
            className="h-9 w-full justify-start gap-2 rounded-[10px] px-2.5 text-[13px] font-medium text-muted-foreground hover:bg-destructive/8 hover:text-destructive"
          >
            <LogOut className="h-4 w-4" aria-hidden />
            {t("app.account.logout")}
          </Button>
        ) : null}
      </div>
    </aside>
  );
}

