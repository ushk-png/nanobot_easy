# WebUI

The WebUI is nanobot's browser workbench for persistent chat sessions, visible
agent activity, workspace controls, Connections, Agent Tools, Agent Management,
Skills, settings, and Automations in one place.

The published `nanobot-ai` wheel already includes the WebUI bundle. You only need
the `webui/` source directory when you are changing the frontend itself.

## Open the WebUI

Use the launcher:

```bash
nanobot webui
```

`nanobot webui` creates the config/workspace when needed, checks provider setup,
offers Quick Start when the model provider is not ready, enables the local
WebSocket channel after confirmation, starts the gateway, and opens the browser.
The first-run path binds the WebUI to `127.0.0.1` by default, so it is not
available from other devices on your LAN.

Run it in the background when you do not want to keep a terminal open:

```bash
nanobot webui --background
```

Manage the background gateway with `nanobot gateway status`, `nanobot gateway
logs`, `nanobot gateway restart`, and `nanobot gateway stop`.

Manual config still works. Set `tokenIssueSecret` when you intentionally expose
the WebUI beyond localhost or want a browser password:

```json
{
  "channels": {
    "websocket": {
      "enabled": true,
      "host": "127.0.0.1",
      "tokenIssueSecret": "your-webui-password",
      "websocketRequiresToken": true
    }
  }
}
```

The WebUI is served by the WebSocket channel on port `8765` by default. The
gateway health endpoint, `18790` by default, is not the browser UI.

## What It Is For

| Area | Use it for |
|---|---|
| Chat | Start, switch, search, fork, and delete browser sessions |
| Agent activity | See thinking, tool calls, file activity, command output, and generated artifacts in context |
| Workspace | Pick the project workspace before asking for file or shell work |
| Access | Choose the access mode for local capabilities allowed by your gateway configuration |
| Composer | Send text, images, voice input, slash commands, and `@` mentions for Programs or MCP presets |
| Connections | Enable built-in integrations, manage optional feature dependencies, and review local Programs/MCP presets |
| Tools | Toggle built-in Agent Tools such as web search, file access, command execution, image generation, and external-program execution |
| Agent Management | Create, edit, and delete user-managed subagent profiles from a dedicated screen |
| Skills | Inspect, create, test, register, and lifecycle-manage skills when management is enabled |
| Automations | Review, search, run, pause, edit, and delete scheduled and local-trigger agent turns |
| Settings | Adjust models, providers, image generation, voice, web tools, runtime, and safety options |

## Chat Workspace

The sidebar is the session switcher. A session keeps its own history, title,
workspace metadata, and linked automations. Use a new session when you want a
separate context; use fork when you want to continue from an existing point
without changing the original thread.

The message timeline shows both user-visible replies and agent activity. Long
tool or reasoning sections can be expanded when you need the details.

## Workspace and Access

Use the workspace picker before starting project-specific work. This gives the
agent the right project context for file paths, shell commands, and session
metadata.

The access control in the composer controls the local capability level for the
chat. It does not bypass your gateway, provider, shell sandbox, or operating
system configuration; it only selects among the capabilities that are already
available to this WebUI session.

## Composer

The composer supports plain messages, image attachments, voice input when
transcription is configured, slash commands, and `@` mentions for installed
Programs or MCP presets. The model badge links back to model settings when setup
is incomplete; when the model is already configured, the home composer keeps the
hero cleaner by hiding the connected-model badge.

For image generation, configure an image provider first and then use the WebUI
image mode from the composer. See [`image-generation.md`](./image-generation.md)
for provider setup and output behavior.

## Connections and Programs

Open Connections from the sidebar or settings navigation to manage integrations
that nanobot can call from a chat. Nanobot features can enable built-in channels
and optional capabilities such as `bedrock` or `documents`. The Programs section
shows local CLI App adapters that nanobot can run on your machine; these are
external App Tools, not the same thing as built-in Agent Tools. MCP presets add
predefined MCP server configurations.

Enabling a Nanobot feature may install Python packages into the environment
running nanobot. By default, the WebUI can install missing packages only when
you open it on the same machine as nanobot. If you open the WebUI from another
device, a domain name, a tunnel, or a reverse proxy, package install is blocked
unless you explicitly allow it with `tools.webuiAllowRemotePackageInstall`.

Optional feature installs use your existing pip download settings. If PyPI is
slow or unavailable from your network, configure pip or set `PIP_INDEX_URL`
before starting nanobot.

