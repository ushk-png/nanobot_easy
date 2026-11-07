"""Run Genie filesystem scans in a killable child without changing search semantics."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from nanobot.agent.tools.base import ToolResult


async def isolated_search(tool, params):
    # Resolve in the parent using the active workspace capability before crossing processes.
    target = tool._resolve_read(params.get("path") or ".")
    payload = {"name": tool.name, "workspace": str(tool._display_workspace() or target.parent),
               "root": str(target if target.is_dir() else target.parent),
               "params": {**params, "path": str(target)}}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "nanobot.agent.tools.search_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
    )
    try:
        output, error = await process.communicate(json.dumps(payload).encode())
        if process.returncode:
            return ToolResult.error("Search worker failed: " + error.decode(errors="replace")[-500:])
        result = json.loads(output)
        return ToolResult(result["content"], is_error=result["is_error"])
    finally:
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def main():
    from nanobot.agent.tools.search import FindFilesTool, GrepTool

    payload = json.loads(sys.stdin.read())
    cls = {"find_files": FindFilesTool, "grep": GrepTool}[payload["name"]]
    tool = cls(workspace=Path(payload["workspace"]), allowed_dir=Path(payload["root"]))
    result = await tool.execute(**payload["params"])
    sys.stdout.write(json.dumps({"content": str(result),
                                "is_error": isinstance(result, ToolResult) and result.is_error}))


if __name__ == "__main__":
    asyncio.run(main())
