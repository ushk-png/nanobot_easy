import { StrictMode, useRef, useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ThreadComposer } from "@/components/thread/ThreadComposer";
import { HomeHero } from "@/components/thread/HomeHero";

const text = "Help me create a project plan.";
function Parent({ onSend }: { onSend: () => void }) {
  const counter = useRef(0);
  const [request, setRequest] = useState<{ id: number; text: string }>();
  return <>
    <HomeHero greeting="Hello" studentMode={false} toolsOnCount={0} skillsCount={0}
      examples={[{ key: "plan", title: "Plan", prompt: text }]}
      onChooseExample={(prompt) => setRequest({ id: ++counter.current, text: prompt })} />
    <ThreadComposer onSend={onSend} draftRequest={request} />
  </>;
}

describe("draft fill requests", () => {
  it("fills from a HomeHero Chip through its parent, focuses at the end and never sends", async () => {
    const onSend = vi.fn();
    render(<StrictMode><Parent onSend={onSend} /></StrictMode>);
    const input = screen.getByRole("textbox") as HTMLTextAreaElement;
    const chip = screen.getByRole("button", { name: text });
    fireEvent.click(chip);
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toHaveValue(text);
    expect(input.selectionStart).toBe(text.length);
    expect(input.selectionEnd).toBe(text.length);
    fireEvent.change(input, { target: { value: "Edited manually" } });
    fireEvent.click(chip);
    await waitFor(() => expect(input).toHaveValue(text));
    await waitFor(() => expect(input).toHaveFocus());
    expect(onSend).not.toHaveBeenCalled();
  });

  it("does not reapply the same ID on rerenders or object replacement", async () => {
    const onSend = vi.fn();
    const { rerender } = render(<ThreadComposer onSend={onSend} draftRequest={{ id: 1, text }} />);
    const input = screen.getByRole("textbox");
    await waitFor(() => expect(input).toHaveFocus());
    fireEvent.change(input, { target: { value: "Keep my edit" } });
    rerender(<ThreadComposer onSend={onSend} draftRequest={{ id: 1, text: "Do not apply" }} />);
    expect(input).toHaveValue("Keep my edit");
    rerender(<ThreadComposer onSend={onSend} draftRequest={{ id: 2, text }} />);
    expect(input).toHaveValue(text);
    expect(onSend).not.toHaveBeenCalled();
  });

  it.each(["disabled", "isStreaming"] as const)("ignores requests while %s and never replays them later", async (blocked) => {
    const onSend = vi.fn();
    const { rerender } = render(<ThreadComposer onSend={onSend} {...{ [blocked]: true }} draftRequest={{ id: 1, text }} />);
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(screen.getByRole("textbox")).not.toHaveFocus();
    rerender(<ThreadComposer onSend={onSend} draftRequest={{ id: 1, text }} />);
    expect(screen.getByRole("textbox")).toHaveValue("");
    rerender(<ThreadComposer onSend={onSend} draftRequest={{ id: 2, text }} />);
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
    expect(screen.getByRole("textbox")).toHaveValue(text);
    expect(onSend).not.toHaveBeenCalled();
  });
});
