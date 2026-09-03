import { useState } from "react";
import { useTranslation } from "react-i18next";

import { updateAgentToolsSettings } from "@/lib/api";
import type { AgentToolsSettingsUpdate, SettingsPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { ToggleButton } from "@/components/settings/settings-primitives";

export function AgentToolsSettings({
  settings,
  onSettingsChange,
}: {
  settings: SettingsPayload | null;
  onSettingsChange?: (payload: SettingsPayload) => void;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const agentTools = settings?.agent_tools;

  const rows: Array<{
    key: keyof AgentToolsSettingsUpdate;
    queryKey: "webEnabled" | "fileEnabled" | "execEnabled" | "cliAppsEnabled" | "imageGenerationEnabled";
    checked: boolean;
    titleKey: string;
    fallbackTitle: string;
    descKey: string;
    fallbackDesc: string;
    locked?: boolean;
  }> = [
    {
      key: "webEnabled",
      queryKey: "webEnabled",
      checked: agentTools?.web_enabled ?? true,
      titleKey: "settings.agentTools.web.title",
      fallbackTitle: "Web search",
      descKey: "settings.agentTools.web.desc",
      fallbackDesc: "Look things up and answer with sources.",
    },
    {
      key: "fileEnabled",
      queryKey: "fileEnabled",
      checked: agentTools?.file_enabled ?? true,
      titleKey: "settings.agentTools.file.title",
      fallbackTitle: "Files",
      descKey: "settings.agentTools.file.desc",
      fallbackDesc: "Read and write files in the workspace.",
    },
    {
      key: "imageGenerationEnabled",
      queryKey: "imageGenerationEnabled",
      checked: agentTools?.image_generation_enabled ?? false,
      titleKey: "settings.agentTools.image.title",
      fallbackTitle: "Image generation",
      descKey: "settings.agentTools.image.desc",
      fallbackDesc: "Turn a description into a picture.",
    },
    {
      key: "cliAppsEnabled",
      queryKey: "cliAppsEnabled",
      checked: agentTools?.cli_apps_enabled ?? false,
      titleKey: "settings.agentTools.cliApps.title",
      fallbackTitle: "Run connected programs",
      descKey: "settings.agentTools.cliApps.desc",
      fallbackDesc: "Actually run the programs connected under Apps.",
    },
    {
      key: "execEnabled",
      queryKey: "execEnabled",
      checked: agentTools?.exec_enabled ?? false,
      titleKey: "settings.agentTools.exec.title",
      fallbackTitle: "Run commands",
      descKey: "settings.agentTools.exec.desc",
      fallbackDesc: "Run commands directly on this computer.",
      locked: true,
    },
  ];

  const handleToggle = async (row: (typeof rows)[number]) => {
    if (pendingKey) return;
    setPendingKey(row.key);
    setError(null);
    try {
      const payload = await updateAgentToolsSettings(token, {
        [row.queryKey]: !row.checked,
      } as AgentToolsSettingsUpdate);
      onSettingsChange?.(payload);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPendingKey(null);
    }
  };

  return (
    <div className="space-y-4">
      <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
        {t("settings.agentTools.intro", {
          defaultValue:
            "Agent Tools are built-in abilities nanobot can use directly. Programs you connect from Apps are separate — turn this on to let nanobot actually run them.",
        })}
      </p>
      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}
      <div className="divide-y divide-border/60 overflow-hidden rounded-[12px] border border-border/60 bg-card/60">
        {rows.map((row) => (
          <div key={row.key} className="flex items-center gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-[13.5px] font-medium text-foreground">
                {t(row.titleKey, { defaultValue: row.fallbackTitle })}
                {row.locked ? (
                  <span className="rounded-full border border-amber-300/60 bg-amber-50 px-2 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
                    {t("settings.agentTools.needsApproval", { defaultValue: "Needs approval" })}
                  </span>
                ) : null}
              </div>
              <p className="text-[12px] text-muted-foreground">
                {t(row.descKey, { defaultValue: row.fallbackDesc })}
              </p>
            </div>
            <ToggleButton
              checked={row.checked}
              onChange={() => handleToggle(row)}
              ariaLabel={t(row.titleKey, { defaultValue: row.fallbackTitle })}
              label={row.checked ? t("common.on", { defaultValue: "On" }) : t("common.off", { defaultValue: "Off" })}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

