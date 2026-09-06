---
name: relay-setup
description: >
  Issue a PSK for an external, executable tool (e.g. a coding CLI like Claude
  Code) that needs to call an LLM through nanobot's built-in `/v1` relay
  listener, instead of handing the tool a real provider API key or OAuth
  token. Only for tools that run their own LLM calls and need a base
  URL/token pair. Not for connecting an MCP server (that's the Apps/Connections
  catalog in the WebUI), not for installing a CLI app, and not for normal
  provider/API-key setup inside nanobot itself.
metadata:
  nanobot:
    id: builtin-relay-setup
    version: 1.0.0
    status: candidate
    category: external.tool
    risk_level: high
    requires_exec: true
    external_tool: true
    install_sources:
      - https://github.com/HKUDS/nanobot
    required_tools:
      - exec
      - read_file
      - edit_file
    triggers:
      - 클로드 코드를 도구로 연결해줘
      - 외부 도구에 LLM 붙여줘
      - relay 키 발급해줘
      - 이 프로그램이 LLM을 쓰게 해줘
      - connect this CLI tool to an LLM through nanobot
      - issue a relay key for an external tool
      - set up the LLM relay for my coding tool
---

# Relay Setup

One-time setup for connecting an external, executable tool to an LLM through
nanobot's PSK-authenticated relay listener (`nanobot/api/relay.py`), so the
tool never sees a real provider API key or OAuth token.

## When To Use

- The user wants an external tool that makes its own LLM calls (a coding CLI,
  a script, a third-party agent) to use nanobot's configured provider as its
  backend.
- The user asks to "connect", "hook up", or "issue a key for" a specific
  external program to an LLM via nanobot.

## When Not To Use

- **MCP server connections** go through the WebUI's Apps/Connections
  catalog, not this skill. If the user wants to add an MCP server, point them
  there instead of issuing a relay key.
- **Installing a CLI app** is a separate install flow. Relay setup only
  applies once a tool already exists and needs an LLM backend.
- Do not use for configuring nanobot's own provider/API key (that's normal
  provider setup, not relay).
- Do not use for one-off questions about what relay is or how it works with
  no intent to actually issue a key.

## Install

### Step 0: Find the config and workspace of the instance you're running in -- do this first

`nanobot relay` does **not** auto-discover the running instance the way
`nanobot skill` does. Every `nanobot relay`/`nanobot restart` call below
needs explicit `--config`/`--workspace` flags, or it silently falls back to
the unrelated global default at `~/.nanobot/`, issues a token against a
gateway that isn't the one you're running in, and reports a base URL that
nothing is listening on. There is no environment variable or runtime
introspection that exposes the active config path to you, so resolve it
concretely before running anything:

1. Run `pwd` via `exec`. Your `exec` calls default to this agent's own
   workspace root as their working directory, so this should print the
   workspace path.
2. By convention, the config file is the sibling `config.json` one
   directory above the workspace (e.g. workspace `.local/workspace` ->
   config `.local/config.json`; workspace `~/.nanobot/workspace` -> config
   `~/.nanobot/config.json`). Use `read_file` to check for it at that
   relative location.
3. If that file isn't there, **stop and ask the user for the config path**
   instead of guessing or falling back to a default -- targeting the wrong
   instance's config is worse than pausing.
4. Once resolved, pass both values explicitly on every command in this
   skill: `--config <resolved-config-path> --workspace <resolved-workspace-path>`
   (the examples below omit them for readability, but never omit them in
   the actual commands you run).

### Step 1: Enable relay if it isn't already

Relay requests never reach the running gateway unless `relay.enabled` is
`true` in the config you resolved above. This defaults to `false`, so on a
fresh install the listener is not running at all.

1. Use `read_file` to check that config file's `relay` section.
2. If `relay.enabled` is missing or `false`:
   - Tell the user relay needs to be turned on and the gateway restarted
     before any key will actually work, e.g.:
     > "Relay가 아직 꺼져 있어요. 설정 파일에 `relay.enabled: true`를 추가하고
     > 게이트웨이를 재시작해야 실제로 연결이 됩니다. 진행할까요?"
   - Do not issue a key yet. Get explicit approval first.
   - On approval, use `edit_file` to set `"enabled": true` in that config's
     `relay` object (keep existing `host`/`port` unless the user asked to
     change them -- defaults are `127.0.0.1:8910`), then run
     `nanobot restart --config <resolved-config-path> --workspace <resolved-workspace-path>`
     so the gateway picks up the new config and starts the relay listener.
