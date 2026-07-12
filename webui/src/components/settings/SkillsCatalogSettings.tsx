import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import {
  Archive,
  Brain,
  Check,
  CircleAlert,
  Edit3,
  Eye,
  FileText,
  KeyRound,
  Loader2,
  PlayCircle,
  Plus,
  Search,
  Save,
  ShieldCheck,
  Terminal,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText } from "@/components/MarkdownText";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  approveManagedSkillDraft,
  composeManagedSkillDraft,
  createImportedManagedSkillDraft,
  fetchManagedSkillDetail,
  fetchManagedSkillDraft,
  fetchManagedSkills,
  fetchSkillDetail,
  importManagedSkillText,
  runManagedSkillRoutingTest,
  runSkillAudit,
  runManagedSkillStatusAction,
  updateManagedSkillMarkdown,
} from "@/lib/api";
import type {
  InstalledExternalTool,
  ManagedSkill,
  ManagedSkillDetail,
  ManagedSkillDraft,
  ManagedSkillDraftGovernanceFlag,
  ManagedSkillImportResult,
  ManagedSkillRoutingTestPayload,
  ManagedSkillStatus,
  ManagedSkillUpdateAssessment,
  SkillAuditReport,
  SkillDetail,
  SkillSummary,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const DRAFT_POLL_INTERVAL_MS = 1_000;
const DRAFT_POLL_ATTEMPTS = 90;

export function SkillsCatalogSettings({ skills }: { skills: SkillSummary[] }) {
  const { token } = useClient();
  const [managedPayload, setManagedPayload] = useState<{
    skills: ManagedSkill[];
    drafts: ManagedSkillDraft[];
    statusCounts: Partial<Record<ManagedSkillStatus, number>>;
  } | null>(null);
  const [manageUnavailable, setManageUnavailable] = useState(false);
  const [manageLoading, setManageLoading] = useState(true);

  const reloadManagedSkills = () => {
    setManageLoading(true);
    fetchManagedSkills(token)
      .then((payload) => {
        setManagedPayload({
          skills: payload.skills,
          drafts: payload.drafts ?? [],
          statusCounts: payload.status_counts,
        });
        setManageUnavailable(false);
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
          setManageUnavailable(true);
          return;
        }
        setManageUnavailable(true);
      })
      .finally(() => setManageLoading(false));
  };

  useEffect(() => {
    reloadManagedSkills();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  if (!manageUnavailable && managedPayload) {
    return (
      <ManagedSkillsSettings
        skills={managedPayload.skills}
        drafts={managedPayload.drafts}
        statusCounts={managedPayload.statusCounts}
        onReload={reloadManagedSkills}
      />
    );
  }

  if (manageLoading && !manageUnavailable) {
    return (
      <div className="flex min-h-[20rem] items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        Loading skill registry...
      </div>
    );
  }

  return <ReadOnlySkillsCatalog skills={skills} />;
}

function ReadOnlySkillsCatalog({ skills }: { skills: SkillSummary[] }) {
  const { t } = useTranslation();
  const availableCount = skills.filter((skill) => skill.available).length;
  const [selectedSkill, setSelectedSkill] = useState<SkillSummary | null>(null);

  return (
    <div className="space-y-7">
      <section className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
          {t("settings.skills.description", {
            defaultValue: "Review the instruction skills this agent can load during a conversation.",
          })}
        </p>
        <span className="text-[12px] font-medium text-muted-foreground">
          {t("settings.skills.caption", {
            available: availableCount,
            total: skills.length,
            defaultValue: "{{available}} available · {{total}} total",
          })}
        </span>
      </section>

      <section>
        <div className="flex items-center justify-between border-b border-border/45 pb-3">
          <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
            {t("settings.skills.featured", { defaultValue: "Agent skills" })}
          </h2>
          <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
            {skills.length}
          </span>
        </div>
        {skills.length ? (
          <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
            {skills.map((skill) => (
              <SkillCatalogRow
                key={`${skill.source}:${skill.name}`}
                skill={skill}
                onSelect={setSelectedSkill}
              />
            ))}
          </div>
        ) : (
          <div className="px-3 py-12 text-center text-sm text-muted-foreground">
            {t("settings.skills.empty", { defaultValue: "No skills are available." })}
          </div>
        )}
      </section>

      <ReadOnlyOperationalSkillsPanel skills={skills} />

      <SkillDetailSheet
        skill={selectedSkill}
        open={selectedSkill !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedSkill(null);
        }}
      />
    </div>
  );
}

const MANAGED_STATUS_ORDER: ManagedSkillStatus[] = [
  "candidate",
  "verified",
  "deprecated",
  "rejected",
  "system",
];

function ManagedSkillsSettings({
  skills,
  drafts,
  statusCounts,
  onReload,
}: {
  skills: ManagedSkill[];
  drafts: ManagedSkillDraft[];
  statusCounts: Partial<Record<ManagedSkillStatus, number>>;
  onReload: () => void;
}) {
  const { token } = useClient();
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(
    () => skills.find((skill) => skill.status !== "draft")?.name ?? skills[0]?.name ?? null,
  );
  const [statusFilter, setStatusFilter] = useState<ManagedSkillStatus | "all">("all");
  const [detail, setDetail] = useState<ManagedSkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [initialCreateDraft, setInitialCreateDraft] = useState<ManagedSkillDraft | null>(null);
  const [audit, setAudit] = useState<SkillAuditReport | null>(null);
  const [auditBusy, setAuditBusy] = useState(false);

  const registryDrafts = useMemo(
    () => skills.filter((skill) => skill.status === "draft"),
    [skills],
  );
  const operationalSkills = useMemo(
    () => skills.filter((skill) => skill.status !== "draft"),
    [skills],
  );
  const filteredSkills = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return operationalSkills.filter((skill) => {
      if (statusFilter !== "all" && skill.status !== statusFilter) return false;
      if (!normalized) return true;
      return [skill.name, skill.description, skill.category]
        .join(" ")
        .toLowerCase()
        .includes(normalized);
    });
  }, [operationalSkills, query, statusFilter]);
  const selectedSkill = skills.find((skill) => skill.name === selectedName) ?? filteredSkills[0] ?? registryDrafts[0] ?? null;

  useEffect(() => {
    if (!selectedSkill) {
      setSelectedName(null);
      return;
    }
    if (!skills.some((skill) => skill.name === selectedName)) {
      setSelectedName(selectedSkill.name);
    }
  }, [selectedName, selectedSkill, skills]);

  useEffect(() => {
    if (!selectedSkill) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetail(null);
    fetchManagedSkillDetail(token, selectedSkill.name)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSkill, token]);

  const runAction = async (
    skill: ManagedSkill,
    action: "approve" | "promote" | "deprecate" | "reject",
  ) => {
    setActionBusy(`${skill.name}:${action}`);
    setMessage(null);
    try {
      const payload = await runManagedSkillStatusAction(token, skill.name, action);
      setSelectedName(payload.skill.name);
      setMessage(statusActionMessage(action, payload.skill.status));
      onReload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Skill status update failed.");
    } finally {
      setActionBusy(null);
    }
  };

  const moveSelection = (delta: number) => {
    const currentIndex = filteredSkills.findIndex((skill) => skill.name === selectedName);
    const next = filteredSkills[Math.max(0, Math.min(filteredSkills.length - 1, currentIndex + delta))];
    if (next) setSelectedName(next.name);
  };

  const runAudit = async () => {
    setAuditBusy(true);
    setMessage(null);
    try {
      const payload = await runSkillAudit(token);
      setAudit(payload.audit);
      setMessage(
        payload.audit.summary.attention
          ? `Audit found ${payload.audit.summary.attention} item(s) needing attention.`
          : "Audit completed with no attention findings.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Skill audit failed.");
    } finally {
      setAuditBusy(false);
    }
  };

  return (
    <div className="flex min-h-[min(72vh,48rem)] flex-col gap-4">
      <section className="flex flex-col gap-3 border-b border-border/45 pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <p className="max-w-[760px] text-[13px] leading-5 text-muted-foreground">
            Registry-backed skill management. Drafts are separated from operational skills so registration decisions stay visually distinct.
          </p>
          {message ? (
            <p className="mt-2 text-[12px] font-medium text-muted-foreground">{message}</p>
          ) : null}
        </div>
        <div className="flex flex-col gap-2 sm:items-end">
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={runAudit}
              disabled={auditBusy}
              className="h-9 rounded-[10px]"
            >
              {auditBusy ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <CircleAlert className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              Run audit
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={() => {
                setInitialCreateDraft(null);
                setCreateOpen(true);
              }}
              className="h-9 rounded-[10px]"
            >
              <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              New skill
            </Button>
          </div>
          <div className="grid grid-cols-3 gap-2 text-right sm:flex">
            <MetricPill label="draft" value={statusCounts.draft ?? 0} />
            <MetricPill label="candidate" value={statusCounts.candidate ?? 0} />
            <MetricPill label="verified" value={statusCounts.verified ?? 0} />
          </div>
        </div>
      </section>

      {audit ? <SkillAuditAttentionPanel audit={audit} /> : null}

      <OperationalSkillsPanel skills={operationalSkills} />

      {drafts.length || registryDrafts.length ? (
        <section className="rounded-[18px] border border-amber-500/20 bg-amber-500/[0.055] p-3">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-amber-700 dark:text-amber-300">
              Inbox
            </h2>
            <span className="text-[12px] text-muted-foreground">{drafts.length + registryDrafts.length}</span>
          </div>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {drafts.map((draft) => (
              <button
                key={draft.draft_id}
                type="button"
                onClick={() => {
                  setInitialCreateDraft(draft);
                  setCreateOpen(true);
                }}
                className="min-w-[15rem] rounded-[12px] border border-amber-500/20 bg-background/55 px-3 py-2 text-left transition-colors hover:bg-background/80"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-semibold">{draft.name}</span>
                  <DraftStatusBadge status={draft.status} />
                </div>
                <p className="mt-1 line-clamp-2 text-[12px] leading-4 text-muted-foreground">
                  {draftInboxSummary(draft)}
                </p>
              </button>
            ))}
            {registryDrafts.map((skill) => (
              <button
                key={skill.name}
                type="button"
                onClick={() => setSelectedName(skill.name)}
                className={cn(
                  "min-w-[15rem] rounded-[12px] border px-3 py-2 text-left transition-colors",
                  selectedName === skill.name
                    ? "border-amber-500/60 bg-background"
                    : "border-amber-500/20 bg-background/55 hover:bg-background/80",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-semibold">{skill.name}</span>
                  <StatusBadge status={skill.status} />
                </div>
                <p className="mt-1 line-clamp-2 text-[12px] leading-4 text-muted-foreground">
                  {skill.description || "Waiting for registration review."}
                </p>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <div className="grid min-h-0 flex-1 gap-4 overflow-x-auto pb-1 lg:grid-cols-[minmax(20rem,24rem)_minmax(54rem,1fr)]">
        <section className="min-h-0 rounded-[18px] border border-border/50 bg-background/45">
          <div className="border-b border-border/45 p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search skills"
                className="h-9 rounded-[10px] pl-9 text-[13px]"
              />
            </div>
            <div className="mt-3 flex flex-wrap gap-1.5">
              <StatusFilterButton active={statusFilter === "all"} onClick={() => setStatusFilter("all")}>
                All
              </StatusFilterButton>
              {MANAGED_STATUS_ORDER.map((status) => (
                <StatusFilterButton
                  key={status}
                  active={statusFilter === status}
                  onClick={() => setStatusFilter(status)}
                >
                  {status}
                </StatusFilterButton>
              ))}
            </div>
          </div>
          <div
            className="max-h-[min(60vh,38rem)] overflow-y-auto p-2"
            tabIndex={0}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                moveSelection(1);
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                moveSelection(-1);
              }
            }}
          >
            {filteredSkills.length ? (
              filteredSkills.map((skill) => (
                <ManagedSkillRow
                  key={skill.name}
                  skill={skill}
                  selected={selectedName === skill.name}
                  onSelect={() => setSelectedName(skill.name)}
                />
              ))
            ) : (
              <div className="px-3 py-10 text-center text-sm text-muted-foreground">
                No skills match this view.
              </div>
            )}
          </div>
        </section>

        <ManagedSkillDetailPanel
          detail={detail}
          fallbackSkill={selectedSkill}
          loading={detailLoading}
          actionBusy={actionBusy}
          onAction={runAction}
          onUpdated={(name) => {
            setSelectedName(name);
            setMessage("Skill instructions saved.");
            onReload();
          }}
        />
      </div>
      <SkillCreateWizard
        open={createOpen}
        onOpenChange={setCreateOpen}
        initialDraft={initialCreateDraft}
        onRegistered={(name) => {
          setSelectedName(name);
          setMessage(`Registered ${name}.`);
          onReload();
        }}
      />
    </div>
  );
}

function ManagedSkillRow({
  skill,
  selected,
  onSelect,
}: {
  skill: ManagedSkill;
  selected: boolean;
  onSelect: () => void;
}) {
  const successTone = successRateTone(skill.success_rate);
  const usageWidth = Math.min(100, Math.max(8, skill.usage_count * 8));
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "mb-1 flex w-full min-w-0 items-center gap-3 rounded-[12px] px-2.5 py-2.5 text-left transition-colors",
        selected ? "bg-muted shadow-sm" : "hover:bg-muted/55",
      )}
    >
      <span className={cn("h-2.5 w-2.5 shrink-0 rounded-full", successTone)} title={successRateTitle(skill.success_rate)} />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[13px] font-semibold leading-5 text-foreground">{skill.name}</span>
          <StatusBadge status={skill.status} />
        </div>
        <p className="line-clamp-1 text-[12px] leading-4 text-muted-foreground">
          {skill.category || skill.description || "Uncategorized"}
        </p>
        <div className="mt-1 h-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-foreground/35" style={{ width: `${usageWidth}%` }} />
        </div>
      </div>
      {skill.requires_exec ? (
        <Terminal className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-label="Requires execution" />
      ) : null}
    </button>
  );
}

function SkillAuditAttentionPanel({ audit }: { audit: SkillAuditReport }) {
  const attention = audit.attention.slice(0, 6);
  return (
    <section className="rounded-[18px] border border-amber-500/25 bg-amber-500/[0.055] p-3">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CircleAlert className="h-4 w-4 text-amber-600 dark:text-amber-300" aria-hidden />
            <h2 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-amber-700 dark:text-amber-300">
              Needs attention
            </h2>
          </div>
          <p className="mt-1 text-[12px] leading-4 text-muted-foreground">
            Advisory audit only. No skill status was changed.
          </p>
        </div>
        <div className="text-left text-[12px] text-muted-foreground sm:text-right">
          <div>{audit.summary.attention} attention · {audit.summary.reference} reference</div>
          <div className="max-w-[22rem] truncate" title={audit.report_path}>
            {audit.report_path}
          </div>
        </div>
      </div>
      {attention.length ? (
        <div className="space-y-2">
          {attention.map((item, index) => (
            <div
              key={`${item.code}:${index}`}
              className="rounded-[12px] border border-amber-500/20 bg-background/60 px-3 py-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Pill>{item.code}</Pill>
                <span className="min-w-0 truncate text-[13px] font-semibold">
                  {item.skill_names.join(", ")}
                </span>
              </div>
              <p className="mt-1 text-[12px] leading-4 text-muted-foreground">{item.message}</p>
              {item.cluster_keys?.length ? (
                <p className="mt-1 text-[11px] text-muted-foreground">
                  Keys: {item.cluster_keys.join(", ")}
                </p>
              ) : null}
            </div>
          ))}
          {audit.attention.length > attention.length ? (
            <div className="px-1 text-[12px] text-muted-foreground">
              {audit.attention.length - attention.length} more attention finding(s) are in the report file.
            </div>
          ) : null}
        </div>
      ) : (
        <div className="rounded-[12px] border border-border/40 bg-background/55 px-3 py-3 text-[13px] text-muted-foreground">
          No attention findings. Reference findings remain in the report file.
        </div>
      )}
    </section>
  );
}

function OperationalSkillsPanel({ skills }: { skills: ManagedSkill[] }) {
  const operational = skills.filter((skill) => isOperationalSkillStatus(skill.status));
  const [statusFilter, setStatusFilter] = useState<"all" | "system" | "verified" | "candidate">("all");
  const filtered = operational.filter((skill) => statusFilter === "all" || skill.status === statusFilter);
  const countFor = (status: "system" | "verified" | "candidate") =>
    operational.filter((skill) => skill.status === status).length;
  return (
    <section className="rounded-[18px] border border-border/50 bg-background/45 p-3">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-foreground/70">
            Installed skills
          </h2>
          <p className="mt-1 text-[12px] leading-4 text-muted-foreground">
            Operational skills currently available to the agent.
          </p>
        </div>
        <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
          {operational.length}
        </span>
      </div>
      <div className="mb-3 flex flex-wrap gap-1.5">
        <StatusFilterButton active={statusFilter === "all"} onClick={() => setStatusFilter("all")}>
          All {operational.length}
        </StatusFilterButton>
        <StatusFilterButton active={statusFilter === "system"} onClick={() => setStatusFilter("system")}>
          System {countFor("system")}
        </StatusFilterButton>
        <StatusFilterButton active={statusFilter === "verified"} onClick={() => setStatusFilter("verified")}>
          Verified {countFor("verified")}
        </StatusFilterButton>
        <StatusFilterButton active={statusFilter === "candidate"} onClick={() => setStatusFilter("candidate")}>
          Candidate {countFor("candidate")}
        </StatusFilterButton>
      </div>
      {operational.length ? (
        <div className="max-h-[13.5rem] overflow-y-auto pr-1 [scrollbar-gutter:stable]">
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {filtered.map((skill) => (
              <div key={skill.name} className="min-w-0 rounded-[12px] border border-border/40 bg-muted/15 px-3 py-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-[13px] font-semibold text-foreground">{skill.name}</span>
                  <StatusBadge status={skill.status} />
                </div>
                <p className="mt-1 truncate text-[12px] text-muted-foreground">
                  {skill.category || skill.description || "Uncategorized"}
                </p>
              </div>
            ))}
          </div>
          {!filtered.length ? (
            <div className="px-3 py-8 text-center text-sm text-muted-foreground">
              No skills match this status.
            </div>
          ) : null}
        </div>
      ) : (
        <div className="px-3 py-8 text-center text-sm text-muted-foreground">
          No operational skills are installed yet.
        </div>
      )}
    </section>
  );
}

function ReadOnlyOperationalSkillsPanel({ skills }: { skills: SkillSummary[] }) {
  const available = skills.filter((skill) => skill.available);
  return (
    <section className="rounded-[18px] border border-border/50 bg-background/45 p-3">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-foreground/70">
            Installed skills
          </h2>
          <p className="mt-1 text-[12px] leading-4 text-muted-foreground">
            Skills currently available to the agent in this workspace.
          </p>
        </div>
        <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
          {available.length}
        </span>
      </div>
      {available.length ? (
        <div className="max-h-[13.5rem] overflow-y-auto pr-1 [scrollbar-gutter:stable]">
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {available.map((skill) => (
              <div key={`${skill.source}:${skill.name}`} className="min-w-0 rounded-[12px] border border-border/40 bg-muted/15 px-3 py-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-[13px] font-semibold text-foreground">{skill.name}</span>
                  <Pill>{skill.source}</Pill>
                </div>
                <p className="mt-1 truncate text-[12px] text-muted-foreground">
                  {skill.description || "No description."}
                </p>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="px-3 py-8 text-center text-sm text-muted-foreground">
          No operational skills are installed yet.
        </div>
      )}
    </section>
  );
}

export function InstalledToolsPanel({
  tools,
  title = "Installed tools",
  description = "Read-only ledger from workspace/tools/installed.md. Actions stay in chat.",
}: {
  tools: InstalledExternalTool[];
  title?: string;
  description?: string;
}) {
  return (
    <section className="rounded-[18px] border border-border/50 bg-background/45 p-3">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="text-[12px] font-semibold uppercase tracking-[0.08em] text-foreground/70">
            {title}
          </h2>
          <p className="mt-1 text-[12px] leading-4 text-muted-foreground">
            {description}
          </p>
        </div>
        <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
          {tools.length}
        </span>
      </div>
      <div className="overflow-x-auto rounded-[12px] border border-border/45">
        {tools.length ? (
          <div className="min-w-[54rem]">
            <div className="grid grid-cols-[minmax(8rem,1.2fr)_minmax(10rem,1.6fr)_7rem_7rem_7rem_9rem] gap-3 border-b border-border/45 bg-muted/45 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
              <span>Name</span>
              <span>Description</span>
              <span>Installed</span>
              <span>Version</span>
              <span>Status</span>
              <span>Last check</span>
            </div>
            {tools.map((tool) => (
              <div
                key={`${tool.name}:${tool.path || tool.source}`}
                className="grid grid-cols-[minmax(8rem,1.2fr)_minmax(10rem,1.6fr)_7rem_7rem_7rem_9rem] gap-3 border-b border-border/30 px-3 py-2.5 text-[12px] last:border-b-0"
              >
                <span className="min-w-0 truncate font-medium text-foreground" title={tool.path || tool.source}>
                  {tool.name}
                </span>
                <span className="min-w-0 truncate text-muted-foreground">
                  {tool.description || tool.source || tool.path || "-"}
                </span>
                <span className="truncate text-muted-foreground">{tool.installed_at || "-"}</span>
                <span className="truncate text-muted-foreground">{tool.version || "-"}</span>
                <span className={cn("truncate font-medium", installedToolStatusTone(tool.status))}>
                  {installedToolStatusLabel(tool.status)}
                </span>
                <span className="truncate text-muted-foreground">{tool.last_checked_at || "Not checked"}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="px-3 py-8 text-center text-sm text-muted-foreground">
            No installed external tools are recorded yet.
          </div>
        )}
      </div>
    </section>
  );
}

function SkillCreateWizard({
  open,
  onOpenChange,
  initialDraft,
  onRegistered,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialDraft: ManagedSkillDraft | null;
  onRegistered: (name: string) => void;
}) {
  const { token } = useClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [trigger, setTrigger] = useState("");
  const [method, setMethod] = useState("");
  const [fullPrompt, setFullPrompt] = useState("");
  const [category, setCategory] = useState("general");
  const [riskLevel, setRiskLevel] = useState("low");
  const [requiresExec, setRequiresExec] = useState(false);
  const [importResult, setImportResult] = useState<ManagedSkillImportResult | null>(null);
  const [importing, setImporting] = useState(false);
  const [draft, setDraft] = useState<ManagedSkillDraft | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !initialDraft) return;
    setName(initialDraft.name);
    setDescription("");
    setTrigger("");
    setMethod("");
    setFullPrompt("");
    setCategory("general");
    setRiskLevel("low");
    setRequiresExec(false);
    setImportResult(null);
    setImporting(false);
    setDraft(initialDraft);
    setOverrideReason("");
    setError(null);
  }, [initialDraft, open]);

  const reset = () => {
    setName("");
    setDescription("");
    setTrigger("");
    setMethod("");
    setFullPrompt("");
    setCategory("general");
    setRiskLevel("low");
    setRequiresExec(false);
    setImportResult(null);
    setImporting(false);
    setDraft(null);
    setOverrideReason("");
    setBusy(false);
    setError(null);
  };

  const close = () => {
    if (busy) return;
    onOpenChange(false);
    reset();
  };

  const finishAndClose = () => {
    onOpenChange(false);
    reset();
  };

  const pollDraft = async (startingDraft: ManagedSkillDraft): Promise<ManagedSkillDraft> => {
    let nextDraft = startingDraft;
    for (let attempt = 0; attempt < DRAFT_POLL_ATTEMPTS && nextDraft.status === "composing"; attempt += 1) {
      if (attempt > 0) {
        await sleep(DRAFT_POLL_INTERVAL_MS);
      }
      nextDraft = (await fetchManagedSkillDraft(token, nextDraft.draft_id)).draft;
      setDraft(nextDraft);
    }
    return nextDraft;
  };

  useEffect(() => {
    if (!open || busy || draft?.status !== "composing") return;
    let cancelled = false;
    setBusy(true);
    pollDraft(draft)
      .then((nextDraft) => {
        if (cancelled) return;
        if (nextDraft.status === "composing") {
          setError("Composer is still running. Close this dialog and return to the draft later.");
        } else if (nextDraft.status === "failed") {
          setError("Composer failed. Adjust the request and compose again.");
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not refresh skill draft.");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, draft?.draft_id, draft?.status, open, token]);

  const composeDraft = async () => {
    setBusy(true);
    setError(null);
    try {
      const values = {
        name,
        description,
        trigger,
        method,
        category,
        risk_level: riskLevel,
        requires_exec: requiresExec,
      };
      const payload = importResult
        ? await createImportedManagedSkillDraft(token, {
            ...values,
            required_tools: importResult.fields.required_tools,
            install_sources: importResult.fields.install_sources,
            validation: importResult.validation,
            estimated_fields: importResult.estimated_fields,
          })
        : await composeManagedSkillDraft(token, values);
      let nextDraft = payload.draft;
      setDraft(nextDraft);
      setOverrideReason("");
      if (nextDraft.status === "composing") {
        nextDraft = await pollDraft(nextDraft);
        if (nextDraft.status === "composing") {
          setError("Composer is still running. Close this dialog and return to the draft later.");
        } else if (nextDraft.status === "failed") {
          setError("Composer failed. Adjust the request and compose again.");
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not compose skill draft.");
    } finally {
      setBusy(false);
    }
  };

  const approveDraft = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await approveManagedSkillDraft(token, draft.draft_id, { reason: overrideReason });
      onRegistered(payload.skill?.name ?? draft.name);
      finishAndClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not register skill draft.");
    } finally {
      setBusy(false);
    }
  };

  const applyFullPrompt = async () => {
    setImporting(true);
    setError(null);
    try {
      const payload = await importManagedSkillText(token, fullPrompt);
      const parsed = payload.import;
      setImportResult(parsed);
      const fields = parsed.fields;
      if (fields.name) setName(fields.name);
      if (fields.description) setDescription(fields.description);
      if (fields.trigger) setTrigger(fields.trigger);
      if (fields.method) setMethod(fields.method);
      if (fields.category) setCategory(fields.category);
      if (fields.risk_level) setRiskLevel(fields.risk_level);
      if (typeof fields.requires_exec === "boolean") setRequiresExec(fields.requires_exec);
    } catch (err) {
      const parsed = parseSkillFullPrompt(fullPrompt);
      if (parsed.name) setName(parsed.name);
      if (parsed.description) setDescription(parsed.description);
      if (parsed.trigger) setTrigger(parsed.trigger);
      if (parsed.method) setMethod(parsed.method);
      if (parsed.category) setCategory(parsed.category);
      if (parsed.risk_level) setRiskLevel(parsed.risk_level);
      if (parsed.requires_exec !== null) setRequiresExec(parsed.requires_exec);
      setError(err instanceof Error ? err.message : "Could not import skill text.");
    } finally {
      setImporting(false);
    }
  };

  const canCompose = name.trim().length > 0 && description.trim().length > 0;
  const canApplyFullPrompt = fullPrompt.trim().length > 0 && !busy && !importing && draft === null;
  const importErrors = importResult?.validation.errors ?? [];
  const importWarnings = importResult?.validation.warnings ?? [];
  const estimatedFields = new Set(importResult?.estimated_fields ?? []);
  const draftReady = draft?.status === "ready";
  const draftRunning = draft?.status === "composing";
  const draftFailed = draft?.status === "failed";
  const governance = draft?.governance;
  const blockingFlags = governance?.blocking ?? [];
  const confirmationFlags = governance?.confirmations ?? [];
  const needsOverrideReason = Boolean(governance?.requires_confirmation);
  const canRegisterDraft = Boolean(
    draft
      && draftReady
      && !governance?.blocked
      && (!needsOverrideReason || overrideReason.trim().length > 0),
  );

  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? close() : onOpenChange(true))}>
      <DialogContent className="max-h-[min(90vh,58rem)] max-w-[92rem] overflow-hidden rounded-[22px] border-border/70 bg-popover p-0 shadow-2xl">
        <div className="border-b border-border/45 px-5 py-4">
          <DialogHeader>
            <DialogTitle>New skill</DialogTitle>
            <DialogDescription>
              Compose a draft, review the generated instructions, then register it as candidate.
            </DialogDescription>
          </DialogHeader>
        </div>
        <div className="max-h-[calc(min(88vh,54rem)-8rem)] overflow-y-auto px-5 py-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(17rem,0.9fr)_minmax(22rem,1fr)_minmax(22rem,1.15fr)]">
            <div className="space-y-3">
              <LabeledField label="Name">
                <Input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="review-renewal-notes"
                  disabled={busy || draft !== null}
                  className="h-9 rounded-[10px]"
                />
                {estimatedFields.has("name") ? <EstimatedBadge /> : null}
              </LabeledField>
              <LabeledField label="Description">
                <Textarea
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  placeholder="Review renewal notes and surface customer risk."
                  disabled={busy || draft !== null}
                  className="min-h-[5rem] resize-y rounded-[10px]"
                />
                {estimatedFields.has("description") ? <EstimatedBadge /> : null}
              </LabeledField>
              <LabeledField label="Trigger utterances">
                <Textarea
                  value={trigger}
                  onChange={(event) => setTrigger(event.target.value)}
                  placeholder={"review this renewal\ncustomer renewal risk"}
                  disabled={busy || draft !== null}
                  className="min-h-[5rem] resize-y rounded-[10px]"
                />
              </LabeledField>
              <div className="grid gap-3 sm:grid-cols-2">
                <LabeledField label="Category">
                  <Input
                    value={category}
                    onChange={(event) => setCategory(event.target.value)}
                    disabled={busy || draft !== null}
                    className="h-9 rounded-[10px]"
                  />
                  {estimatedFields.has("category") ? <EstimatedBadge /> : null}
                </LabeledField>
                <LabeledField label="Risk">
                  <select
                    value={riskLevel}
                    onChange={(event) => setRiskLevel(event.target.value)}
                    disabled={busy || draft !== null}
                    className="h-9 w-full rounded-[10px] border border-input bg-background px-3 text-sm"
                  >
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                  </select>
                  {estimatedFields.has("risk_level") ? <EstimatedBadge /> : null}
                </LabeledField>
              </div>
              <label className="flex items-center gap-2 rounded-[10px] border border-border/45 px-3 py-2 text-[13px]">
                <input
                  type="checkbox"
                  checked={requiresExec}
                  onChange={(event) => setRequiresExec(event.target.checked)}
                  disabled={busy || draft !== null}
                  className="h-4 w-4"
                />
                Requires execution tools
                {estimatedFields.has("requires_exec") ? <EstimatedBadge /> : null}
              </label>
              <LabeledField label="Method draft">
                <Textarea
                  value={method}
                  onChange={(event) => setMethod(event.target.value)}
                  placeholder={"# Method\n1. Read the input.\n2. Identify risks.\n3. Return concise findings."}
                  disabled={busy || draft !== null}
                  className="min-h-[9rem] resize-y rounded-[10px] font-mono text-[12px] leading-5"
                />
              </LabeledField>
            </div>
            <div className="space-y-3">
              <LabeledField label="Smart paste">
                <Textarea
                  value={fullPrompt}
                  onChange={(event) => setFullPrompt(event.target.value)}
                  placeholder={[
                    "Paste a ClawHub SKILL.md, another agent's skill file, or rough prompt text.",
                    "",
                    "---",
                    "name: review-renewal-notes",
                    "description: Review renewal notes and surface customer risk.",
                    "metadata:",
                    "  nanobot:",
                    "    category: business.review",
                    "    risk_level: low",
                    "    requires_exec: false",
                    "---",
                    "# Method",
                    "1. Read the input and return concise findings.",
                  ].join("\n")}
                  disabled={busy || draft !== null}
                  spellCheck={false}
                  className="min-h-[34rem] resize-y rounded-[10px] font-mono text-[12px] leading-5"
                />
              </LabeledField>
              <div className="flex items-center justify-between gap-2 rounded-[12px] border border-border/45 bg-muted/20 px-3 py-2">
                <p className="text-[12px] leading-4 text-muted-foreground">
                  Import parses frontmatter on the server. Non-standard text is normalized before draft creation.
                </p>
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  onClick={applyFullPrompt}
                  disabled={!canApplyFullPrompt}
                  className="h-8 shrink-0 rounded-[9px]"
                >
                  {importing ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
                  Import
                </Button>
              </div>
              {importResult ? (
                <div className="space-y-2 rounded-[12px] border border-border/45 bg-muted/15 px-3 py-2 text-[12px] leading-5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold">Import preview</span>
                    <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                      {importResult.mode}
                    </span>
                  </div>
                  {importResult.preserved_method ? (
                    <p className="text-muted-foreground">Method content is preserved for review.</p>
                  ) : null}
                  {importErrors.length ? (
                    <div className="rounded-[10px] bg-destructive/10 px-2.5 py-2 text-destructive">
                      {importErrors.map((item) => <div key={item}>{item}</div>)}
                    </div>
                  ) : null}
                  {importWarnings.length ? (
                    <div className="rounded-[10px] bg-amber-500/10 px-2.5 py-2 text-amber-700 dark:text-amber-300">
                      {importWarnings.map((item) => <div key={item}>{item}</div>)}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
            <div className="space-y-3">
              {draft ? (
                <>
                  <div
                    className={cn(
                      "rounded-[14px] border px-3.5 py-3 text-[13px] leading-5",
                      draftFailed
                        ? "border-destructive/30 bg-destructive/10"
                        : draftRunning
                          ? "border-sky-500/25 bg-sky-500/10"
                          : "border-emerald-500/25 bg-emerald-500/10",
                    )}
                  >
                    <div
                      className={cn(
                        "flex items-center gap-2 font-semibold",
                        draftFailed
                          ? "text-destructive"
                          : draftRunning
                            ? "text-sky-700 dark:text-sky-300"
                            : "text-emerald-700 dark:text-emerald-300",
                      )}
                    >
                      {draftRunning ? (
                        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                      ) : draftFailed ? (
                        <CircleAlert className="h-4 w-4" aria-hidden />
                      ) : (
                        <Check className="h-4 w-4" aria-hidden />
                      )}
                      {draftRunning ? "Composer running" : draftFailed ? "Composer failed" : "Draft ready"}
                    </div>
                    <p className="mt-1 text-muted-foreground">
                      {draftRunning
                        ? "Generating the method, review, and routing cases. You can return after the draft is ready."
                        : draftFailed
                          ? typeof draft.review.summary === "string"
                            ? draft.review.summary
                            : "The draft could not be composed."
                          : "Review the generated method, routing cases, and governance checks before registration."}
                    </p>
                  </div>
                  {draftReady && (blockingFlags.length || confirmationFlags.length) ? (
                    <div
                      className={cn(
                        "rounded-[14px] border px-3.5 py-3 text-[13px] leading-5",
                        blockingFlags.length
                          ? "border-destructive/30 bg-destructive/10"
                          : "border-amber-500/30 bg-amber-500/10",
                      )}
                    >
                      <div
                        className={cn(
                          "flex items-center gap-2 font-semibold",
                          blockingFlags.length
                            ? "text-destructive"
                            : "text-amber-700 dark:text-amber-300",
                        )}
                      >
                        <CircleAlert className="h-4 w-4" aria-hidden />
                        {blockingFlags.length ? "Registration blocked" : "Confirmation required"}
                      </div>
                      <div className="mt-2 space-y-1.5">
                        {[...blockingFlags, ...confirmationFlags].map((flag, index) => (
                          <p key={`${flag.kind}:${index}`} className="text-muted-foreground">
                            {draftGovernanceFlagLabel(flag)}
                          </p>
                        ))}
                      </div>
                      {needsOverrideReason && !blockingFlags.length ? (
                        <Textarea
                          value={overrideReason}
                          onChange={(event) => setOverrideReason(event.target.value)}
                          placeholder="Example: Internal-only skill; neighboring trigger overlap is intentional."
                          disabled={busy}
                          className="mt-3 min-h-[4.5rem] resize-y rounded-[10px] bg-background"
                        />
                      ) : null}
                    </div>
                  ) : null}
                  {draftReady ? (
                    <>
                      <DetailSection title="Review">
                        <pre className="max-h-28 overflow-auto rounded-[12px] bg-muted/35 p-3 text-[12px] leading-5">
                          {JSON.stringify(draft.review, null, 2)}
                        </pre>
                      </DetailSection>
                      <DetailSection title="Routing cases">
                        <div className="space-y-1.5">
                          {draft.routing_cases.map((row) => (
                            <div key={`${row.query}:${row.expected}`} className="rounded-[10px] bg-muted/30 px-3 py-2 text-[12px]">
                              <div className="truncate font-medium">{row.query}</div>
                              <div className="text-muted-foreground">expected {row.expected}</div>
                            </div>
                          ))}
                        </div>
                      </DetailSection>
                      <DetailSection title="Draft">
                        <div className="max-h-72 overflow-auto rounded-[12px] border border-border/45 bg-muted/15 px-3 py-2">
                          <MarkdownText className="max-w-none text-[13px] leading-6">
                            {draft.markdown}
                          </MarkdownText>
                        </div>
                      </DetailSection>
                    </>
                  ) : null}
                </>
              ) : (
                <DetailSection title="SKILL.md preview">
                  {importResult?.normalized_markdown ? (
                    <div className="max-h-[34rem] overflow-auto rounded-[12px] border border-border/45 bg-muted/15 px-3 py-2">
                      <MarkdownText className="max-w-none text-[13px] leading-6">
                        {formatSkillMarkdownForPreview(importResult.normalized_markdown)}
                      </MarkdownText>
                    </div>
                  ) : (
                    <div className="flex min-h-[24rem] items-center justify-center rounded-[16px] border border-dashed border-border/60 px-6 text-center text-[13px] leading-5 text-muted-foreground">
                      Paste skill text and import it to preview the normalized SKILL.md.
                    </div>
                  )}
                </DetailSection>
              )}
            </div>
          </div>
          {error ? (
            <p className="mt-4 rounded-[10px] bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">
              {error}
            </p>
          ) : null}
        </div>
        <DialogFooter className="border-t border-border/45 px-5 py-4 sm:space-x-0">
          <Button type="button" variant="outline" onClick={close} disabled={busy}>
            Cancel
          </Button>
          {draft ? (
            <Button type="button" onClick={approveDraft} disabled={busy || !canRegisterDraft}>
              {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
              Register
            </Button>
          ) : (
            <Button type="button" onClick={composeDraft} disabled={busy || !canCompose || importErrors.length > 0}>
              {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
              Create draft
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-[12px] font-semibold text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function EstimatedBadge() {
  return (
    <span className="inline-flex w-fit rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
      estimated
    </span>
  );
}

function ManagedSkillDetailPanel({
  detail,
  fallbackSkill,
  loading,
  actionBusy,
  onAction,
  onUpdated,
}: {
  detail: ManagedSkillDetail | null;
  fallbackSkill: ManagedSkill | null;
  loading: boolean;
  actionBusy: string | null;
  onAction: (
    skill: ManagedSkill,
    action: "approve" | "promote" | "deprecate" | "reject",
  ) => void;
  onUpdated: (name: string) => void;
}) {
  const { token } = useClient();
  const skill = detail?.skill ?? fallbackSkill;
  const [editing, setEditing] = useState(false);
  const [editorTab, setEditorTab] = useState<"edit" | "preview">("edit");
  const [draftMarkdown, setDraftMarkdown] = useState("");
  const [assessment, setAssessment] = useState<ManagedSkillUpdateAssessment | null>(null);
  const [confirmMajorOpen, setConfirmMajorOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [routingBusy, setRoutingBusy] = useState(false);
  const [routingResult, setRoutingResult] = useState<ManagedSkillRoutingTestPayload | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [routingError, setRoutingError] = useState<string | null>(null);

  useEffect(() => {
    if (!editing) {
      setDraftMarkdown(detail?.raw_markdown ?? "");
      setAssessment(null);
      setEditorError(null);
      setConfirmMajorOpen(false);
      setRoutingResult(null);
      setRoutingError(null);
    }
  }, [detail?.raw_markdown, detail?.skill.name, editing]);

  if (!skill) {
    return (
      <section className="rounded-[18px] border border-border/50 p-8 text-center text-sm text-muted-foreground">
        Select a skill to inspect it.
      </section>
    );
  }
  const actions = allowedStatusActions(skill.status);
  const canEdit = skill.status !== "system" && Boolean(detail?.raw_markdown);

  const startEditing = () => {
    setDraftMarkdown(detail?.raw_markdown ?? "");
    setAssessment(null);
    setEditorError(null);
    setEditorTab("edit");
    setEditing(true);
  };

  const closeEditor = () => {
    if (saving) return;
    setEditing(false);
    setDraftMarkdown(detail?.raw_markdown ?? "");
    setAssessment(null);
    setEditorError(null);
    setConfirmMajorOpen(false);
  };

  const applyUpdate = async () => {
    setSaving(true);
    setEditorError(null);
    try {
      const payload = await updateManagedSkillMarkdown(token, skill.name, draftMarkdown);
      setAssessment(payload.assessment);
      setEditing(false);
      setConfirmMajorOpen(false);
      onUpdated(payload.skill?.name ?? skill.name);
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : "Skill update failed.");
    } finally {
      setSaving(false);
    }
  };

  const assessAndSave = async () => {
    if (!detail || draftMarkdown === detail.raw_markdown) {
      setAssessment({
        kind: "noop",
        reasons: ["No instruction changes detected."],
        changed_fields: [],
        current_status: skill.status,
        next_status: skill.status,
        requires_revalidation: false,
      });
      return;
    }
    setSaving(true);
    setEditorError(null);
    try {
      const payload = await updateManagedSkillMarkdown(token, skill.name, draftMarkdown, { dryRun: true });
      setAssessment(payload.assessment);
      if (payload.assessment.kind === "major") {
        setConfirmMajorOpen(true);
        return;
      }
      await applyUpdate();
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : "Skill update assessment failed.");
    } finally {
      setSaving(false);
    }
  };

  const runRoutingTest = async () => {
    setRoutingBusy(true);
    setRoutingError(null);
    try {
      const payload = await runManagedSkillRoutingTest(token, skill.name);
      setRoutingResult(payload);
    } catch (error) {
      setRoutingError(error instanceof Error ? error.message : "Routing test failed.");
    } finally {
      setRoutingBusy(false);
    }
  };

  return (
    <section className="min-h-0 overflow-hidden rounded-[18px] border border-border/50 bg-background/45">
      <div className="flex items-start justify-between gap-4 border-b border-border/45 px-4 py-4">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <h2 className="truncate text-[18px] font-semibold tracking-[-0.01em]">{skill.name}</h2>
            <StatusBadge status={skill.status} />
          </div>
          <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-muted-foreground">
            {skill.description || "No description."}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          {canEdit ? (
            <Button
              size="sm"
              variant={editing ? "outline" : "secondary"}
              onClick={editing ? closeEditor : startEditing}
              disabled={saving}
              className="h-8 rounded-[9px]"
            >
              {editing ? <X className="mr-1.5 h-3.5 w-3.5" aria-hidden /> : <Edit3 className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
              {editing ? "Cancel" : "Edit"}
            </Button>
          ) : null}
          {actions.map((action) => (
            <Button
              key={action}
              size="sm"
              variant={action === "reject" || action === "deprecate" ? "outline" : "default"}
              onClick={() => onAction(skill, action)}
              disabled={actionBusy === `${skill.name}:${action}`}
              className="h-8 rounded-[9px]"
            >
              {actionBusy === `${skill.name}:${action}` ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : statusActionIcon(action)}
              {statusActionLabel(action)}
            </Button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex h-56 items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          Loading details...
        </div>
      ) : (
        <div className="max-h-[min(62vh,40rem)] overflow-y-auto p-4">
          <div className="grid gap-2 sm:grid-cols-4">
            <MetaItem label="Risk" value={skill.risk_level} />
            <MetaItem label="Version" value={skill.version || "n/a"} />
            <MetaItem label="Usage" value={String(skill.usage_count)} />
            <MetaItem label="Success" value={successRateTitle(skill.success_rate)} />
          </div>

          <div className="mt-5 grid gap-4 xl:grid-cols-[minmax(0,3fr)_18rem]">
            <div className="space-y-5">
              <DetailSection title="Instructions">
                {editing ? (
                  <SkillInstructionEditor
                    markdown={draftMarkdown}
                    onChange={setDraftMarkdown}
                    tab={editorTab}
                    onTabChange={setEditorTab}
                    assessment={assessment}
                    error={editorError}
                    saving={saving}
                    onSave={assessAndSave}
                  />
                ) : (
                  <div className="rounded-[14px] border border-border/40 bg-muted/15 px-3.5 py-3">
                    <MarkdownText className="max-w-none text-[13px] leading-6 text-foreground/85">
                      {formatSkillMarkdownForPreview(detail?.raw_markdown || "No SKILL.md content.")}
                    </MarkdownText>
                  </div>
                )}
              </DetailSection>
              <DetailSection title="Recent trace">
                {detail?.traces.length ? (
                  <div className="space-y-2">
                    {detail.traces.slice(0, 5).map((trace) => (
                      <div key={trace.trace_id} className="rounded-[12px] bg-muted/30 px-3 py-2">
                        <div className="flex items-center justify-between gap-2 text-[12px]">
                          <span className="truncate font-medium">{trace.selection_reason || "trace"}</span>
                          <span className="shrink-0 text-muted-foreground">{trace.gate_result ?? "none"}</span>
                        </div>
                        <p className="mt-1 truncate text-[12px] text-muted-foreground">
                          {trace.query_digest || trace.session_key || trace.trace_id}
                        </p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-[13px] text-muted-foreground">No recent traces.</p>
                )}
              </DetailSection>
            </div>
            <div className="space-y-3">
              <DetailSection title="Routing test">
                <RoutingTestPanel
                  result={routingResult}
                  error={routingError}
                  busy={routingBusy}
                  onRun={runRoutingTest}
                />
              </DetailSection>
              <RelationList title="Conflicts" values={detail?.relations.conflicts_with ?? []} />
              <RelationList title="Supersedes" values={detail?.relations.supersedes ?? []} />
              <RelationList title="Fallback" values={detail?.relations.fallback_to ?? []} />
              <DetailSection title="Tools">
                <div className="flex flex-wrap gap-1.5">
                  {skill.required_tools.length ? (
                    skill.required_tools.map((tool) => <Pill key={tool}>{tool}</Pill>)
                  ) : (
                    <p className="text-[13px] text-muted-foreground">No explicit tools.</p>
                  )}
                </div>
              </DetailSection>
            </div>
          </div>
        </div>
      )}
      <MajorUpdateDialog
        open={confirmMajorOpen}
        assessment={assessment}
        saving={saving}
        error={editorError}
        onCancel={() => {
          if (!saving) setConfirmMajorOpen(false);
        }}
        onConfirm={applyUpdate}
      />
    </section>
  );
}

function RoutingTestPanel({
  result,
  error,
  busy,
  onRun,
}: {
  result: ManagedSkillRoutingTestPayload | null;
  error: string | null;
  busy: boolean;
  onRun: () => void;
}) {
  const accuracy = result && result.total > 0 ? `${Math.round(result.accuracy * 100)}%` : "n/a";
  return (
    <div className="space-y-2">
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={onRun}
        disabled={busy}
        className="h-8 rounded-[9px]"
      >
        {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <PlayCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
        Run test
      </Button>
      {result ? (
        result.available ? (
          <div className="rounded-[12px] bg-muted/30 px-3 py-2 text-[12px] leading-5">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold">
                {result.passed}/{result.total} passed
              </span>
              <span className={cn("font-semibold", result.accuracy >= 0.9 ? "text-emerald-600" : result.accuracy >= 0.7 ? "text-amber-600" : "text-destructive")}>
                {accuracy}
              </span>
            </div>
            <div className="mt-2 space-y-1.5">
              {result.rows.slice(0, 5).map((row) => (
                <div key={`${row.query}:${row.expected}`} className="min-w-0 rounded-[9px] bg-background/65 px-2 py-1.5">
                  <div className="flex items-center gap-1.5">
                    {row.ok ? (
                      <Check className="h-3.5 w-3.5 shrink-0 text-emerald-600" aria-hidden />
                    ) : (
                      <CircleAlert className="h-3.5 w-3.5 shrink-0 text-destructive" aria-hidden />
                    )}
                    <span className="truncate font-medium">{row.query}</span>
                  </div>
                  <p className="mt-0.5 truncate text-muted-foreground">
                    {row.actual || "-"} / expected {row.expected || "-"}
                  </p>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <p className="rounded-[12px] bg-muted/30 px-3 py-2 text-[12px] leading-5 text-muted-foreground">
            No routing_cases.json found for this skill.
          </p>
        )
      ) : null}
      {error ? (
        <p className="rounded-[10px] bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function SkillInstructionEditor({
  markdown,
  onChange,
  tab,
  onTabChange,
  assessment,
  error,
  saving,
  onSave,
}: {
  markdown: string;
  onChange: (value: string) => void;
  tab: "edit" | "preview";
  onTabChange: (tab: "edit" | "preview") => void;
  assessment: ManagedSkillUpdateAssessment | null;
  error: string | null;
  saving: boolean;
  onSave: () => void;
}) {
  return (
    <div className="rounded-[14px] border border-border/45 bg-background">
      <details className="border-b border-border/45 px-3.5 py-3">
        <summary className="cursor-pointer text-[12px] font-semibold text-muted-foreground">
          Metadata form
        </summary>
        <p className="mt-2 text-[12px] leading-5 text-muted-foreground">
          Structured frontmatter editing will sit here. For now this editor preserves the full SKILL.md document and lets the server classify changes before writing.
        </p>
      </details>
      <div className="flex items-center justify-between gap-2 border-b border-border/45 px-3 py-2">
        <div className="flex rounded-[10px] bg-muted p-1">
          <EditorTabButton active={tab === "edit"} onClick={() => onTabChange("edit")}>
            <FileText className="h-3.5 w-3.5" aria-hidden />
            Edit
          </EditorTabButton>
          <EditorTabButton active={tab === "preview"} onClick={() => onTabChange("preview")}>
            <Eye className="h-3.5 w-3.5" aria-hidden />
            Preview
          </EditorTabButton>
        </div>
        <Button
          size="sm"
          onClick={onSave}
          disabled={saving}
          className="h-8 rounded-[9px]"
        >
          {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <Save className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
          Save instructions
        </Button>
      </div>
      <div className="p-3">
        {tab === "edit" ? (
          <Textarea
            aria-label="Skill instructions editor"
            value={markdown}
            onChange={(event) => onChange(event.target.value)}
            spellCheck={false}
            className="min-h-[24rem] resize-y rounded-[12px] border-border/50 font-mono text-[12px] leading-5"
          />
        ) : (
          <div className="min-h-[24rem] rounded-[12px] border border-border/45 bg-muted/15 px-3.5 py-3">
            <MarkdownText className="max-w-none text-[13px] leading-6 text-foreground/85">
              {formatSkillMarkdownForPreview(markdown || "No SKILL.md content.")}
            </MarkdownText>
          </div>
        )}
        {assessment ? <AssessmentNotice assessment={assessment} /> : null}
        {error ? (
          <p className="mt-3 rounded-[10px] bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function EditorTabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-[8px] px-2 text-[12px] font-medium transition-colors",
        active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function AssessmentNotice({ assessment }: { assessment: ManagedSkillUpdateAssessment }) {
  const major = assessment.kind === "major";
  return (
    <div
      className={cn(
        "mt-3 rounded-[12px] border px-3 py-2 text-[12px] leading-5",
        major
          ? "border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200"
          : "border-border/45 bg-muted/25 text-muted-foreground",
      )}
    >
      <div className="flex items-center gap-2 font-semibold">
        {major ? <CircleAlert className="h-3.5 w-3.5" aria-hidden /> : <Check className="h-3.5 w-3.5" aria-hidden />}
        {assessment.kind === "noop" ? "No changes" : `${assessment.kind} update`}
      </div>
      {assessment.reasons.length ? (
        <p className="mt-1">{assessment.reasons.join(" ")}</p>
      ) : null}
      {assessment.changed_fields.length ? (
        <p className="mt-1">Changed: {assessment.changed_fields.join(", ")}</p>
      ) : null}
    </div>
  );
}

function MajorUpdateDialog({
  open,
  assessment,
  saving,
  error,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  assessment: ManagedSkillUpdateAssessment | null;
  saving: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? onCancel() : undefined)}>
      <DialogContent className="max-w-xl rounded-[22px] border-border/70 bg-popover p-5 shadow-2xl">
        <DialogHeader>
          <DialogTitle>Method changed</DialogTitle>
          <DialogDescription>
            This skill will move from {assessment?.current_status ?? "current"} to {assessment?.next_status ?? "candidate"} and needs routing validation before it should be treated as stable again.
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-[14px] border border-amber-500/25 bg-amber-500/10 px-3.5 py-3 text-[13px] leading-5 text-amber-900 dark:text-amber-100">
          <div className="mb-1 flex items-center gap-2 font-semibold">
            <CircleAlert className="h-4 w-4" aria-hidden />
            Major patch
          </div>
          <p>
            Method, tools, execution requirements, or risk changed. The edit can be saved, but the skill is no longer considered verified until it is promoted again.
          </p>
          {assessment?.changed_fields.length ? (
            <p className="mt-2 text-[12px]">Changed: {assessment.changed_fields.join(", ")}</p>
          ) : null}
        </div>
        {error ? (
          <p className="rounded-[10px] bg-destructive/10 px-3 py-2 text-[12px] font-medium text-destructive">
            {error}
          </p>
        ) : null}
        <DialogFooter className="gap-2 sm:space-x-0">
          <Button type="button" variant="outline" onClick={onCancel} disabled={saving}>
            Cancel
          </Button>
          <Button type="button" onClick={onConfirm} disabled={saving}>
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : <Save className="mr-1.5 h-3.5 w-3.5" aria-hidden />}
            Save anyway
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StatusBadge({ status }: { status: ManagedSkillStatus }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none",
        status === "draft" && "bg-amber-500/12 text-amber-700 dark:text-amber-300",
        status === "candidate" && "bg-sky-500/12 text-sky-700 dark:text-sky-300",
        status === "verified" && "bg-emerald-500/12 text-emerald-700 dark:text-emerald-300",
        status === "deprecated" && "bg-muted text-muted-foreground",
        status === "rejected" && "bg-destructive/10 text-destructive",
        status === "system" && "bg-violet-500/12 text-violet-700 dark:text-violet-300",
      )}
    >
      {status}
    </span>
  );
}

function StatusFilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full px-2.5 py-1 text-[12px] font-medium transition-colors",
        active ? "bg-foreground text-background" : "bg-muted text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function DraftStatusBadge({ status }: { status: string }) {
  const tone = status === "ready"
    ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
    : status === "failed"
      ? "bg-destructive/10 text-destructive"
      : "bg-sky-500/10 text-sky-700 dark:text-sky-300";
  return (
    <span className={cn("shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold", tone)}>
      {status}
    </span>
  );
}

function draftInboxSummary(draft: ManagedSkillDraft): string {
  if (draft.status === "composing") return "Composer is running.";
  if (draft.status === "ready") return "Ready for review and registration.";
  if (draft.status === "failed") {
    return typeof draft.review.summary === "string" ? draft.review.summary : "Composer failed.";
  }
  return "Waiting for review.";
}

function MetricPill({ label, value }: { label: string; value: number }) {
  return (
    <span className="rounded-[12px] bg-muted/55 px-3 py-2">
      <span className="block text-[11px] text-muted-foreground">{label}</span>
      <span className="block text-[15px] font-semibold leading-5">{value}</span>
    </span>
  );
}

function RelationList({ title, values }: { title: string; values: string[] }) {
  return (
    <DetailSection title={title}>
      <div className="flex flex-wrap gap-1.5">
        {values.length ? (
          values.map((value) => <Pill key={value}>{value}</Pill>)
        ) : (
          <p className="text-[13px] text-muted-foreground">None</p>
        )}
      </div>
    </DetailSection>
  );
}

function successRateTone(rate: number | null): string {
  if (rate === null) return "bg-muted-foreground/35";
  if (rate >= 0.85) return "bg-emerald-500";
  if (rate >= 0.7) return "bg-amber-500";
  return "bg-destructive";
}

function successRateTitle(rate: number | null): string {
  return rate === null ? "No attempts" : `${Math.round(rate * 100)}%`;
}

function allowedStatusActions(status: ManagedSkillStatus): Array<"approve" | "promote" | "deprecate" | "reject"> {
  if (status === "draft") return ["approve", "reject"];
  if (status === "candidate") return ["promote"];
  if (status === "verified") return ["deprecate"];
  return [];
}

function isOperationalSkillStatus(status: ManagedSkillStatus): boolean {
  return status === "system" || status === "candidate" || status === "verified";
}

function statusActionLabel(action: "approve" | "promote" | "deprecate" | "reject"): string {
  if (action === "approve") return "등록";
  if (action === "promote") return "verified로 승격";
  if (action === "deprecate") return "사용 중지";
  return "반려";
}

function statusActionMessage(action: "approve" | "promote" | "deprecate" | "reject", status: ManagedSkillStatus): string {
  if (action === "approve") return `등록되었습니다. 현재 상태: ${status}`;
  if (action === "promote") return `승격되었습니다. 현재 상태: ${status}`;
  if (action === "deprecate") return `사용 중지되었습니다. 현재 상태: ${status}`;
  return `반려되었습니다. 현재 상태: ${status}`;
}

function statusActionIcon(action: "approve" | "promote" | "deprecate" | "reject") {
  if (action === "approve") return <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden />;
  if (action === "promote") return <ShieldCheck className="mr-1.5 h-3.5 w-3.5" aria-hidden />;
  if (action === "deprecate") return <Archive className="mr-1.5 h-3.5 w-3.5" aria-hidden />;
  return <X className="mr-1.5 h-3.5 w-3.5" aria-hidden />;
}

function SkillCatalogRow({
  skill,
  onSelect,
}: {
  skill: SkillSummary;
  onSelect: (skill: SkillSummary) => void;
}) {
  const { t } = useTranslation();
  const sourceLabel = skillSourceLabel(skill.source, t);
  const StatusIcon = skill.available ? Check : CircleAlert;
  const statusLabel = skill.available
    ? t("settings.skills.statusAvailable", { defaultValue: "Available" })
    : t("settings.skills.statusUnavailable", { defaultValue: "Unavailable" });

  return (
    <button
      type="button"
      aria-label={t("settings.skills.openDetails", {
        name: skill.name,
        defaultValue: "Open details for {{name}}",
      })}
      onClick={() => onSelect(skill)}
      className={cn(
        "group flex min-w-0 items-center gap-3 rounded-[16px] px-3 py-3 text-left transition-colors",
        "hover:bg-muted/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        !skill.available && "opacity-65",
      )}
    >
      <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[14px] bg-muted/70 text-muted-foreground">
        <Brain className="h-5 w-5" strokeWidth={1.8} aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="truncate text-[15px] font-semibold leading-5 text-foreground">
            {skill.name}
          </h3>
          <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold leading-none text-muted-foreground">
            {sourceLabel}
          </span>
        </div>
        <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-muted-foreground">
          {skill.description}
        </p>
        {!skill.available && skill.unavailable_reason ? (
          <p className="mt-1 truncate text-[12px] leading-4 text-muted-foreground/80">
            {t("settings.skills.unavailableReason", {
              reason: skill.unavailable_reason,
              defaultValue: "Missing: {{reason}}",
            })}
          </p>
        ) : null}
      </div>
      <span
        title={!skill.available && skill.unavailable_reason ? skill.unavailable_reason : undefined}
        className={cn(
          "hidden shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-medium sm:inline-flex",
          skill.available
            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : "bg-muted text-muted-foreground",
        )}
      >
        <StatusIcon className="h-3.5 w-3.5" aria-hidden />
        {statusLabel}
      </span>
    </button>
  );
}

function SkillDetailSheet({
  skill,
  open,
  onOpenChange,
}: {
  skill: SkillSummary | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    if (!open || !skill) return;
    let cancelled = false;
    setDetail(null);
    setLoading(true);
    setLoadFailed(false);
    fetchSkillDetail(token, skill.name)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, skill, token]);

  if (!skill) return null;

  const activeSkill = detail ?? skill;
  const sourceLabel = skillSourceLabel(activeSkill.source, t);
  const statusLabel = activeSkill.available
    ? t("settings.skills.statusAvailable", { defaultValue: "Available" })
    : t("settings.skills.statusUnavailable", { defaultValue: "Unavailable" });

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[min(34rem,calc(100vw-1rem))] max-w-none gap-0 overflow-hidden p-0 sm:max-w-none"
      >
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          <div className="flex items-start gap-3 pr-8">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[15px] bg-muted/70 text-muted-foreground">
              <Brain className="h-5 w-5" strokeWidth={1.8} aria-hidden />
            </div>
            <div className="min-w-0">
              <SheetTitle className="truncate text-[20px] font-semibold">
                {activeSkill.name}
              </SheetTitle>
              <SheetDescription className="sr-only">
                {t("settings.skills.detailDescription", {
                  name: activeSkill.name,
                  defaultValue: "Details for {{name}}.",
                })}
              </SheetDescription>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-muted-foreground">
                <Pill>{sourceLabel}</Pill>
                <Pill tone={activeSkill.available ? "success" : "muted"}>{statusLabel}</Pill>
              </div>
            </div>
          </div>

          {loading ? (
            <div className="mt-8 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {t("settings.skills.loadingDetail", { defaultValue: "Loading skill details..." })}
            </div>
          ) : loadFailed ? (
            <div className="mt-8 rounded-[16px] bg-destructive/10 px-3 py-3 text-sm text-destructive">
              {t("settings.skills.loadFailed", { defaultValue: "Could not load skill details." })}
            </div>
          ) : (
            <div className="mt-7 space-y-6">
              <DetailSection title={t("settings.skills.descriptionTitle", { defaultValue: "Description" })}>
                <p className="text-[14px] leading-6 text-muted-foreground">{activeSkill.description}</p>
              </DetailSection>

              <div className="grid grid-cols-2 gap-2">
                <MetaItem
                  label={t("settings.skills.source", { defaultValue: "Source" })}
                  value={sourceLabel}
                />
                <MetaItem
                  label={t("settings.skills.status", { defaultValue: "Status" })}
                  value={statusLabel}
                />
              </div>

              {!activeSkill.available && activeSkill.unavailable_reason ? (
                <DetailSection
                  title={t("settings.skills.unavailableReasonLabel", {
                    defaultValue: "Unavailable reason",
                  })}
                >
                  <p className="text-[13px] leading-5 text-destructive/85">
                    {activeSkill.unavailable_reason}
                  </p>
                </DetailSection>
              ) : null}

              {detail ? <RequirementsSection detail={detail} /> : null}

              {detail ? <RawInstructionsBlock markdown={detail.raw_markdown} /> : null}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function RawInstructionsBlock({ markdown }: { markdown: string }) {
  const { t } = useTranslation();
  const content =
    markdown ||
    t("settings.skills.rawInstructionsEmpty", {
      defaultValue: "No raw instructions.",
    });

  return (
    <details className="group rounded-[18px] border border-border/45 bg-muted/20 px-3 py-3">
      <summary className="cursor-pointer select-none text-[13px] font-medium text-foreground/90 transition-colors hover:text-foreground">
        {t("settings.skills.rawInstructions", { defaultValue: "Raw SKILL.md" })}
      </summary>
      <div className="mt-3 overflow-hidden rounded-[14px] border border-border/35 bg-background/70">
        <pre
          className={cn(
            "max-h-[min(42vh,32rem)] overflow-auto overscroll-contain px-3.5 py-3 pr-4",
            "whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.7] text-foreground/62",
            "scrollbar-thin scrollbar-track-transparent",
            "[&::-webkit-scrollbar]:h-1.5 [&::-webkit-scrollbar]:w-1.5",
            "[&::-webkit-scrollbar-thumb]:bg-muted-foreground/25",
          )}
        >
          {content}
        </pre>
      </div>
    </details>
  );
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[16px] bg-muted/35 px-3 py-2.5">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate text-[13px] font-medium text-foreground">{value}</div>
    </div>
  );
}

function RequirementsSection({ detail }: { detail: SkillDetail }) {
  const { t } = useTranslation();
  const { bins, env, missing_bins, missing_env } = detail.requirements;
  const hasRequirements = bins.length > 0 || env.length > 0;

  return (
    <DetailSection title={t("settings.skills.requirements", { defaultValue: "Requirements" })}>
      {hasRequirements ? (
        <div className="space-y-3">
          {missing_bins.length ? (
            <RequirementLine
              title={t("settings.skills.missingCommands", { defaultValue: "Missing CLI" })}
              items={missing_bins}
              tone="danger"
              icon={<Terminal className="h-3.5 w-3.5" aria-hidden />}
            />
          ) : null}
          {missing_env.length ? (
            <RequirementLine
              title={t("settings.skills.missingEnvironment", { defaultValue: "Missing ENV" })}
              items={missing_env}
              tone="danger"
              icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />}
            />
          ) : null}
          {bins.length ? (
            <RequirementLine
              title={t("settings.skills.commands", { defaultValue: "Commands" })}
              items={bins}
              icon={<Terminal className="h-3.5 w-3.5" aria-hidden />}
            />
          ) : null}
          {env.length ? (
            <RequirementLine
              title={t("settings.skills.environment", { defaultValue: "Environment variables" })}
              items={env}
              icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />}
            />
          ) : null}
        </div>
      ) : (
        <p className="text-[13px] text-muted-foreground">
          {t("settings.skills.noRequirements", { defaultValue: "No explicit requirements." })}
        </p>
      )}
    </DetailSection>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-[12px] font-medium text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

function draftGovernanceFlagLabel(flag: ManagedSkillDraftGovernanceFlag): string {
  if (flag.kind === "security") {
    return `Security review is ${flag.severity ?? "flagged"} risk.`;
  }
  if (flag.kind === "routing") {
    return `Routing test passed ${flag.passed ?? 0}/${flag.total ?? 10}.`;
  }
  if (flag.kind === "duplicate") {
    const score = typeof flag.score === "number" ? ` (${Math.round(flag.score * 100)}%)` : "";
    return `Duplicate check found a close neighbor${score}.`;
  }
  return flag.message ?? `${flag.kind} needs review.`;
}

function installedToolStatusLabel(status: string): string {
  if (status === "running") return "Running";
  if (status === "stopped") return "Stopped";
  if (!status || status === "unknown") return "Unknown";
  return status;
}

function installedToolStatusTone(status: string): string {
  if (status === "running") return "text-emerald-600 dark:text-emerald-300";
  if (status === "stopped") return "text-muted-foreground";
  return "text-amber-700 dark:text-amber-300";
}

function parseSkillFullPrompt(input: string): {
  name: string;
  description: string;
  trigger: string;
  method: string;
  category: string;
  risk_level: string;
  requires_exec: boolean | null;
} {
  const sections = splitSkillFullPrompt(input);
  const method = sections.method || sections.instructions || sections.instruction || "";
  const trigger = sections.triggers || sections.trigger || sections.trigger_utterances || "";
  const risk = (sections.risk || sections.risk_level || "").trim().toLowerCase();
  const requiresExecRaw = (sections.requires_exec || sections.requiresexec || sections.requires_execution || "").trim().toLowerCase();
  return {
    name: firstLine(sections.name),
    description: sections.description.trim(),
    trigger: normalizeListText(trigger),
    method: method.trim(),
    category: firstLine(sections.category),
    risk_level: ["low", "medium", "high"].includes(risk) ? risk : "",
    requires_exec: parseBooleanLike(requiresExecRaw),
  };
}

function splitSkillFullPrompt(input: string): Record<string, string> {
  const result: Record<string, string> = {};
  let currentKey = "";
  for (const rawLine of input.split(/\r?\n/)) {
    const line = rawLine.replace(/\s+$/, "");
    const heading = line.match(/^\s*(?:#{1,4}\s*)?([A-Za-z][\w\s-]{0,40})\s*:\s*(.*)$/);
    if (heading) {
      currentKey = heading[1].trim().toLowerCase().replace(/[-_]+/g, " ");
      result[currentKey] = heading[2].trim();
      continue;
    }
    if (!currentKey) {
      result.description = [result.description, line.trim()].filter(Boolean).join("\n");
      continue;
    }
    result[currentKey] = [result[currentKey], line].filter(Boolean).join("\n");
  }
  return Object.fromEntries(
    Object.entries(result).map(([key, value]) => [key.replace(/\s+/g, "_"), value]),
  );
}

function firstLine(value: string | undefined): string {
  return (value ?? "").split(/\r?\n/, 1)[0]?.trim() ?? "";
}

function normalizeListText(value: string): string {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim().replace(/^[-*]\s+/, ""))
    .filter(Boolean)
    .join("\n");
}

function parseBooleanLike(value: string): boolean | null {
  if (["true", "yes", "y", "1", "필요", "예"].includes(value)) return true;
  if (["false", "no", "n", "0", "불필요", "아니오"].includes(value)) return false;
  return null;
}

function formatSkillMarkdownForPreview(markdown: string): string {
  const match = markdown.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!match) return markdown;
  const frontmatter = match[1].trimEnd();
  const body = markdown.slice(match[0].length).replace(/^\s+/, "");
  return ["```yaml", frontmatter, "```", "", body].join("\n");
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function RequirementLine({
  title,
  items,
  icon,
  tone = "muted",
}: {
  title: string;
  items: string[];
  icon: ReactNode;
  tone?: "muted" | "danger";
}) {
  return (
    <div className="space-y-1.5">
      <div
        className={cn(
          "flex items-center gap-1.5 text-[12px]",
          tone === "danger" ? "text-destructive" : "text-muted-foreground",
        )}
      >
        {icon}
        {title}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <Pill key={item}>{item}</Pill>
        ))}
      </div>
    </div>
  );
}

function Pill({
  children,
  tone = "muted",
}: {
  children: ReactNode;
  tone?: "muted" | "success";
}) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
        tone === "success"
          ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function skillSourceLabel(source: string, t: TFunction): string {
  if (source === "workspace") {
    return t("settings.skills.sourceWorkspace", { defaultValue: "Custom" });
  }
  if (source === "builtin") {
    return t("settings.skills.sourceBuiltin", { defaultValue: "Built-in" });
  }
  return source;
}
