This file provides guidance to AI coding agents working with this repository.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket, Mattermost). `manager.py` discovers and coordinates them. Channels are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (long-goal, cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

### `nanobot skill …` workspace auto-discovery

Every `nanobot skill …` subcommand (list, stats, reindex, audit, approve,
promote, deprecate, test-routing, …) resolves the runtime workspace in this
order:

1. `--workspace / -w` explicit CLI arg
2. `NANOBOT_WORKSPACE` environment variable
3. Auto-discovery: walk up from the current directory (max 6 levels) and
   accept the first path whose `.skillstore/skillstore.db` exists. Candidates
   at each level: `./`, `./.local/workspace`, `./.nanobot/workspace`,
   `./workspace`.
4. Default: `~/.nanobot/workspace` (nanobot's global default)

The CLI **always** prints the resolved path to stderr on the first line, e.g.
`nanobot skill: workspace=/…/nanobot_skill/.local/workspace (source=discovered)`.
If auto-discovery falls back to the default while the current dir looks like a
project (has `pyproject.toml`, `.git`, or `package.json`) an additional warning
prompts the user to set `NANOBOT_WORKSPACE` or pass `--workspace`.

**Debugging skill-approval mismatches:** always read the stderr `workspace=…`
line to confirm the CLI is talking to the same DB the runtime writes to. A
bare `nanobot skill list` from outside a project targets the default DB and
will not see drafts pending in a project workspace.

## Staged Workflow Execution Guardrails

When a user defines or uses a staged/manual-approval workflow, execute exactly
what has been approved and stop before the next stage.

- Treat phrases such as `Stage N 실행해줘`, `Stage N 진행해줘`, `N단계 해줘`,
  or `계속 진행해줘` as explicit approval for that stage when the stage is
  already known from the conversation.
- Do not ask for confirmation again for an already-approved stage unless a
  required input is missing or the requested action is destructive beyond that
  stage's stated scope.
- Boundary statements are not completion. After stating the boundary, perform
  the approved stage's actual deliverables before ending the turn.
- Do not confuse bookkeeping, routing checks, plans, or tool setup with the
  requested work. A stage is complete only when its promised output has been
  produced or its approved actions have been executed and verified.
- Before replying, check that the approved stage's deliverables are included.
  If not, continue working rather than asking whether to continue.
- Lock only future stages (`N+1` and later). Do not apply the approval gate to
  the current stage after the user has approved it.
- End staged responses with the next-stage boundary, e.g.
  `다음 단계(N+1)는 승인 전까지 실행하지 않습니다.`

## Contribution Flow

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.