Some MCP presets connect to hosted keyless endpoints. For example, the Firecrawl
preset uses Firecrawl's hosted MCP endpoint for search, scrape, crawl, and
extraction tools without requiring an API key. This does not replace nanobot's
built-in web search provider; mention the Firecrawl MCP preset with `@` when a
turn needs Firecrawl's richer web data tools.

After a Program or MCP preset is available, mention it from the composer with
`@` to attach that capability to the next message.

## Agent Tools

Open Tools from the sidebar to control built-in Agent Tools. This screen is for
runtime capabilities exposed to nanobot itself, such as web search/fetch, file
access, shell command execution, image generation, and permission to run external
Programs. It is separate from Connections: Connections manages integrations and
installed Programs; Tools controls whether the agent may use those capability
classes during a turn.

Some toggles affect gateway/runtime behavior and may require restart. The WebUI
shows the restart requirement when a setting cannot take effect immediately.

## Agent Management

Open Agent Management from the sidebar to manage user-created subagent profiles.
This is a real settings screen, not a shortcut that only pre-fills a chat
message. Use it to create or rename an agent, choose a display icon, describe
when that agent should be used, and delete profiles after confirmation.

Student-mode seeded profiles such as `study-coach` and `review-teacher` are not
shown here because they are managed by the mode setup flow. User-created agents
are backed by nanobot's subagent profile configuration; display icons are stored
as workspace-local UI metadata.

## Skills

The Skills view shows the skill instructions available to the agent, including
built-in skills, system skills, and workspace-provided skills. It is a catalog
of loadable instructions, not a list of instructions injected into every model
request. Always-on skills may be preloaded, but most skills are selected only
when the agent finds them relevant, including through `skill_search`.

The `source` value tells you where a skill came from:

- `builtin`: packaged task skills under `nanobot/skills`;
- `system`: orchestration and skill-management skills under
  `nanobot/skills-system`;
- `workspace`: custom skills under `<workspace>/skills`.

Check this view when you want to know whether nanobot already has a focused
workflow for a task before you ask it to perform that task. If a skill appears
as unavailable, open its detail sheet to see missing commands or environment
variables.

When `tools.webuiSkillManagement.enabled` is true, the Skills view becomes a
registry-backed management console. It uses the same skill store service as the
`nanobot skill ...` CLI, so system skills remain write-protected and lifecycle
rules are enforced server-side instead of only by the UI.

The management layout is a master-detail view:

- the top draft inbox shows drafts that are composing, ready, failed, or waiting
  for a decision;
- the left list filters registry skills by status and search text;
- the right detail panel shows markdown, metadata, recent routing traces,
  routing-test results, edit controls, and the next valid lifecycle actions.

Allowed lifecycle actions are shown as concrete buttons for the current status:
drafts can be registered or rejected, candidates can be promoted to verified,
and verified skills can be deprecated. The service rejects invalid transitions
and all writes to system skills.

Use **New skill** to open the creation wizard. The wizard collects the proposed
name, triggers, category, risk level, execution requirement, and method draft,
then starts Composer in the background. The server returns a draft id, the UI
polls for progress, and the draft stays visible in the inbox if you leave the
screen. When Composer finishes, review the generated `SKILL.md`, review report,
and routing cases before selecting **Register**. Registration writes the skill
under `<workspace>/skills/<name>/`, moves routing cases into
`routing_cases.json`, reindexes, and exposes the skill as `candidate`.

Red flags expand the one-click registration flow into explicit review. By
default this happens when fewer than 7 of 10 routing cases pass, security risk
is at least `medium`, or duplicate score is at least `0.8`. Overrides require a
reason. Security risk at `high` or above is blocked and cannot be overridden.

