import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { deleteAgentProfile, fetchAgentProfiles, saveAgentProfile } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { SettingsPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

export function AgentManagementSettings({ settings }: { settings: SettingsPayload | null }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [agents, setAgents] = useState(settings?.agent_profiles ?? []);
  const [loading, setLoading] = useState(!settings?.agent_profiles);
  const [error, setError] = useState<string | null>(null);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [confirmDeleteName, setConfirmDeleteName] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [draft, setDraft] = useState<{ name: string; icon: string; requirements: string }>({
    name: "",
    icon: "\u{1F4A1}",
    requirements: "",
  });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (settings?.agent_profiles) {
      setAgents(settings.agent_profiles);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetchAgentProfiles(token)
      .then((payload) => {
        if (!cancelled) setAgents(payload.agents);
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
  }, [settings?.agent_profiles, token]);

  const startEdit = (name: string | null) => {
    setConfirmDeleteName(null);
    setHelpOpen(false);
    if (name === null) {
      setDraft({ name: "", icon: "\u{1F4A1}", requirements: "" });
      setEditingName("__new__");
      return;
    }
    const agent = agents.find((a) => a.name === name);
    if (!agent) return;
    setDraft({ name: agent.name, icon: agent.icon, requirements: agent.description });
    setEditingName(name);
  };

  const cancelEdit = () => {
    setEditingName(null);
    setHelpOpen(false);
  };

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const payload = await saveAgentProfile(token, {
        originalName: editingName && editingName !== "__new__" ? editingName : undefined,
        name: draft.name,
        icon: draft.icon,
        requirements: draft.requirements,
      });
      setAgents(payload.agents);
      setEditingName(null);
      setHelpOpen(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (name: string) => {
    setError(null);
    try {
      const payload = await deleteAgentProfile(token, name);
      setAgents(payload.agents);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setConfirmDeleteName(null);
    }
  };

  const EMOJI_CHOICES = ["\u{1F4D8}", "\u{1F4C5}", "\u{1F9ED}", "\u{1F4A1}", "\u{1F3AF}", "\u{1F9E0}", "\u{270D}\u{FE0F}", "\u{1F52C}", "\u{1F3A8}", "\u{1F331}"];

  const HELP_EXAMPLE = t("settings.agentManagement.helpExample", {
    defaultValue:
      "시험 공부나 문제 풀이를 도와달라고 하면 선생님처럼 차근차근 설명해줘.\n복습 계획도 같이 세워주고, 어려운 부분은 예시를 들어서 알려줘.\n나노봇 자체 설정 이야기가 나오면 이 에이전트 말고 메인이 처리하게 해줘.",
  });

  return (
    <div className="space-y-4">
      <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
        {t("settings.agentManagement.intro", {
          defaultValue:
            "Give nanobot a dedicated helper for one job. Just a name and what you need — nanobot decides when to hand a task to it while you chat with the main agent.",
        })}
      </p>
      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}
      {loading ? (
        <p className="text-[13px] text-muted-foreground">{t("common.loading", { defaultValue: "Loading…" })}</p>
      ) : (
        <div className="space-y-2">
          {agents.map((agent) => {
            if (confirmDeleteName === agent.name) {
              return (
                <div
                  key={agent.name}
                  className="flex items-center gap-3 rounded-[12px] border border-amber-300/70 bg-amber-50/70 px-4 py-3 dark:border-amber-500/40 dark:bg-amber-500/10"
                >
                  <span className="text-[18px]">{agent.icon}</span>
                  <div className="min-w-0 flex-1">
                    <p className="text-[13.5px] font-medium text-foreground">
                      {t("settings.agentManagement.confirmDeleteTitle", {
                        defaultValue: `Delete "${agent.name}"?`,
                        name: agent.name,
                      })}
                    </p>
                    <p className="text-[12px] text-muted-foreground">
                      {t("settings.agentManagement.confirmDeleteDesc", {
                        defaultValue: "This can't be undone.",
                      })}
                    </p>
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => setConfirmDeleteName(null)}>
                    {t("common.cancel", { defaultValue: "Cancel" })}
                  </Button>
                  <Button variant="destructive" size="sm" onClick={() => remove(agent.name)}>
                    {t("settings.agentManagement.confirmDeleteButton", { defaultValue: "Delete" })}
                  </Button>
                </div>
              );
            }
            if (editingName === agent.name) {
              return (
                <AgentEditCard
                  key={agent.name}
                  draft={draft}
                  setDraft={setDraft}
                  helpOpen={helpOpen}
                  setHelpOpen={setHelpOpen}
                  helpExample={HELP_EXAMPLE}
                  emojiChoices={EMOJI_CHOICES}
                  saving={saving}
                  onCancel={cancelEdit}
                  onSave={save}
                  t={t}
                />
              );
            }
            return (
              <div
                key={agent.name}
                className="flex items-center gap-3 rounded-[12px] border border-border/60 bg-card/60 px-4 py-3"
              >
                <span className="text-[18px]">{agent.icon}</span>
                <div className="min-w-0 flex-1">
                  <p className="text-[13.5px] font-medium text-foreground">{agent.name}</p>
                  <p className="truncate text-[12px] text-muted-foreground">{agent.description}</p>
                </div>
                <Button variant="ghost" size="sm" onClick={() => startEdit(agent.name)}>
                  {t("common.edit", { defaultValue: "Edit" })}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setConfirmDeleteName(agent.name)}>
                  {t("common.delete", { defaultValue: "Delete" })}
                </Button>
              </div>
            );
          })}
          {editingName === "__new__" ? (
            <AgentEditCard
              draft={draft}
              setDraft={setDraft}
              helpOpen={helpOpen}
              setHelpOpen={setHelpOpen}
              helpExample={HELP_EXAMPLE}
              emojiChoices={EMOJI_CHOICES}
              saving={saving}
              onCancel={cancelEdit}
              onSave={save}
              t={t}
            />
          ) : (
            <button
              type="button"
              onClick={() => startEdit(null)}
              className="w-full rounded-[12px] border border-dashed border-border/70 px-4 py-3 text-[13px] font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
            >
              {t("settings.agentManagement.addAgent", { defaultValue: "+ Add a new agent" })}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function AgentEditCard({
  draft,
  setDraft,
  helpOpen,
  setHelpOpen,
  helpExample,
  emojiChoices,
  saving,
  onCancel,
  onSave,
  t,
}: {
  draft: { name: string; icon: string; requirements: string };
  setDraft: (updater: (prev: { name: string; icon: string; requirements: string }) => { name: string; icon: string; requirements: string }) => void;
  helpOpen: boolean;
  setHelpOpen: (open: boolean) => void;
  helpExample: string;
  emojiChoices: string[];
  saving: boolean;
  onCancel: () => void;
  onSave: () => void;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  return (
    <div className="space-y-3 rounded-[12px] border border-primary/40 bg-card px-4 py-4">
      <div>
        <label className="mb-1 block text-[12.5px] font-medium text-foreground">
          {t("settings.agentManagement.nameLabel", { defaultValue: "Name" })}
        </label>
        <input
          type="text"
          value={draft.name}
          onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))}
          placeholder={t("settings.agentManagement.namePlaceholder", { defaultValue: "e.g. Presentation coach" })}
          className="w-full rounded-[8px] border border-border/70 bg-background px-3 py-2 text-[13px] text-foreground outline-none focus:border-primary"
        />
      </div>
      <div>
        <label className="mb-1 block text-[12.5px] font-medium text-foreground">
          {t("settings.agentManagement.iconLabel", { defaultValue: "Icon" })}
        </label>
        <div className="flex flex-wrap gap-1.5">
          {emojiChoices.map((emoji) => (
            <button
              key={emoji}
              type="button"
              aria-pressed={draft.icon === emoji}
              onClick={() => setDraft((prev) => ({ ...prev, icon: emoji }))}
              className={cn(
                "flex h-8 w-8 items-center justify-center rounded-[8px] border text-[15px]",
                draft.icon === emoji ? "border-primary bg-primary/10" : "border-border/70 bg-background",
              )}
            >
              {emoji}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className="text-[12.5px] font-medium text-foreground">
            {t("settings.agentManagement.requirementsLabel", { defaultValue: "Requirements" })}
          </label>
          <button
            type="button"
            onClick={() => setHelpOpen(!helpOpen)}
            className="text-[11px] font-medium text-primary underline-offset-2 hover:underline"
          >
            {t("settings.agentManagement.helpToggle", { defaultValue: "❓ Not sure what to write" })}
          </button>
        </div>
        {helpOpen ? (
          <div className="mb-2 space-y-2 rounded-[10px] border border-primary/30 bg-primary/5 px-3 py-3 text-[12px] text-muted-foreground">
            <p>
              {t("settings.agentManagement.helpIntro", {
                defaultValue: "Three things are enough: when to call it, what tone to use, and (optionally) what to avoid.",
              })}
            </p>
            <pre className="whitespace-pre-wrap rounded-[8px] border border-border/60 bg-background px-3 py-2 text-[12px] text-foreground">
              {helpExample}
            </pre>
            <button
              type="button"
              onClick={() => setDraft((prev) => ({ ...prev, requirements: helpExample }))}
              className="text-[11.5px] font-medium text-primary hover:underline"
            >
              {t("settings.agentManagement.useExample", { defaultValue: "Use this example" })}
            </button>
          </div>
        ) : null}
        <textarea
          value={draft.requirements}
          onChange={(e) => setDraft((prev) => ({ ...prev, requirements: e.target.value }))}
          placeholder={t("settings.agentManagement.requirementsPlaceholder", {
            defaultValue: "e.g. Keep presentation scripts short and confident.",
          })}
          className="min-h-[190px] max-h-[420px] w-full resize-y overflow-y-auto rounded-[8px] border border-border/70 bg-background px-3 py-2 text-[13px] leading-relaxed text-foreground outline-none focus:border-primary"
        />
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          {t("common.cancel", { defaultValue: "Cancel" })}
        </Button>
        <Button size="sm" disabled={saving || !draft.name.trim() || !draft.requirements.trim()} onClick={onSave}>
          {t("common.save", { defaultValue: "Save" })}
        </Button>
      </div>
    </div>
  );
}


