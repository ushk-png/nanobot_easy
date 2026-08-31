import { useTranslation } from "react-i18next";
import { Blocks, Brain, Wrench } from "lucide-react";

import { cn } from "@/lib/utils";

interface HomeHeroProps {
  greeting: string;
  studentMode: boolean;
  providerLabel?: string | null;
  toolsOnCount: number;
  skillsCount: number;
  onOpenApps?: () => void;
  onOpenTools?: () => void;
  onOpenSkills?: () => void;
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
        "border-success/35 bg-success-soft text-success",
        onClick && "transition-colors hover:bg-success/15",
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
}: HomeHeroProps) {
  const { t } = useTranslation();
  return (
    <div className="flex w-full flex-col items-center gap-3 text-center animate-in fade-in-0 slide-in-from-bottom-2 duration-500">
      <h1 className="max-w-[44rem] text-balance text-[34px] font-normal leading-[1.08] tracking-normal text-foreground sm:text-[48px] sm:leading-tight">
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
    </div>
  );
}
