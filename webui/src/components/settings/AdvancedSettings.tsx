import { type Dispatch, type SetStateAction } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type {
  NetworkSafetySettingsUpdate,
  SkillGovernanceLevel,
  SkillGovernanceSettingsUpdate,
  StudentModeSettingsUpdate,
  WebuiDefaultAccessMode,
} from "@/lib/types";

import {
  RestartSettingsFooter,
  SegmentedControl,
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
  ToggleButton,
} from "@/components/settings/settings-primitives";

export function SkillGovernanceQuickPanel({
  form,
  dirty,
  saving,
  onChange,
  onSave,
}: {
  form: Required<SkillGovernanceSettingsUpdate>;
  dirty: boolean;
  saving: boolean;
  onChange: Dispatch<SetStateAction<Required<SkillGovernanceSettingsUpdate>>>;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const domainText = form.allowedInstallDomains.join(", ");
  return (
    <section className="space-y-3">
      <SettingsSectionTitle>{tx("settings.easySetup.governance", "Skill governance")}</SettingsSectionTitle>
      <SettingsGroup>
        <SettingsRow title="webui_skill_management.enabled" description={tx("settings.easySetup.skillApprovalHelp", "Review and approve skill drafts inside WebUI.")}>
          <ToggleButton checked={form.webuiSkillManagementEnabled} label={form.webuiSkillManagementEnabled ? "On" : "Off"} onChange={(webuiSkillManagementEnabled) => onChange((prev) => ({ ...prev, webuiSkillManagementEnabled }))} />
        </SettingsRow>
        <SettingsRow title="security_block_at_least" description={tx("settings.easySetup.securityBlockHelp", "Schema value is low / medium / high, not a numeric slider.")}>
          <SegmentedControl value={form.securityBlockAtLeast} options={[{ value: "low", label: "낮음" }, { value: "medium", label: "중간" }, { value: "high", label: "높음" }]} onChange={(securityBlockAtLeast) => onChange((prev) => ({ ...prev, securityBlockAtLeast: securityBlockAtLeast as SkillGovernanceLevel }))} />
        </SettingsRow>
        <SettingsRow title="security_risk_at_least" description={tx("settings.easySetup.securityRiskHelp", "Flag drafts at this risk level or above.")}>
          <SegmentedControl value={form.securityRiskAtLeast} options={[{ value: "low", label: "낮음" }, { value: "medium", label: "중간" }, { value: "high", label: "높음" }]} onChange={(securityRiskAtLeast) => onChange((prev) => ({ ...prev, securityRiskAtLeast: securityRiskAtLeast as SkillGovernanceLevel }))} />
        </SettingsRow>
        <SettingsRow title="draft_expire_days" description="1–365">
          <Input type="number" min={1} max={365} value={form.draftExpireDays} onChange={(event) => onChange((prev) => ({ ...prev, draftExpireDays: Number(event.target.value) }))} className="h-8 w-24 rounded-full text-right" />
        </SettingsRow>
        <SettingsRow title="duplicate_score_at_least" description="0.0–1.0, default 0.8">
          <Input type="number" min={0} max={1} step={0.01} value={form.duplicateScoreAtLeast} onChange={(event) => onChange((prev) => ({ ...prev, duplicateScoreAtLeast: Number(event.target.value) }))} className="h-8 w-24 rounded-full text-right" />
        </SettingsRow>
        <SettingsRow title="external_tool_skills.allowed_install_domains" description={domainText}>
          <Input value={domainText} onChange={(event) => onChange((prev) => ({ ...prev, allowedInstallDomains: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) }))} className="h-8 max-w-[340px] rounded-full text-[12px]" />
        </SettingsRow>
        <SettingsRow title="external_tool_skills.install_root" description="Relative workspace path, default tools">
          <Input value={form.installRoot} onChange={(event) => onChange((prev) => ({ ...prev, installRoot: event.target.value }))} className="h-8 w-36 rounded-full text-[12px]" />
        </SettingsRow>
        <SettingsRow title="external_tool_skills.deny_global_install">
          <ToggleButton checked={form.denyGlobalInstall} label={form.denyGlobalInstall ? "On" : "Off"} onChange={(denyGlobalInstall) => onChange((prev) => ({ ...prev, denyGlobalInstall }))} />
        </SettingsRow>
        <SettingsRow title={tx("settings.actions.save", "Save")}>
          <Button size="sm" className="rounded-full" onClick={onSave} disabled={!dirty || saving}>{saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}{tx("settings.actions.save", "Save")}</Button>
        </SettingsRow>
      </SettingsGroup>
    </section>
  );
}

