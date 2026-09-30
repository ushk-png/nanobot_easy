import * as React from "react";

import { cn } from "@/lib/utils";

export interface OptionCardProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  selected?: boolean;
}

const OptionCard = React.forwardRef<HTMLButtonElement, OptionCardProps>(
  ({ className, selected, type = "button", ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      aria-pressed={selected}
      {...props}
      className={cn(
        "flex w-full items-start gap-[11px] rounded-xl border border-border bg-card px-[14px] py-[13px] text-left text-[13.5px] text-foreground transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        "aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:shadow-[inset_0_0_0_1px_hsl(var(--primary))]",
        className,
      )}
    />
  ),
);
OptionCard.displayName = "OptionCard";

export { OptionCard };
