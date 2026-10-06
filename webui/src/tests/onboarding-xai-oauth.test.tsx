import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OnboardingWizardPage } from "@/components/onboarding/OnboardingWizardPage";
import {
  completeProviderOAuth,
  fetchNanobotFeatures,
  fetchProviderModels,
  fetchSettings,
  loginProviderOAuth,
  updateModelConfiguration,
  updateSettings,
} from "@/lib/api";
import type { SettingsPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchSettings: vi.fn(),
    fetchNanobotFeatures: vi.fn(),
    fetchProviderModels: vi.fn(),
    loginProviderOAuth: vi.fn(),
    completeProviderOAuth: vi.fn(),
    updateSettings: vi.fn(),
    updateModelConfiguration: vi.fn(),
  };
});

function settings(configured: boolean, model: string): SettingsPayload {
  return {
    agent: { model, provider: "xai_grok", resolved_provider: "xai_grok" },
    model_presets: [{ name: "default", label: "Default", is_default: true }],
    providers: [
      {
        name: "xai_grok",
        label: "xAI Grok",
        configured,
        auth_type: "oauth",
        api_key_required: false,
        oauth_account: configured ? "me@example.com" : null,
        oauth_login_supported: true,
        oauth_login_mode: "authorization_url",
        oauth_default_model: "xai-grok/grok-4.5",
        oauth_default_context_window_tokens: 500000,
      },
    ],
  } as unknown as SettingsPayload;
}

const REQUIRED = {
  status: "authorization_required" as const,
  provider: "xai_grok",
  flow_id: "flow-1",
  authorization_url: "https://auth.x.ai/oauth2/auth?state=abc",
  expires_in: 600,
};

describe("onboarding wizard: xAI Grok sign-in", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSettings).mockResolvedValue(settings(false, "anthropic/claude-opus-4-5"));
    vi.mocked(fetchNanobotFeatures).mockResolvedValue({ features: [] } as never);
    vi.mocked(fetchProviderModels).mockResolvedValue({ models: [], status: "unsupported" } as never);
    vi.mocked(loginProviderOAuth).mockResolvedValue(REQUIRED);
    vi.mocked(completeProviderOAuth).mockImplementation(async (_t, _p, _f, code) =>
      code
        ? settings(true, "anthropic/claude-opus-4-5")
        : { status: "pending", provider: "xai_grok", flow_id: "flow-1" },
    );
    vi.mocked(updateSettings).mockResolvedValue(settings(true, "xai-grok/grok-4.5"));
    vi.spyOn(window, "open").mockReturnValue(null);
  });

  it("signs in through the dialog and selects the Grok model on the default configuration", async () => {
    const { container } = render(
      <ClientProvider client={{} as never} token="tok">
        <OnboardingWizardPage onDone={() => {}} />
      </ClientProvider>,
    );

    const connect = await waitFor(() => {
      const button = container.querySelector<HTMLButtonElement>(".ne-oauth-btn");
      if (!button) throw new Error("connect button not rendered yet");
      return button;
    });
    fireEvent.click(connect);

    // The sign-in tab is requested inside the click, before the request resolves.
    expect(window.open).toHaveBeenCalledWith("about:blank", "_blank");
    const codeInput = await screen.findByLabelText(/authorization code/i);
    fireEvent.change(codeInput, { target: { value: "pasted-code" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /finish sign-in/i }));
    });

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith("tok", {
        provider: "xai_grok",
        model: "xai-grok/grok-4.5",
        contextWindowTokens: 500000,
      }),
    );
    expect(completeProviderOAuth).toHaveBeenCalledWith("tok", "xai_grok", "flow-1", "pasted-code");
    // The model-configurations endpoint rejects the built-in "default" name.
    expect(updateModelConfiguration).not.toHaveBeenCalled();
    expect(screen.queryByTestId("provider-oauth-dialog")).toBeNull();
  });

  it("leaves the configuration untouched when the sign-in dialog is cancelled", async () => {
    const { container } = render(
      <ClientProvider client={{} as never} token="tok">
        <OnboardingWizardPage onDone={() => {}} />
      </ClientProvider>,
    );

    const connect = await waitFor(() => {
      const button = container.querySelector<HTMLButtonElement>(".ne-oauth-btn");
      if (!button) throw new Error("connect button not rendered yet");
      return button;
    });
    fireEvent.click(connect);
    fireEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));

    await waitFor(() => expect(connect.disabled).toBe(false));
    expect(updateSettings).not.toHaveBeenCalled();
    expect(updateModelConfiguration).not.toHaveBeenCalled();
  });
});
