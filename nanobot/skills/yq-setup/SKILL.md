---
name: yq-setup
description: >
  One-time setup for the external yq YAML/XML/TOML query tool in the workspace.
  Use only when the user explicitly asks to install or configure yq for nanobot.
  Not for normal YAML questions or already-installed yq usage.
metadata:
  nanobot:
    id: builtin-yq-setup
    version: 1.0.0
    status: candidate
    category: external.tool
    risk_level: high
    requires_exec: true
    external_tool: true
    install_sources:
      - https://pypi.org/project/yq/
      - https://github.com/kislyuk/yq
    required_tools:
      - exec
      - write_file
    triggers:
      - set up yq
      - install yq
      - setup yq
      - configure yq for nanobot
      - add a yaml query tool
---

# yq Setup

## When To Use

- The user explicitly asks to install, configure, or prepare yq for this
  workspace.
- `yq-usage` checked for yq and found it missing.

## When Not To Use

- Do not use for one-off YAML explanation or editing tasks.
- Do not use if a working yq is already available for the requested task; use
  `yq-usage` instead.

## Install

1. Confirm the user wants to install an external tool into the workspace.
2. Create an isolated directory under `workspace/tools/yq/`.
3. Create a Python virtual environment at `workspace/tools/yq/.venv`.
4. Install yq only inside that virtual environment:

```bash
python -m venv workspace/tools/yq/.venv
workspace/tools/yq/.venv/bin/python -m pip install --upgrade pip
workspace/tools/yq/.venv/bin/python -m pip install yq
```

5. Do not write outside `workspace/tools/yq/`.

## Verify

Run:

```bash
workspace/tools/yq/.venv/bin/yq --version
workspace/tools/yq/.venv/bin/yq --help
```

If either command fails, report the error and stop. Do not continue to usage
steps until the tool is verified.

Record the successful install in `workspace/tools/installed.md`:

```text
| yq | <version> | workspace/tools/yq/.venv/bin/yq | <YYYY-MM-DD> | https://pypi.org/project/yq/ |
```

Create the file with a small table header if it does not exist.

## Uninstall

1. Delete `workspace/tools/yq/`.
2. Remove the yq row from `workspace/tools/installed.md`.
3. Verify removal by checking that `workspace/tools/yq/.venv/bin/yq` no longer
   exists.

## Failure Rules

- If the user has not explicitly approved installation, stop and ask.
- If network access or package installation fails, report the failing command and
  leave any partial files under `workspace/tools/yq/` for user inspection.
- If the installed version differs from the expected PyPI yq package, report the
  version and source.
