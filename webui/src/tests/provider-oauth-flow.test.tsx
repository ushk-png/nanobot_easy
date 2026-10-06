import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProviderOAuthLoginDialog } from "@/components/settings/ProviderOAuthLoginDialog";
import { useProviderOAuthFlow } from "@/hooks/useProviderOAuthFlow";
import { completeProviderOAuth, loginProviderOAuth } from "@/lib/api";
import type { SettingsPayload } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    loginProviderOAuth: vi.fn(),
    completeProviderOAuth: vi.fn(),
  };
});

const login = vi.mocked(loginProviderOAuth);
const complete = vi.mocked(completeProviderOAuth);

const SETTINGS = { providers: [] } as unknown as SettingsPayload;
const REQUIRED = {
  status: "authorization_required" as const,
  provider: "xai_grok",
  flow_id: "flow-1",
  authorization_url: "https://auth.x.ai/oauth2/auth?state=abc",
  expires_in: 600,
};
const PENDING = { status: "pending" as const, provider: "xai_grok", flow_id: "flow-1" };

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function tick(ms = 1000) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useProviderOAuthFlow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    login.mockReset();
    complete.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("returns settings directly for one-step providers and opens no window", async () => {
    const open = vi.spyOn(window, "open");
    login.mockResolvedValue(SETTINGS);
    const { result } = renderHook(() => useProviderOAuthFlow("tok"));

    let payload: SettingsPayload | null | undefined;
    await act(async () => {
      payload = await result.current.login("openai_codex");
    });

    expect(payload).toBe(SETTINGS);
    expect(result.current.flow).toBeNull();
    expect(open).not.toHaveBeenCalled();
    expect(complete).not.toHaveBeenCalled();
  });

  it("opens the sign-in tab before the request and polls until the callback lands", async () => {
    const popup = { closed: false, close: vi.fn(), location: { href: "" } };
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    login.mockImplementation(async () => {
      // The tab must already exist when the request is sent (popup blockers).
      expect(open).toHaveBeenCalledWith("about:blank", "_blank");
      return REQUIRED;
    });
    complete.mockResolvedValueOnce(PENDING).mockResolvedValueOnce(SETTINGS);
    const { result } = renderHook(() => useProviderOAuthFlow("tok"));

    let settled: SettingsPayload | null | undefined;
    act(() => {
      void result.current.login("xai_grok", { preopenWindow: true }).then((value) => {
        settled = value;
      });
    });
    await flush();

    expect(popup.location.href).toBe(REQUIRED.authorization_url);
    expect(result.current.flow).toEqual(REQUIRED);
    expect(settled).toBeUndefined();

    await tick();
    expect(complete).toHaveBeenCalledTimes(1);
    expect(complete).toHaveBeenLastCalledWith("tok", "xai_grok", "flow-1");
    expect(result.current.flow).toEqual(REQUIRED);

    await tick();
    expect(complete).toHaveBeenCalledTimes(2);
    expect(settled).toBe(SETTINGS);
    expect(result.current.flow).toBeNull();

    await tick(5000);
    expect(complete).toHaveBeenCalledTimes(2);
  });

  it("finishes with a pasted authorization code", async () => {
    login.mockResolvedValue(REQUIRED);
    complete.mockImplementation(async (_token, _provider, _flow, code) =>
      code ? SETTINGS : PENDING,
    );
    const { result } = renderHook(() => useProviderOAuthFlow("tok"));

    let settled: SettingsPayload | null | undefined;
    act(() => {
      void result.current.login("xai_grok").then((value) => {
        settled = value;
      });
    });
    await flush();
    act(() => result.current.setCode("  pasted-code  "));
    await act(async () => {
      await result.current.submitCode();
    });

    expect(complete).toHaveBeenCalledWith("tok", "xai_grok", "flow-1", "pasted-code");
    expect(settled).toBe(SETTINGS);
    expect(result.current.flow).toBeNull();
    expect(result.current.code).toBe("");
  });

  it("stops polling and resolves null when cancelled", async () => {
    login.mockResolvedValue(REQUIRED);
    complete.mockResolvedValue(PENDING);
    const { result } = renderHook(() => useProviderOAuthFlow("tok"));

    let settled: SettingsPayload | null | undefined;
    act(() => {
      void result.current.login("xai_grok").then((value) => {
        settled = value;
      });
    });
    await flush();
    await tick();
    act(() => result.current.cancel());
    await flush();

    expect(settled).toBeNull();
    expect(result.current.flow).toBeNull();
    const calls = complete.mock.calls.length;
    await tick(5000);
    expect(complete).toHaveBeenCalledTimes(calls);
  });

  it("rejects and closes the flow when the server reports a failure", async () => {
    login.mockResolvedValue(REQUIRED);
    complete.mockRejectedValue(new Error("xAI sign-in expired. Start again."));
    const { result } = renderHook(() => useProviderOAuthFlow("tok"));

    let failure: Error | undefined;
    act(() => {
      void result.current.login("xai_grok").catch((error: Error) => {
        failure = error;
      });
    });
    await flush();
    await tick();

    expect(failure?.message).toBe("xAI sign-in expired. Start again.");
    expect(result.current.flow).toBeNull();
  });

  it("closes the pre-opened tab when starting sign-in fails", async () => {
    const popup = { closed: false, close: vi.fn(), location: { href: "" } };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    login.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useProviderOAuthFlow("tok"));

    await expect(
      act(async () => {
        await result.current.login("xai_grok", { preopenWindow: true });
      }),
    ).rejects.toThrow("boom");
    expect(popup.close).toHaveBeenCalled();
  });

  it("stops polling when the component unmounts", async () => {
    login.mockResolvedValue(REQUIRED);
    complete.mockResolvedValue(PENDING);
    const { result, unmount } = renderHook(() => useProviderOAuthFlow("tok"));

    act(() => {
      void result.current.login("xai_grok");
    });
    await flush();
    unmount();
    await tick(5000);

    expect(complete).not.toHaveBeenCalled();
  });
});

