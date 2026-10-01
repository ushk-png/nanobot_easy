import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";
import type { ConnectionStatus } from "@/lib/types";

const COPY: Record<ConnectionStatus, { color: string }> = {
  idle: { color: "text-muted-foreground" },
  connecting: {
    color: "text-warning",
  },
  open: {
    color: "text-success",
  },
  reconnecting: {
    color: "text-warning",
  },
  closed: {
    color: "text-muted-foreground",
  },
  error: {
    color: "text-destructive",
  },
};

export function ConnectionBadge({
  showLabel = false,
  variant = "default",
}: {
  showLabel?: boolean;
  variant?: "default" | "footer" | "pill";
}) {
  const { t } = useTranslation();
  const { client } = useClient();
  const [status, setStatus] = useState<ConnectionStatus>(client.status);

  useEffect(() => client.onStatus(setStatus), [client]);

  const meta = COPY[status];
  const pulsing =
    status === "connecting" ||
    status === "reconnecting" ||
    status === "error";
  const label = t(`connection.${status}`);
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center transition-colors",
        showLabel
          ? "gap-1.5 rounded-full px-2.5 py-1.5 text-xs font-medium"
          : "h-8 w-8 justify-center rounded-full text-muted-foreground/70 hover:bg-sidebar-accent/65",
        meta.color,
        showLabel && variant === "footer" && "conn-badge gap-[5px] p-0 pr-0.5 text-[11px] font-normal",
        showLabel && variant === "pill" && "status-pill gap-1.5 rounded-full border border-current px-3 py-1.5 text-[12px] font-medium",
        variant === "pill" && status === "open" && "bg-success-soft",
        variant === "pill" && (status === "connecting" || status === "reconnecting") && "bg-warning-soft",
      )}
      aria-live="polite"
      role="status"
      title={showLabel ? undefined : label}
    >
      <span className={cn("relative flex shrink-0", variant === "default" ? "h-2 w-2" : "h-1.5 w-1.5")} aria-hidden>
        {pulsing && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-75" />
        )}
        <span className="relative inline-flex h-full w-full rounded-full bg-current" />
      </span>
      {showLabel ? <span className="whitespace-nowrap">{label}</span> : <span className="sr-only">{label}</span>}
    </span>
  );
}
