import { useCallback, useEffect, useRef, useState } from "react";

import {
  completeProviderOAuth,
  isProviderOAuthAuthorizationRequired,
  isProviderOAuthPending,
  loginProviderOAuth,
} from "@/lib/api";
import type { ProviderOAuthAuthorizationRequired, SettingsPayload } from "@/lib/types";

const POLL_INTERVAL_MS = 1000;

export interface ProviderOAuthLoginOptions {
  /**
   * Open the sign-in tab before the request is sent. Popup blockers only allow
   * `window.open` synchronously inside the click handler, so two-step providers
   * need the tab to exist before the authorization URL is known.
   */
  preopenWindow?: boolean;
}

export interface ProviderOAuthFlowState {
  /** The pending two-step sign-in, or null when none is in progress. */
  flow: ProviderOAuthAuthorizationRequired | null;
  code: string;
  setCode: (value: string) => void;
  /** True while a pasted code is being exchanged. */
  submitting: boolean;
  /**
   * Sign in. Resolves with the refreshed settings, or `null` when the user
   * cancelled. One-step providers resolve as soon as the request returns;
   * two-step providers resolve after the loopback callback or a pasted code.
   */
  login: (provider: string, options?: ProviderOAuthLoginOptions) => Promise<SettingsPayload | null>;
  submitCode: () => Promise<void>;
  cancel: () => void;
}

interface Pending {
  flow: ProviderOAuthAuthorizationRequired;
  resolve: (payload: SettingsPayload | null) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout> | null;
}

/**
 * Shared sign-in logic for OAuth providers, used by Settings and by the
 * onboarding wizard so the two-step flow is implemented exactly once.
 */
export function useProviderOAuthFlow(token: string): ProviderOAuthFlowState {
  const [flow, setFlow] = useState<ProviderOAuthAuthorizationRequired | null>(null);
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const pendingRef = useRef<Pending | null>(null);
  const tokenRef = useRef(token);
  tokenRef.current = token;

  const settle = useCallback((pending: Pending, finish: () => void) => {
    // A late response for a flow that was cancelled or replaced is ignored.
    if (pendingRef.current !== pending) return;
    pendingRef.current = null;
    if (pending.timer !== null) clearTimeout(pending.timer);
    setFlow(null);
    setCode("");
    setSubmitting(false);
    finish();
  }, []);

  const schedulePoll = useCallback(
    (pending: Pending) => {
      pending.timer = setTimeout(() => {
        pending.timer = null;
        if (pendingRef.current !== pending) return;
        completeProviderOAuth(tokenRef.current, pending.flow.provider, pending.flow.flow_id)
          .then((payload) => {
            if (pendingRef.current !== pending) return;
            if (isProviderOAuthPending(payload)) {
              schedulePoll(pending);
              return;
            }
            settle(pending, () => pending.resolve(payload));
          })
          .catch((error: unknown) => {
            settle(pending, () =>
              pending.reject(error instanceof Error ? error : new Error(String(error))),
            );
          });
      }, POLL_INTERVAL_MS);
    },
    [settle],
  );

  const cancel = useCallback(() => {
    const pending = pendingRef.current;
    if (pending) settle(pending, () => pending.resolve(null));
  }, [settle]);

  const login = useCallback(
    async (provider: string, options?: ProviderOAuthLoginOptions) => {
      cancel();
      let popup: Window | null = null;
      if (options?.preopenWindow) {
        try {
          popup = window.open("about:blank", "_blank");
        } catch {
          popup = null;
        }
      }
      let result;
      try {
        result = await loginProviderOAuth(tokenRef.current, provider);
      } catch (error) {
        popup?.close();
        throw error;
      }
      if (!isProviderOAuthAuthorizationRequired(result)) {
        popup?.close();
        return result;
      }
      try {
        // When the tab was blocked or already closed, the dialog still offers the link.
        if (popup && !popup.closed) popup.location.href = result.authorization_url;
      } catch {
        // Ignore: the dialog link is the fallback.
      }
      const required = result;
      return new Promise<SettingsPayload | null>((resolve, reject) => {
        const pending: Pending = { flow: required, resolve, reject, timer: null };
        pendingRef.current = pending;
        setFlow(required);
        setCode("");
        schedulePoll(pending);
      });
    },
    [cancel, schedulePoll],
  );

  const submitCode = useCallback(async () => {
    const pending = pendingRef.current;
    const authorizationCode = code.trim();
    if (!pending || !authorizationCode || submitting) return;
    setSubmitting(true);
    try {
      const payload = await completeProviderOAuth(
        tokenRef.current,
        pending.flow.provider,
        pending.flow.flow_id,
        authorizationCode,
      );
      if (pendingRef.current !== pending) return;
      if (isProviderOAuthPending(payload)) {
        setSubmitting(false);
        return;
      }
      settle(pending, () => pending.resolve(payload));
    } catch (error) {
      // The server drops a flow whose code exchange failed, so this sign-in is over.
      settle(pending, () =>
        pending.reject(error instanceof Error ? error : new Error(String(error))),
      );
    }
  }, [code, settle, submitting]);

  useEffect(
    () => () => {
      const pending = pendingRef.current;
      if (!pending) return;
      pendingRef.current = null;
      if (pending.timer !== null) clearTimeout(pending.timer);
      pending.resolve(null);
    },
    [],
  );

  return { flow, code, setCode, submitting, login, submitCode, cancel };
}
