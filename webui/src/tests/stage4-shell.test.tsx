import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import { ThreadHeader } from "@/components/thread/ThreadHeader";
import { ThreadComposer } from "@/components/thread/ThreadComposer";
import { HomeHero } from "@/components/thread/HomeHero";
import type { ConnectionStatus } from "@/lib/types";

const connection = vi.hoisted(() => ({
  status: "open" as ConnectionStatus,
  listeners: new Set<(status: ConnectionStatus) => void>(),
}));
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: {
    status: connection.status,
    onStatus: (listener: (status: ConnectionStatus) => void) => {
      connection.listeners.add(listener);
      return () => connection.listeners.delete(listener);
    },
  } }),
}));

const noop = () => {};

describe("stage 4 shell presentation", () => {
  it("shows live connection changes in the header instead of a fixed connected label", () => {
    connection.status = "open";
    render(<ThreadHeader title="Home" onToggleSidebar={noop} theme="light" onToggleTheme={noop} />);
    expect(screen.getByRole("status")).toHaveTextContent("Connected");
    expect(screen.getByRole("status")).toHaveClass("status-pill", "bg-success-soft");
    for (const status of ["reconnecting", "closed", "error", "connecting", "idle", "open"] as const) {
      act(() => connection.listeners.forEach((listener) => listener(status)));
      expect(screen.getByRole("status")).toHaveTextContent({
        reconnecting: "Reconnecting", closed: "Disconnected", error: "Connection error",
        connecting: "Connecting", idle: "Idle", open: "Connected",
      }[status]);
    }
  });

  it("keeps utility navigation and footer connection alongside mockup styling", () => {
    const onOpenTools = vi.fn();
    render(<Sidebar sessions={[]} activeKey={null} loading={false}
      onNewChat={noop} onSelect={noop} onRequestDelete={noop} onTogglePin={noop}
      onRequestRename={noop} onToggleArchive={noop} onToggleGroup={noop}
      onRequestRenameProject={noop} onNewChatInProject={noop} onOpenSettings={noop}
      onOpenApps={noop} onOpenSkills={noop} onOpenTools={onOpenTools}
      onOpenAutomations={noop} onOpenSearch={noop} onOpenAgentManagement={noop}
      onToggleArchived={noop} onCollapse={noop} activeUtility="tools" />);
    expect(screen.getByText("nanobot-easy")).toBeInTheDocument();
    const selected = document.querySelector('[aria-current="page"]')!;
    expect(selected).toHaveClass("bg-accent", "text-primary", "rounded-[7px]");
    fireEvent.click(selected);
    expect(onOpenTools).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveClass("conn-badge");
    expect(document.querySelector(".new-chat")).toHaveClass("bg-primary", "justify-center");
    expect(document.querySelector(".side-search")).toHaveClass("border-border-strong", "rounded-[7px]");
  });

  it("existing quick action fills the draft and waits for explicit send", async () => {
    const onSend = vi.fn();
    render(<ThreadComposer onSend={onSend} variant="hero" quickActions={[
      { key: "summary", title: "Summary", prompt: "Summarize today's notes" },
    ]} />);
    const example = screen.getByRole("button", { name: "Summary" });
    expect(example).toHaveAttribute("type", "button");
    fireEvent.click(example);
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("Summarize today's notes"));
    expect(onSend).not.toHaveBeenCalled();
    const send = screen.getByRole("button", { name: "Send message" });
    expect(send).toHaveClass("bg-primary", "rounded-full");
    expect(document.querySelector(".group\\/composer")).toHaveClass("rounded-[16px]");
    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledWith("Summarize today's notes", undefined, undefined);
  });

  it("home counts still invoke the existing tools and skills callbacks", () => {
    const onOpenTools = vi.fn();
    const onOpenSkills = vi.fn();
    render(<HomeHero greeting="Hello" studentMode={false} toolsOnCount={3} skillsCount={2}
      onOpenTools={onOpenTools} onOpenSkills={onOpenSkills} />);
    screen.getAllByRole("button").forEach((button) => fireEvent.click(button));
    expect(onOpenTools).toHaveBeenCalledOnce();
    expect(onOpenSkills).toHaveBeenCalledOnce();
  });
});
