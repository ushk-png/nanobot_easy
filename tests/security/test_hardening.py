"""Regression coverage for the operating security profile; never uses real credentials."""
import io
import urllib.request
from unittest.mock import patch

import pytest
from loguru import logger
from mcp import ClientSession, types

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import FindFilesTool, GrepTool
from nanobot.config.schema import ToolsConfig
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.security.secrets import RedactingStream, redact, register_secrets
from nanobot.security.workspace_access import WorkspaceScopeResolver, default_workspace_scope
from nanobot.webui.file_preview import WebUIFilePreviewError, file_preview_payload
from nanobot.webui.http_utils import request_origin_allowed
from nanobot.webui.workspaces import WebUIWorkspaceController


def test_final_exception_output_is_redacted():
    secret = "synthetic-config-credential-23456"
    register_secrets({"apiKey": secret})
    stream = io.StringIO()
    sink = logger.add(RedactingStream(stream), diagnose=False, backtrace=False)
    try:
        try:
            raise ValueError(f"dependency error: {secret}; ExtBot[token=123456789:synthetic_token_0123456789012345]")
        except ValueError:
            logger.exception("request failed")
    finally:
        logger.remove(sink)
    assert secret not in stream.getvalue()
    assert "synthetic_token" not in stream.getvalue()
    assert "ValueError" in stream.getvalue()
    assert "Bearer [REDACTED]" in redact("Bearer synthetic-api-token")


@pytest.mark.parametrize("headers,allowed", [
    ({"Host": "127.0.0.1:8766", "Origin": "http://127.0.0.1:8766"}, True),
    ({"Host": "localhost:8766"}, True),
    ({"Host": "audit.invalid:8766", "Origin": "http://audit.invalid:8766"}, False),
    ({"Host": "localhost:8766", "Origin": "https://audit.invalid"}, False),
    ({"Host": "localhost:8766", "Origin": "null"}, False),
    ({"Host": "localhost:8766", "Origin": "http://[broken"}, False),
    ({}, False),
])
def test_host_and_origin_boundaries(headers, allowed):
    assert request_origin_allowed(headers, host="127.0.0.1", port=8766) is allowed


@pytest.mark.asyncio
async def test_sensitive_files_hidden_from_read_search_and_preview(tmp_path):
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    key = secrets / "service.env"
    key.write_text("SYNTHETIC_PRIVATE_MARKER")
    (tmp_path / "alias.txt").symlink_to(key)
    (tmp_path / "public.txt").write_text("PUBLIC_MARKER")
    reader = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    for name in [".secrets/service.env", "alias.txt"]:
        result = await reader.execute(path=name)
        assert "SYNTHETIC_PRIVATE_MARKER" not in result
        with pytest.raises(WebUIFilePreviewError) as error:
            file_preview_payload(name, scope=default_workspace_scope(tmp_path, True))
        assert error.value.status == 403
    grep = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    assert "SYNTHETIC_PRIVATE_MARKER" not in await grep.execute(
        pattern="MARKER", output_mode="content")
    found = await FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path).execute()
    assert "alias.txt" not in found and "service.env" not in found
    assert "PUBLIC_MARKER" in await reader.execute(path="public.txt")


def test_hardened_profile_does_not_load_plugins_or_executors(tmp_path, monkeypatch):
    loader = ToolLoader()
    monkeypatch.setattr(loader, "_discover_plugins", lambda: pytest.fail("plugin imported"))
    config = ToolsConfig(hardened_mode=True, restrict_to_workspace=True)
    registry = ToolRegistry()
    names = loader.load(ToolContext(config=config, workspace=str(tmp_path)), registry)
    assert {"read_file", "write_file", "grep", "search_memory"} <= set(names)
    assert not set(names) & {"exec", "exec_job", "run_cli_app", "codewhale", "spawn", "delegate", "my"}
    assert not any(name.startswith("mcp_") for name in names)


