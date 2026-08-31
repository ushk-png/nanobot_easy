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

export function ConnectionBadge({ showLabel = false }: { showLabel?: boolean }) {
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
      )}
      aria-live="polite"
      role="status"
      title={showLabel ? undefined : label}
    >
      <span className="relative flex h-2 w-2 shrink-0" aria-hidden>
        {pulsing && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-75" />
        )}
        <span className="relative inline-flex h-2 w-2 rounded-full bg-current" />
      </span>
      {showLabel ? <span className="whitespace-nowrap">{label}</span> : <span className="sr-only">{label}</span>}
    </span>
  );
}
