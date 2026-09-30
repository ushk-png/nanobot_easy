import * as React from "react";

import { cn } from "@/lib/utils";

export interface ChipProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "filter" | "example";
  selected?: boolean;
}

const Chip = React.forwardRef<HTMLButtonElement, ChipProps>(
  ({ className, variant = "filter", selected, type = "button", ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      aria-pressed={selected}
      {...props}
      className={cn(
        "inline-flex items-center justify-center rounded-full border border-border-strong bg-background text-[12.5px] transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
        "aria-pressed:border-primary aria-pressed:bg-primary aria-pressed:text-primary-foreground",
        variant === "filter"
          ? "px-[13px] py-1.5 font-medium text-foreground"
          : "px-[14px] py-2 text-foreground-secondary hover:bg-accent hover:text-primary",
        className,
      )}
    />
  ),
);
Chip.displayName = "Chip";

export { Chip };