export function StudentModeQuickPanel({
  form,
  dirty,
  saving,
  onChange,
  onSave,
}: {
  form: Required<StudentModeSettingsUpdate>;
  dirty: boolean;
  saving: boolean;
  onChange: Dispatch<SetStateAction<Required<StudentModeSettingsUpdate>>>;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <section className="space-y-3">
      <SettingsSectionTitle>{tx("settings.easySetup.studentMode", "Student mode")}</SettingsSectionTitle>
      <SettingsGroup>
        <SettingsRow title="student_mode.mode" description="general | student">
          <SegmentedControl value={form.mode} options={[{ value: "general", label: "일반" }, { value: "student", label: "학생" }]} onChange={(mode) => onChange((prev) => ({ ...prev, mode: mode as "general" | "student" }))} />
        </SettingsRow>
        <SettingsRow title="coach_name" description="Default: 담임 선생님">
          <Input value={form.coachName} onChange={(event) => onChange((prev) => ({ ...prev, coachName: event.target.value }))} className="h-8 w-44 rounded-full" />
        </SettingsRow>
        <SettingsRow title="review_teacher_name" description="Default: AGENT_A 선생님">
          <Input value={form.reviewTeacherName} onChange={(event) => onChange((prev) => ({ ...prev, reviewTeacherName: event.target.value }))} className="h-8 w-44 rounded-full" />
        </SettingsRow>
        <SettingsRow title="study_log_path" description="Workspace-relative path">
          <Input value={form.studyLogPath} onChange={(event) => onChange((prev) => ({ ...prev, studyLogPath: event.target.value }))} className="h-8 w-48 rounded-full font-mono text-[12px]" />
        </SettingsRow>
        <SettingsRow title="review_queue_path" description="Workspace-relative path">
          <Input value={form.reviewQueuePath} onChange={(event) => onChange((prev) => ({ ...prev, reviewQueuePath: event.target.value }))} className="h-8 w-48 rounded-full font-mono text-[12px]" />
        </SettingsRow>
        <SettingsRow title="daily_review_cron_name">
          <Input value={form.dailyReviewCronName} onChange={(event) => onChange((prev) => ({ ...prev, dailyReviewCronName: event.target.value }))} className="h-8 w-44 rounded-full font-mono text-[12px]" />
        </SettingsRow>
        <SettingsRow title={tx("settings.actions.save", "Save")}>
          <Button size="sm" className="rounded-full" onClick={onSave} disabled={!dirty || saving}>{saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}{tx("settings.actions.save", "Save")}</Button>
        </SettingsRow>
      </SettingsGroup>
    </section>
  );
}


export function AdvancedSettings({
  form,
  dirty,
  saving,
  requiresRestartPending,
  isNativeHostSurface,
  onChangeForm,
  onSave,
  onRestart,
  isRestarting,
}: {
  form: NetworkSafetySettingsUpdate;
  dirty: boolean;
  saving: boolean;
  requiresRestartPending: boolean;
  isNativeHostSurface: boolean;
  onChangeForm: Dispatch<SetStateAction<NetworkSafetySettingsUpdate>>;
  onSave: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <div className="space-y-7">
      <section>
        <SettingsSectionTitle>
          {isNativeHostSurface
            ? tx("settings.sections.hostSafety", "App safety")
            : tx("settings.sections.webuiSafety", "Web safety")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.rows.localServiceAccess", "Local Service Access")}
            description={tx(
              isNativeHostSurface ? "settings.help.localServiceAccessNative" : "settings.help.localServiceAccess",
              isNativeHostSurface
                ? "Allow Full Access shell commands to reach services on this Mac."
                : "Allow Full Access shell commands to reach localhost services.",
            )}
          >
            <ToggleButton
              checked={form.webuiAllowLocalServiceAccess}
              onChange={(webuiAllowLocalServiceAccess) =>
                onChangeForm((prev) => ({ ...prev, webuiAllowLocalServiceAccess }))
              }
              ariaLabel={tx("settings.rows.localServiceAccess", "Local Service Access")}
              label={form.webuiAllowLocalServiceAccess ? tx("settings.values.on", "On") : tx("settings.values.off", "Off")}
            />
          </SettingsRow>
          <SettingsRow
            title={tx("settings.rows.webuiDefaultAccess", "Default access")}
            description={tx(
              isNativeHostSurface ? "settings.help.webuiDefaultAccessNative" : "settings.help.webuiDefaultAccess",
              isNativeHostSurface
                ? "Used by native chats without a project-specific permission."
                : "Used by web chats without a project-specific permission.",
            )}
          >
            <SegmentedControl
              value={form.webuiDefaultAccessMode}
              options={[
                { value: "default", label: tx("settings.values.defaultPermission", "Default Permission") },
                { value: "full", label: tx("settings.values.fullAccess", "Full Access") },
              ]}
              onChange={(webuiDefaultAccessMode) =>
                onChangeForm((prev) => ({
                  ...prev,
                  webuiDefaultAccessMode: webuiDefaultAccessMode as WebuiDefaultAccessMode,
                }))
              }
            />
          </SettingsRow>
          <RestartSettingsFooter
            dirty={dirty}
            saving={saving}
            pendingRestart={requiresRestartPending}
            onSave={onSave}
            onRestart={onRestart}
            isRestarting={isRestarting}
          />
        </SettingsGroup>
      </section>

      <p className="max-w-3xl px-1 text-sm leading-6 text-muted-foreground">
        {tx(
          "settings.help.securityManagedControls",
          "Web fetches always protect local, private, and metadata services. Core channel safety stays in config.json.",
        )}
      </p>
    </div>
  );
}