@pytest.mark.asyncio
async def test_hardened_root_cannot_be_changed_by_session_metadata(tmp_path):
    resolver = WorkspaceScopeResolver(tmp_path, False, locked=True)
    full = {"workspace_scope": {"project_path": str(tmp_path.parent), "access_mode": "full"}}
    scope = resolver.for_turn(channel="websocket", message_metadata=full, session_metadata=full)
    assert scope.project_path == tmp_path and scope.restrict_to_workspace
    controller = WebUIWorkspaceController(session_manager=None, default_workspace=tmp_path,
                                         default_restrict_to_workspace=False, controls_enabled=False)
    with pytest.raises(ValueError):
        controller.scope_from_envelope(full, session_key=None, controls_available=True)
    writer = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path, lock_workspace=True)
    await writer.execute(path="../outside.txt", content="secret")
    assert not (tmp_path.parent / "outside.txt").exists()
    result = await writer.execute(path="normal.txt", content="allowed")
    assert (tmp_path / "normal.txt").read_text() == "allowed", result


@pytest.mark.asyncio
async def test_sdk_schema_references_cannot_retrieve_external_urls():
    client = ClientSession(None, None)
    client._tool_output_schemas["audit"] = {"$ref": "https://audit.invalid/schema.json"}
    with patch.object(urllib.request, "urlopen", side_effect=AssertionError("external retrieval")) as fetch:
        with pytest.raises(RuntimeError, match="Invalid schema"):
            await client._validate_tool_result("audit", types.CallToolResult(content=[], structuredContent={}))
        fetch.assert_not_called()


def test_session_purge_removes_indexes_entities_tasks_and_preserves_other_chat(tmp_path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="telegram-user", workspace_id=str(tmp_path), agent_id="main")
    events = []
    for session in ["websocket:deleted", "websocket:kept"]:
        events.append(store.append_event(scope=scope, conversation_id=session, session_id=session,
                      actor="user", event_type="USER_MESSAGE", content="UniquePrivateMarker https://example.com"))
    with store.connect() as conn:
        for event in events:
            conn.execute("INSERT INTO tasks(task_id,title,status,source_event_id) VALUES(?,?,?,?)",
                         (event.event_id, "private task", "pending", event.event_id))
    assert store.purge_session("websocket:deleted") == 1
    assert store.purge_session("websocket:deleted") == 0
    assert store.get_events(scope, [events[0].event_id]) == []
    assert len(store.get_events(scope, [events[1].event_id])) == 1
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM entities WHERE event_id=?", (events[0].event_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'UniquePrivateMarker'").fetchone()[0] == 1

@pytest.mark.asyncio
async def test_sdk_local_schema_references_still_work():
    client = ClientSession(None, None)
    client._tool_output_schemas["local"] = {
        "$defs": {"value": {"type": "string"}},
        "type": "object", "properties": {"value": {"$ref": "#/$defs/value"}},
    }
    await client._validate_tool_result("local", types.CallToolResult(content=[], structuredContent={"value": "valid"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["https://other.invalid/mcp", "http://trusted.invalid/mcp", "https://trusted.invalid:8443/mcp"])
async def test_sdk_redirect_cannot_forward_custom_credentials_or_body(location):
    import httpx
    from mcp.shared._httpx_utils import stream_within_origin

    seen = []

    def transport(request):
        seen.append((str(request.url), request.headers.get("X-Api-Key"), request.content))
        return httpx.Response(307, headers={"Location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True,
                                 headers={"X-Api-Key": "synthetic-credential"}) as client:
        async with stream_within_origin(client, "POST", "https://trusted.invalid/mcp",
                                        content=b"synthetic-private-body") as response:
            assert response.status_code == 307
    assert seen == [("https://trusted.invalid/mcp", "synthetic-credential", b"synthetic-private-body")]

@pytest.mark.asyncio
async def test_recursive_search_cannot_follow_external_or_internal_aliases(tmp_path):
    from nanobot.agent.tools.filesystem import ListDirTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("OUTSIDE_PRIVATE_MARKER")
    (workspace / "alias.txt").symlink_to(outside)
    internal = workspace / "sessions"
    internal.mkdir()
    (internal / "state.txt").write_text("INTERNAL_STATE_MARKER")
    (workspace / "normal.txt").write_text("PUBLIC_MARKER")
    for tool_cls, params in [(GrepTool, {"pattern": "MARKER", "output_mode": "content"}),
                             (FindFilesTool, {}), (ListDirTool, {"path": ".", "recursive": True})]:
        tool = tool_cls(workspace=workspace, allowed_dir=workspace, lock_workspace=True)
        result = await tool.execute(**params)
        assert "OUTSIDE_PRIVATE_MARKER" not in result and "INTERNAL_STATE_MARKER" not in result
        assert "alias.txt" not in result and "state.txt" not in result
        assert "normal.txt" in result
