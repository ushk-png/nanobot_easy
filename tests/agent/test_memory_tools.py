import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.memory import ReadMemoryEventsTool, SearchMemoryTool
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope


@pytest.mark.asyncio
async def test_memory_tools_are_scope_bound(tmp_path):
    scope = MemoryScope.from_runtime(owner_id="chat-a", workspace_id=str(tmp_path.resolve()), agent_id=None)
    store = ConversationEventStore(tmp_path)
    event = store.append_event(
        scope=scope,
        conversation_id="telegram:chat-a",
        session_id="telegram:chat-a",
        actor="assistant",
        event_type="ASSISTANT_MESSAGE",
        content="그때 답변 원문입니다. ToolRegistry와 intent_summary를 설명했습니다.",
    )

    search = SearchMemoryTool(str(tmp_path))
    search.set_context(RequestContext(channel="telegram", chat_id="chat-a", session_key="telegram:chat-a"))
    output = await search.execute(query="ToolRegistry intent_summary", limit=5)
    assert event.event_id in output

    reader = ReadMemoryEventsTool(str(tmp_path))
    reader.set_context(RequestContext(channel="telegram", chat_id="chat-a", session_key="telegram:chat-a"))
    raw = await reader.execute(event_ids=[event.event_id])
    assert "그때 답변 원문입니다" in raw

    search_other = SearchMemoryTool(str(tmp_path))
    search_other.set_context(RequestContext(channel="telegram", chat_id="chat-b", session_key="telegram:chat-b"))
    other_output = await search_other.execute(query="ToolRegistry intent_summary", limit=5)
    assert event.event_id not in other_output