3. If `relay.enabled` is already `true`, skip straight to issuing the key.

### Step 2: Issue the key

Issuing a PSK creates a credential. Get explicit user approval before running
this, the same way `yq-setup` confirms before installing anything.

```bash
nanobot relay issue <client-id> --preset <preset> --tool-name "<사람이 읽는 이름>" \
  --config <resolved-config-path> --workspace <resolved-workspace-path>
```

- `<client-id>`: lowercase, hyphenated (e.g. `claude-code`). Ask the user if
  it's not obvious from context.
- `--preset`: the model preset this tool should be bound to. Use `default` if
  the user has no preference.
- `--tool-name`: a human-readable label for `nanobot relay list`, e.g.
  `"Claude Code"`.
- `--write-env` is on by default and writes
  `.secrets/relay/<client-id>.env` for you. Do not write that file yourself
  -- that would duplicate what the CLI already does.
- Only pass `--replace` if the user explicitly asks to reissue/replace an
  existing key for that client id. Without it, issuing against an existing
  client id fails.

### Handling the token -- read this before running the command

`nanobot relay issue` prints the raw token (`nbrelay_...`) exactly once, and
nanobot does not store it in plaintext afterward. This changes how you must
respond:

- **Never echo the raw token back into the conversation.** After running the
  command, tell the user the token was written to
  `.secrets/relay/<client-id>.env` and point them at that path -- do not
  paste the token value itself into your reply.
- Do not copy the token into memory, notes, or any other file. `--write-env`
  already wrote the one place it belongs.
- If a command's output containing the raw token would otherwise appear in
  your response, summarize the outcome instead of quoting that line.

## Verify

```bash
nanobot relay test <client-id> --config <resolved-config-path> --workspace <resolved-workspace-path>
```

reports the client's status and the *actual configured* base URL/model for
this instance -- use that base URL for the checks below rather than
assuming the `127.0.0.1:8910` default, since the user may have changed
`relay.host`/`relay.port`. Note that `relay test` itself does **not** make a
live network call -- it only reads config and registry state. For a real
connectivity check, using the base URL `relay test` just reported:

```bash
curl -s <base-url-without-/v1>/health
```

confirms the listener itself is up, and

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $(grep NANOBOT_RELAY_API_KEY .secrets/relay/<client-id>.env | cut -d= -f2)" \
  <base-url>/models
```

A `401` means the token itself was rejected -- stop and report that before
anything else. Any other code (`200`, or a `5xx` from a provider-side
problem such as a missing/invalid provider API key in nanobot's own config)
means the token was accepted; a `5xx` here is a separate, provider-config
issue for the user to fix in nanobot itself, not a relay-setup failure. If
the health check fails at all, stop and report it -- do not tell the user
setup succeeded.

Once verified, tell the user what to put into the external tool's own
configuration:

- Base URL: exactly what `relay test` reported for this instance
- Model: the model resolved from the preset (shown in the `issue`/`test`
  output)
- Token: from `.secrets/relay/<client-id>.env` -- point at the file, don't
  quote the value

If the user named a specific tool, map these to that tool's own env var
convention (e.g. many OpenAI-compatible CLIs use `OPENAI_BASE_URL` /
`OPENAI_API_KEY`). If you don't know the tool's convention, ask or say so
rather than guessing.

## Uninstall

```bash
nanobot relay revoke <client-id> --config <resolved-config-path> --workspace <resolved-workspace-path>
```

then delete `.secrets/relay/<client-id>.env`. Revoking invalidates the key
immediately -- any request using the old token starts failing right away.

```bash
nanobot relay list --config <resolved-config-path> --workspace <resolved-workspace-path>
```

already shows every issued client (id, tool name, preset, status, key id,
last used) without exposing secrets. Don't keep a separate ledger of relay
clients in `workspace/tools/installed.md` -- that would just drift out of
sync with the real registry.

## Failure Rules

- If you cannot resolve the config path in Step 0, stop and ask the user for
  it. Never run a `nanobot relay`/`nanobot restart` command without explicit
  `--config`/`--workspace` -- falling back to CLI defaults silently targets
  a different, unrelated instance.
- If the user has not explicitly approved enabling relay or issuing a key,
  stop and ask.
- If `nanobot relay issue`/`rotate` fails (e.g. client id already exists
  without `--replace`), report the exact error and do not retry with
  `--replace` unless the user confirms that's what they want.
- If the `curl` verification step fails, report which check failed (listener
  unreachable vs. token rejected) and stop before telling the user the tool
  is ready to use.
