import * as React from "react";
import { Slot } from "@radix-ui/react-slot";

import { cn } from "@/lib/utils";

export interface OptionCardProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  selected?: boolean;
  /** Compose with a semantic child (e.g. a menu item or a noninteractive card). */
  asChild?: boolean;
}

const OptionCard = React.forwardRef<HTMLButtonElement, OptionCardProps>(
  ({ className, selected, asChild = false, type = "button", ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        type={asChild ? undefined : type}
        aria-pressed={asChild ? undefined : selected}
        data-selected={selected === undefined ? undefined : selected}
        {...props}
        className={cn(
          "flex w-full items-start gap-[11px] rounded-xl border border-border bg-card px-[14px] py-[13px] text-left text-[13.5px] text-foreground transition-colors hover:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
          "aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:shadow-[inset_0_0_0_1px_hsl(var(--primary))] data-[selected=true]:border-primary data-[selected=true]:bg-accent data-[selected=true]:shadow-[inset_0_0_0_1px_hsl(var(--primary))]",
          className,
        )}
      />
    );
  },

);
OptionCard.displayName = "OptionCard";

export { OptionCard };
