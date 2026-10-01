import { createRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Input } from "@/components/ui/input";
import { OptionCard } from "@/components/ui/option-card";
import { PageHeader } from "@/components/ui/page-header";
import { Textarea } from "@/components/ui/textarea";
import { SegmentedControl, ToggleButton, StatusPill, SettingsGroup, SettingsRow, SettingsSectionTitle } from "@/components/settings/settings-primitives";

describe("shared UI primitives", () => {
  it("preserves button Slot, refs, disabled state and caller class overrides", () => {
    const ref = createRef<HTMLButtonElement>();
    const onClick = vi.fn();
    render(<><Button ref={ref} disabled onClick={onClick} className="rounded-full">Save</Button><Button asChild><a href="/help">Help</a></Button></>);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).not.toHaveBeenCalled();
    expect(ref.current).toBe(screen.getByRole("button", { name: "Save" }));
    expect(ref.current).toHaveClass("rounded-full");
    expect(ref.current).not.toHaveClass("rounded-lg");
    expect(screen.getByRole("link", { name: "Help" })).toHaveAttribute("href", "/help");
  });

  it("preserves native input and textarea attributes, change events and refs", () => {
    const inputRef = createRef<HTMLInputElement>();
    const textareaRef = createRef<HTMLTextAreaElement>();
    const onChange = vi.fn();
    render(<><Input ref={inputRef} aria-label="Name" onChange={onChange} /><Textarea ref={textareaRef} aria-label="Notes" disabled /></>);
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), { target: { value: "Frank" } });
    expect(onChange).toHaveBeenCalledOnce();
    expect(inputRef.current?.value).toBe("Frank");
    expect(textareaRef.current).toBeDisabled();
  });

  it("keeps switch labels and controlled callbacks", () => {
    const onChange = vi.fn();
    const { rerender } = render(<ToggleButton checked={false} onChange={onChange} label="Enabled" ariaLabel="Enable tool" />);
    fireEvent.click(screen.getByRole("switch", { name: "Enable tool" }));
    expect(onChange).toHaveBeenCalledWith(true);
    rerender(<ToggleButton checked onChange={onChange} label="Enabled" />);
    expect(screen.getByRole("switch", { name: "Enabled" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it("keeps segmented option values and exposes selected state", () => {
    const onChange = vi.fn();
    render(<SegmentedControl value="one" options={[{ value: "one", label: "One" }, { value: "two", label: "Two" }]} onChange={onChange} />);
    expect(screen.getByRole("button", { name: "One" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Two" }));
    expect(onChange).toHaveBeenCalledWith("two");
  });

  it("preserves settings content and status tones", () => {
    render(<><SettingsSectionTitle>General</SettingsSectionTitle><SettingsGroup><SettingsRow title="Model" description="Current model"><StatusPill tone="success">Connected</StatusPill></SettingsRow><SettingsRow title="Empty" /></SettingsGroup></>);
    expect(screen.getByRole("heading", { name: "General" })).toBeInTheDocument();
    expect(screen.getByText("Current model")).toBeInTheDocument();
    expect(screen.getByText("Connected").parentElement).toHaveClass("border-success", "bg-success-soft");
  });

  it("provides opt-in card and filter states, refs and native button behavior", () => {
    const ref = createRef<HTMLButtonElement>();
    const onClick = vi.fn();
    render(<><OptionCard ref={ref} selected onClick={onClick}>Option</OptionCard><Chip selected disabled onClick={onClick}>Filter</Chip><Chip variant="example">Example</Chip></>);
    expect(ref.current).toHaveAttribute("type", "button");
    expect(ref.current).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Option" }));
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    expect(onClick).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Example" })).not.toHaveAttribute("aria-pressed");
  });

  it("composes noninteractive cards without button-only attributes or nested buttons", () => {
    const onEdit = vi.fn();
    render(<OptionCard asChild selected><div aria-label="Agent card"><Button onClick={onEdit}>Edit agent</Button></div></OptionCard>);
    const card = screen.getByLabelText("Agent card");
    expect(card.tagName).toBe("DIV");
    expect(card).toHaveAttribute("data-selected", "true");
    expect(card).not.toHaveAttribute("type");
    expect(card).not.toHaveAttribute("aria-pressed");
    expect(card).not.toHaveAttribute("tabindex");
    expect(card.querySelector("button button")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit agent" }));
    expect(onEdit).toHaveBeenCalledOnce();
  });

  it("renders page heading, optional description and actions without default copy", () => {
    const { rerender } = render(<PageHeader title="Tools" description="Available tools" actions={<Button>Refresh</Button>} aria-label="Tools header" />);
    expect(screen.getByRole("heading", { level: 1, name: "Tools" })).toBeInTheDocument();
    expect(screen.getByText("Available tools")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    rerender(<PageHeader title="Skills" />);
    expect(screen.queryByText("Available tools")).not.toBeInTheDocument();
  });
});
