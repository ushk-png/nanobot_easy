import { ExternalLink, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { ProviderOAuthFlowState } from "@/hooks/useProviderOAuthFlow";

/**
 * Shown while a two-step OAuth sign-in (xAI Grok) is pending. Sign-in normally
 * finishes by itself through the loopback callback; the code field is the path
 * for browsers on a different machine than the gateway.
 */
export function ProviderOAuthLoginDialog({
  oauth,
  providerLabel,
}: {
  oauth: ProviderOAuthFlowState;
  providerLabel?: string;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const { flow } = oauth;
  const label = providerLabel || "xAI";

  return (
    <Dialog
      open={flow !== null}
      onOpenChange={(open) => {
        if (!open) oauth.cancel();
      }}
    >
      <DialogContent data-testid="provider-oauth-dialog">
        <DialogHeader>
          <DialogTitle>
            {tx("settings.oauth.dialogTitle", "Sign in to {{provider}}", { provider: label })}
          </DialogTitle>
          <DialogDescription>
            {tx(
              "settings.oauth.localCodeHelp",
              "Complete sign-in in your browser. Nanobot usually finishes automatically; if it does not, paste the authorization code below.",
            )}
          </DialogDescription>
        </DialogHeader>

        {flow ? (
          <div className="grid gap-3">
            <a
              href={flow.authorization_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex w-fit items-center gap-1.5 text-sm font-medium text-primary underline-offset-4 hover:underline"
            >
              <ExternalLink className="h-3.5 w-3.5" aria-hidden />
              {tx("settings.oauth.openSignInPage", "Open the sign-in page")}
            </a>
            <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              {tx("settings.oauth.waitingForSignIn", "Waiting for sign-in to finish...")}
            </p>
            <form
              className="grid gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                void oauth.submitCode();
              }}
            >
              <label className="text-sm font-medium" htmlFor="provider-oauth-code">
                {tx("settings.oauth.authorizationCode", "Authorization code")}
              </label>
              <Input
                id="provider-oauth-code"
                autoComplete="off"
                spellCheck={false}
                value={oauth.code}
                onChange={(event) => oauth.setCode(event.target.value)}
                placeholder={tx(
                  "settings.oauth.authorizationCodePlaceholder",
                  "Paste the code or the final callback URL",
                )}
              />
              <p className="text-xs text-muted-foreground">
                {tx(
                  "settings.oauth.remoteCodeHelp",
                  "Using nanobot from another device? After signing in, paste the authorization code shown by xAI here.",
                )}
              </p>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={oauth.cancel}>
                  {tx("settings.oauth.cancelSignIn", "Cancel")}
                </Button>
                <Button type="submit" disabled={!oauth.code.trim() || oauth.submitting}>
                  {oauth.submitting
                    ? tx("settings.oauth.signingIn", "Signing in...")
                    : tx("settings.oauth.finishSignIn", "Finish sign-in")}
                </Button>
              </DialogFooter>
            </form>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
