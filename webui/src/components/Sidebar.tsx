import { useState, type ReactNode } from "react";
import {
  Archive,
  Bot,
  Brain,
  CalendarClock,
  Menu,
  Search,
  Settings,
  SquarePen,
  Blocks,
  Wrench,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import type {
  ChatSummary,
  SidebarViewState,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  onTogglePin: (key: string) => void;
  onRequestRename: (key: string, label: string) => void;
  onToggleArchive: (key: string) => void;
  onToggleGroup: (groupId: string) => void;
  onRequestRenameProject: (projectKey: string, label: string) => void;
  onNewChatInProject: (projectPath: string, projectName: string) => void;
  onOpenSettings: () => void;
  onOpenApps: () => void;
  onOpenSkills: () => void;
  onOpenTools: () => void;
  onOpenAutomations: () => void;
  onOpenSearch: () => void;
  onOpenAgentManagement: () => void;
  activeUtility?: "apps" | "skills" | "tools" | "automations" | "agent-management" | null;
  onToggleArchived: () => void;
  onCollapse: () => void;
  onExpand?: () => void;
  containActionMenus?: boolean;
  collapsed?: boolean;
  pinnedKeys?: string[];
  archivedKeys?: string[];
  titleOverrides?: Record<string, string>;
  projectNameOverrides?: Record<string, string>;
  collapsedGroups?: Record<string, boolean>;
  runningChatIds?: string[];
  updatedChatIds?: string[];
  viewState?: SidebarViewState;
  showArchived?: boolean;
  archivedCount?: number;
  defaultWorkspacePath?: string | null;
  hostChromeInset?: boolean;
}

type NavigatorWithUserAgentData = Navigator & {
  userAgentData?: { platform?: string };
};

function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform = navigator.platform || "";
  const userAgentPlatform =
    (navigator as NavigatorWithUserAgentData).userAgentData?.platform || "";
  return /mac|iphone|ipad|ipod/i.test(`${platform} ${userAgentPlatform}`);
}