describe("ProviderOAuthLoginDialog", () => {
  beforeEach(() => {
    login.mockReset();
    complete.mockReset();
  });

  function Harness({ onDone }: { onDone: (payload: SettingsPayload | null) => void }) {
    const oauth = useProviderOAuthFlow("tok");
    return (
      <>
        <button type="button" onClick={() => void oauth.login("xai_grok").then(onDone)}>
          start
        </button>
        <ProviderOAuthLoginDialog oauth={oauth} providerLabel="xAI Grok" />
      </>
    );
  }

  it("shows the sign-in link and completes with a pasted code", async () => {
    login.mockResolvedValue(REQUIRED);
    complete.mockImplementation(async (_token, _provider, _flow, code) =>
      code ? SETTINGS : PENDING,
    );
    const onDone = vi.fn();
    render(<Harness onDone={onDone} />);

    expect(screen.queryByTestId("provider-oauth-dialog")).toBeNull();
    fireEvent.click(screen.getByText("start"));

    const link = await screen.findByRole("link", { name: /sign-in page/i });
    expect(link.getAttribute("href")).toBe(REQUIRED.authorization_url);
    expect(link.getAttribute("rel")).toContain("noopener");

    const finish = screen.getByRole("button", { name: /finish sign-in/i });
    expect(finish).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText(/authorization code/i), {
      target: { value: "pasted-code" },
    });
    await act(async () => {
      fireEvent.click(finish);
    });

    expect(complete).toHaveBeenCalledWith("tok", "xai_grok", "flow-1", "pasted-code");
    expect(onDone).toHaveBeenCalledWith(SETTINGS);
    expect(screen.queryByTestId("provider-oauth-dialog")).toBeNull();
  });

  it("cancels the sign-in from the dialog", async () => {
    login.mockResolvedValue(REQUIRED);
    complete.mockResolvedValue(PENDING);
    const onDone = vi.fn();
    render(<Harness onDone={onDone} />);

    fireEvent.click(screen.getByText("start"));
    fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
    await flush();

    expect(onDone).toHaveBeenCalledWith(null);
    expect(screen.queryByTestId("provider-oauth-dialog")).toBeNull();
  });
});
