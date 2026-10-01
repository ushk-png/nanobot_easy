import { useTranslation } from "react-i18next";
import { Blocks, Brain, Wrench } from "lucide-react";

import { cn } from "@/lib/utils";
import { Chip } from "@/components/ui/chip";

/** Example questions are draft suggestions, never submit actions. */
export function HomeExampleQuestions({
  actions,
  onChoose,
}: {
  actions: { key: string; title: string; prompt: string }[];
  onChoose: (prompt: string) => void;
}) {
  return (
    <div className="example-grid mx-auto mt-5 flex w-full max-w-[620px] flex-wrap justify-center gap-2">
      {actions.map((action) => (
        <Chip key={action.key} variant="example" onClick={() => onChoose(action.prompt)}>
          {action.prompt}
        </Chip>
      ))}
    </div>
  );
}

interface HomeHeroProps {
  greeting: string;
  studentMode: boolean;
  providerLabel?: string | null;
  toolsOnCount: number;
  skillsCount: number;
  onOpenApps?: () => void;
  onOpenTools?: () => void;
  onOpenSkills?: () => void;
  examples?: { key: string; title: string; prompt: string }[];
  onChooseExample?: (text: string) => void;
}

function StatusPill({
  icon,
  children,
  onClick,
}: {
  icon: React.ReactNode;
  children: React.ReactNode;
  onClick?: () => void;
}) {
  const Comp = onClick ? "button" : "span";
  return (
    <Comp
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12px] font-medium",
        "status-pill border-success bg-success-soft text-success",
        onClick && "cursor-pointer border-dashed transition-colors hover:bg-background",
      )}
    >
      {icon}
      {children}
    </Comp>
  );
}

export function HomeHero({
  greeting,
  studentMode,
  providerLabel,
  toolsOnCount,
  skillsCount,
  onOpenApps,
  onOpenTools,
  onOpenSkills,
  examples = [],
  onChooseExample,
}: HomeHeroProps) {
  const { t } = useTranslation();
  return (
    <div className="flex w-full flex-col items-center gap-3 text-center animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
      <h1 className="max-w-[620px] text-balance text-[22px] font-semibold leading-snug tracking-[-0.02em] text-foreground">
        {greeting}
      </h1>
      {studentMode ? (
        <p className="text-[13px] font-medium text-warning">
          {t("thread.empty.studentModeNotice")}
        </p>
      ) : null}
      <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
        {providerLabel ? (
          <StatusPill icon={<Blocks className="h-3 w-3" aria-hidden />} onClick={onOpenApps}>
            {providerLabel}
          </StatusPill>
        ) : null}
        <StatusPill icon={<Wrench className="h-3 w-3" aria-hidden />} onClick={onOpenTools}>
          {t("thread.empty.toolsInUse", { count: toolsOnCount, defaultValue: `${toolsOnCount} tools on` })}
        </StatusPill>
        <StatusPill icon={<Brain className="h-3 w-3" aria-hidden />} onClick={onOpenSkills}>
          {t("thread.empty.skillsAvailable", { count: skillsCount, defaultValue: `${skillsCount} skills available` })}
        </StatusPill>
      </div>
      {onChooseExample && <HomeExampleQuestions actions={examples} onChoose={onChooseExample} />}
    </div>
  );
}
