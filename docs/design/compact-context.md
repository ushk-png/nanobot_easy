# Compact context for ordinary answers

The legacy path sends execution tutorials, always-loaded built-in skill bodies,
and tool schemas even when the model only needs to answer `2+2`.
`agents.defaults.compactContext` selects the alternative described here. It is on by default
since 2026-11-22 (`13b048de2`); the legacy path remains available with `false`:

```json
{
  "agents": {
    "defaults": {
      "compactContext": true
    }
  }
}
```

Restart the selected gateway after changing this setting. The default is `true`.
An installation that wants the previous behavior sets `"compactContext": false`.

Ordinary turns start with a compact system prompt and one `activate_tools` tool.
The model answers self-contained requests directly. For tool work it calls
`activate_tools`, which restores the original system prompt at system priority
and exposes selected original tool definitions. Its optional `tool_names` list
contains the available tool-name catalog; the model should load only the tools
needed now, for example `read_file` and `write_file` for save-and-verify. The
activation tool stays available to add more tools during this turn. Selections
are additive and cannot remove a tool already loaded. Omitting `tool_names`
loads the full original registry for compatibility or uncertain discovery.
Activation alone never counts as execution.
Tool-result truncation cannot remove the restored instructions. The names a turn
activated are saved in the session metadata and seed the next turn's tool set, so the
system message is composed before the first provider call and does not change mid-turn
(a mid-turn change would break the provider's prompt cache). A turn that activated
nothing leaves the earlier seed untouched, and an explicit `tool_names` call narrows
even a session seeded with the full registry. Full instructions are not copied into
tool history.
Unloaded tools cannot execute through this view; calls must first activate them.
Selected schemas and execution paths still come from the original registry,
including intent validation, workspace restrictions and permission checks.
Lightweight mode keeps its independent `update_task` control available.

The compact prompt preserves bootstrap files (`AGENTS.md`, `SOUL.md`, `USER.md`),
long-term memory, archived summaries, configured hot skills, and non-built-in
always-on skills when always-skill preloading is enabled. Disabling
preloadAlwaysSkills still defers those bodies, while explicitly configured hot
skills stay loaded. The original dialogue and runtime context remain intact.
Permission gates and untrusted-input rules remain present from the first call.
Built-in execution workflows are deferred, not deleted. Existing intent metadata,
workspace checks, tool execution and verification are unchanged.

This mode is bypassed for ephemeral/subagent turns and the
context-selection path. Lightweight task-note mode can be combined with it: simple
answers remain compact, and work requests retain update_task evidence checks.

## Tradeoffs and verification

An ordinary answer still uses one model call. Execution usually needs one extra
activation call, so this is most useful for gateways with frequent ordinary chat;
it is not a universal token reduction for tool-heavy work. Long histories and
large user policy files can still dominate input, and are deliberately preserved.

On 2026-11-16, paired isolated tests using `openai/gpt-6.1-sol` / medium and the
same read/write registry measured:

| Request | Legacy input tokens | Compact input tokens | Reduction |
| --- | ---: | ---: | ---: |
| 2+2 | 7,377 | 2,238 | 69.7% |
| Explain cache in two sentences | 7,383 | 2,244 | 69.6% |

Both retained a one-call answer. A contextual rewrite preserved its one-sentence
constraint. Read and write-then-read cases executed real local tools in isolated
workspaces. These are individual observations, not a guarantee for every model
or workload. Operating gateway configuration was not changed by this evaluation.

Reproduce in a fresh output directory:

```bash
pytest tests/agent/test_compact_context.py tests/agent/test_context_builder.py tests/agent/test_loop_runner_integration.py tests/agent/test_task_note.py -q
python -m scripts.improvement_eval.live_tool_context --config .local/config.json \
  --out .local/evaluations/compact-new
```

The live evaluator uses the configured provider/model and incurs model usage.

## Tool-heavy context profile (2026-11-17)

An offline profile of the current operating policy and core registry estimated
17,929 system tokens and 11,347 tokens across 28 core tool definitions with
cl100k_base. Of the system prompt, long-term memory contributed 7,664 tokens and
bootstrap instructions 3,047. These local estimates are not vendor usage and
exclude connected MCP schemas. User policy and memory were preserved.

Paired Grok 4.7 file evaluations copied operating bootstrap/memory/skill files to
isolated workspaces, used the same 28 core schemas and executed only real local
read/write tools (unrelated tools were refusing schema fixtures; MCP was omitted):

| Request | Full tool activation input | Selected activation input | Model calls |
| --- | ---: | ---: | ---: |
| 2+2 | 15,890 | 16,176 | 1 / 1 |
| Save and reread a.md | 159,770 | 118,377 | 5 / 5 |

The file request used `read_file` and `write_file`, with `activate_tools` and
`update_task` still available. Post-activation core schemas dropped from 29 to
4 including controls, with a local schema estimate of 12,108 to 1,670 tokens.
Actual total file input fell 25.9%; an earlier trial measured 26.5%. Full system
guidance is still repeated. The initial catalog added 286 vendor input tokens
(1.8%) to the ordinary question. These are individual observations, not guaranteed
reductions or measurements of a running production gateway. Instruction files in
the baseline and both revised evaluations were verified byte-for-byte identical;
the final run reused the baseline's frozen policy snapshot.

Reproduce with the operating provider and a fresh output directory:

```bash
python -m scripts.improvement_eval.live_tool_context --config .local/config.json \
  --out .local/evaluations/tool-context-new
```

The output directory contains private instruction copies and is created with
owner-only access. Journals record component counts, tool names and actual model
usage rather than full input payloads. Do not share policy copies without review.