Skill edits are assessed as Minor or Major before saving. Minor edits keep the
current lifecycle status. Major edits, such as method or tool-use changes,
require confirmation and return verified skills to `candidate` for revalidation.
See [`configuration.md#webui-skill-management`](./configuration.md#webui-skill-management)
for the management capability flag and red-flag thresholds.

If external tool skills have written `<workspace>/tools/installed.md`, the
Connections view can show those local Programs as a read-only installed-tools
ledger. The Skills view may also show the same summary. The ledger shows the tool
name, description, install date, version, last recorded status, and last check
time, but it does not provide delete, update, start, or stop buttons. Ask in chat
for those actions so the normal setup/usage skill routing and confirmation rules
apply.

## Automations

Automations are agent turns that run later in a linked chat/session. They should
be created from the chat, channel, or session where they are supposed to run so
nanobot keeps the correct target context. When an automation runs, it normally
delivers the result back to that linked chat.

There are two user-facing automation types:

- Scheduled automations, created by the agent's cron tool, run at a time,
  interval, or cron expression.
- Local triggers, created with `/trigger <name>`, run when you call a local
  command such as `nanobot trigger trg_8K4P2Q9X "Review PR #4502"`.

If a GitHub webhook, CI system, or another service should wake nanobot up, keep
that webhook/service outside nanobot and have it call the trigger command with
the final message.

Trigger deliveries use the same workspace as the gateway. They survive gateway
restarts and are requeued if the process exits before the linked turn completes.
If the linked session is already running a turn, the local trigger waits until
that session is idle instead of being injected into the active turn. This is an
at-least-once local queue, so repeated delivery is possible after an interrupted
process. A delivered trigger is recorded as an automation turn in the linked
session; if the agent receives it but the turn fails, Automations marks the run
failed instead of retrying indefinitely.

For recurring background checks that should stay quiet unless there is something
useful to report, use the protected heartbeat job by editing `HEARTBEAT.md`
instead of creating a chat automation.

Use the Automations view to:

- Filter by all, active, paused, needs-attention, or system jobs.
- Search by task name, message, trigger command, linked chat, schedule, or status.
- Sort by next run, last run, updated time, or name.
- Run scheduled automations now.
- Pause or resume, rename, or delete user-created automations.
- Copy the CLI command for local triggers.
- Inspect protected system automations without changing them.

Search accepts plain text and field filters such as `name:backup`,
`chat:WeChat`, `schedule:09:30`, `cron:"0 23 * * *"`, `trigger`, and
`status:paused`.

An automation without a linked chat cannot be enabled or run from the WebUI,
because nanobot would not know where to deliver the scheduled turn. Recreate it
from the target chat or channel so the automation has complete context.

Local triggers do not have a WebUI "Run now" action because each run needs a
message. Use the copied `nanobot trigger ...` command and replace `"message"`
with the content that should be delivered.

## Settings

Settings is the control surface for the browser session and gateway-backed
runtime configuration. Use it to review or adjust model presets, provider
visibility, image generation, voice transcription, Agent Tools, Connections,
Automations, Skills, Agent Management, runtime identity, and advanced safety
controls.

Some settings take effect immediately. Runtime settings that affect the gateway
or agent process may require a restart; the WebUI shows that requirement next to
the relevant control.

## LAN Access

To open the WebUI from another device on the same network, bind the WebSocket
channel to all interfaces and set a token or token issue secret:

```json
{
  "channels": {
    "websocket": {
      "host": "0.0.0.0",
      "port": 8765,
      "tokenIssueSecret": "your-secret-here"
    }
  }
}
```

The gateway refuses to start with `host` set to `"0.0.0.0"` unless `token` or
`tokenIssueSecret` is configured. After the gateway starts, open
`http://<your-ip>:8765` from the other device and enter the secret in the login
form.

Remote WebUI clients can view Connections and toggle already-installed features
with a valid token, but they cannot install missing Python packages by default.
To allow trusted remote admins to install optional feature dependencies from the
WebUI, opt in explicitly:

```json
{
  "tools": {
    "webuiAllowRemotePackageInstall": true
  }
}
```

Use this only for a private deployment where every authenticated WebUI user is
trusted to change the Python environment that nanobot runs in. If you publish
the WebUI through Nginx, Caddy, Cloudflare Tunnel, or a similar service, treat it
as remote access and leave package installs disabled unless that is intentional.

Optional feature installs use pip's configured package index, including
`PIP_INDEX_URL`.

Leave remote package installs disabled when the WebUI is exposed beyond a
private, trusted network.

## Troubleshooting

If the page does not open, check these in order:

1. `nanobot agent -m "Hello!"` works in the same Python environment.
2. `~/.nanobot/config.json` does not explicitly set `channels.websocket.enabled` to `false`.
3. `nanobot gateway` is still running.
4. You are opening port `8765`, not the gateway health port.
5. LAN access uses `host: "0.0.0.0"` and a token or token issue secret.

For detailed diagnostics, see
[`troubleshooting.md#webui-problems`](./troubleshooting.md#webui-problems).
For frontend development, see [`../webui/README.md`](../webui/README.md).