function newChatShortcutLabel(): string {
  return isApplePlatform() ? "⌘⇧O" : "Ctrl+Shift+O";
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  const [menuPortalContainer, setMenuPortalContainer] =
    useState<HTMLElement | null>(null);
  const collapsed = Boolean(props.collapsed);
  const toggleLabel = t("thread.header.toggleSidebar");
  const newChatShortcut = newChatShortcutLabel();

  return (
    <nav
      ref={props.containActionMenus ? setMenuPortalContainer : undefined}
      aria-label={t("sidebar.navigation")}
      className={cn(
        "flex h-full w-full min-w-0 flex-col text-sidebar-foreground",
        props.hostChromeInset ? "bg-transparent" : "bg-panel",
        !props.hostChromeInset && "border-r border-border",
      )}
    >
      <div
        className={cn(
          "side-brand flex items-center gap-2 px-4 pb-2.5",
          props.hostChromeInset ? "pt-[2.85rem]" : "pt-4",
          collapsed ? "w-14 justify-start" : "justify-between",
        )}
      >
        <button
          type="button"
          aria-label={collapsed ? toggleLabel : undefined}
          aria-hidden={collapsed ? undefined : true}
          title={collapsed ? toggleLabel : undefined}
          onClick={collapsed ? props.onExpand : undefined}
          tabIndex={collapsed ? 0 : -1}
          className={cn(
            "flex h-[26px] w-[26px] shrink-0 items-center justify-center overflow-hidden rounded-lg bg-primary transition-colors",
            collapsed
              ? "-ml-0.5 hover:bg-sidebar-accent/75"
              : "pointer-events-none -ml-0.5",
          )}
        >
          <img
            src="/brand/nanobot_icon.png"
            alt=""
            className="h-6 w-6 select-none object-contain"
            draggable={false}
          />
        </button>
        {!collapsed && (
          <span className="min-w-0 flex-1 truncate text-[14.5px] font-bold tracking-[-0.01em] text-foreground">
            nanobot-easy
          </span>
        )}
        {!collapsed && !props.hostChromeInset && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.collapse")}
            onClick={props.onCollapse}
            className="h-7 w-7 rounded-lg text-muted-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
          >
            <Menu className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>

      <div
        className={cn(
          "space-y-0.5 px-3 pb-2",
          collapsed && "flex w-14 flex-col items-center px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.newChat")}
          onClick={props.onNewChat}
          icon={<SquarePen className="h-4 w-4" />}
          shortcut={newChatShortcut}
          ariaKeyShortcuts="Meta+Shift+O Control+Shift+O"
          emphasize
        />
        {collapsed ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={t("sidebar.searchAria")}
            onClick={props.onOpenSearch}
            icon={<Search className="h-4 w-4" />}
          />
        ) : (
          <button
            type="button"
            onClick={props.onOpenSearch}
            aria-label={t("sidebar.searchAria")}
            className="side-search my-2 flex min-h-9 w-full items-center gap-2 rounded-[7px] border border-border-strong bg-background px-2.5 text-[12.5px] text-muted-foreground transition-colors hover:border-primary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <Search className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="truncate">{t("sidebar.searchPlaceholder")}</span>
          </button>
        )}
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.apps")}
          onClick={props.onOpenApps}
          active={props.activeUtility === "apps"}
          icon={<Blocks className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.skills.title")}
          onClick={props.onOpenSkills}
          active={props.activeUtility === "skills"}
          icon={<Brain className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.tools", { defaultValue: "App Tools" })}
          onClick={props.onOpenTools}
          active={props.activeUtility === "tools"}
          icon={<Wrench className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.automations", { defaultValue: "Automations" })}
          onClick={props.onOpenAutomations}
          active={props.activeUtility === "automations"}
          icon={<CalendarClock className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.agentManagement", { defaultValue: "Agent management" })}
          active={props.activeUtility === "agent-management"}
          onClick={props.onOpenAgentManagement}
          icon={<Bot className="h-4 w-4" />}
        />
        {props.archivedCount ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={props.showArchived ? t("chat.hideArchived") : t("chat.showArchived")}
            onClick={props.onToggleArchived}
            icon={<Archive className="h-4 w-4" />}
          />
        ) : null}
      </div>
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden transition-opacity duration-200",
          collapsed && "pointer-events-none opacity-0",
        )}
      >
        {!collapsed && (
          <ChatList
            sessions={props.sessions}
            activeKey={props.activeKey}
            loading={props.loading}
            emptyLabel={t("chat.noSessions")}
            onSelect={props.onSelect}
            onRequestDelete={props.onRequestDelete}
            onTogglePin={props.onTogglePin}
            onRequestRename={props.onRequestRename}
            onToggleArchive={props.onToggleArchive}
            onToggleGroup={props.onToggleGroup}
            onRequestRenameProject={props.onRequestRenameProject}
            onNewChatInProject={props.onNewChatInProject}
            pinnedKeys={props.pinnedKeys}
            archivedKeys={props.archivedKeys}
            titleOverrides={props.titleOverrides}
            projectNameOverrides={props.projectNameOverrides}
            collapsedGroups={props.collapsedGroups}
            runningChatIds={props.runningChatIds}
            updatedChatIds={props.updatedChatIds}
            density={props.viewState?.density}
            showPreviews={props.viewState?.show_previews}
            showTimestamps={props.viewState?.show_timestamps}
            sort={props.viewState?.sort}
            showArchived={props.showArchived}
            defaultWorkspacePath={props.defaultWorkspacePath}
            actionMenuPortalContainer={
              props.containActionMenus ? menuPortalContainer : undefined
            }
          />
        )}
      </div>
      <Separator className="mx-3 w-auto bg-border" />
      <div
        className={cn(
          "side-bottom-row flex shrink-0 items-center gap-2 px-2.5 pb-2.5 pt-2 text-xs",
          collapsed && "w-14 flex-col px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.settings")}
          onClick={props.onOpenSettings}
          className={collapsed ? undefined : "flex-1"}
          icon={<Settings className="h-4 w-4" />}
        />
        <ConnectionBadge showLabel={!collapsed} variant="footer" />
      </div>
    </nav>
  );
}

function SidebarActionButton({
  collapsed,
  label,
  icon,
  onClick,
  active = false,
  emphasize = false,
  className,
  shortcut,
  ariaKeyShortcuts,
}: {
  collapsed: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  active?: boolean;
  emphasize?: boolean;
  className?: string;
  shortcut?: string;
  ariaKeyShortcuts?: string;
}) {
  const title = shortcut ? `${label} (${shortcut})` : collapsed ? label : undefined;

  return (
    <Button
      type="button"
      variant={emphasize ? "default" : "ghost"}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      aria-keyshortcuts={ariaKeyShortcuts}
      title={title}
      onClick={() => onClick()}
      className={cn(
        "side-item group h-auto min-h-9 min-w-0 gap-2 overflow-hidden rounded-[7px] py-2 font-normal",
        "transition-[width,padding,border-radius,color,background-color] duration-300 ease-out",
        emphasize
          ? "new-chat border border-primary bg-primary font-semibold text-primary-foreground shadow-sm hover:bg-primary/90"
          : "text-foreground-secondary hover:bg-background hover:text-foreground",
        collapsed
          ? "w-9 justify-center gap-0 rounded-xl px-0"
          : emphasize
            ? "w-full justify-center gap-[7px] px-3 text-[13px]"
            : "w-full justify-start gap-[9px] px-2.5 text-[13.5px]",
        active && "bg-accent font-semibold text-primary hover:bg-accent hover:text-primary",
        className,
      )}
    >
      <span
        className={cn(
          "flex shrink-0 items-center justify-center transition-transform duration-300 ease-out",
          collapsed ? "translate-x-0" : "translate-x-0",
        )}
        aria-hidden
      >
        {icon}
      </span>
      <span
        className={cn(
          "min-w-0 overflow-hidden truncate whitespace-nowrap transition-[max-width,opacity,transform] duration-200 ease-out",
          collapsed
            ? "max-w-0 -translate-x-1 opacity-0"
            : "max-w-[12rem] translate-x-0 opacity-100",
        )}
      >
        {label}
      </span>
    </Button>
  );
}
