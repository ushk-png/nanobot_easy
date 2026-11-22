"""Phase 5: short reviewed core memory, details on demand (synthetic data, fake provider)."""

import json
import socket

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory_prompt import (
    SNAPSHOT_NAME,
    BoundedMemoryPrompt,
    check_candidate,
    encode_snapshot,
    legacy_memory_tokens,
    make_snapshot,
    projection_tokens,
)
from nanobot.agent.memory_prompt_refresh import MemoryPromptRefresher
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.memory import ForgetMemoryEventsTool, SearchMemoryTool
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.memory import search as memory_search
from nanobot.memory.event_store import ConversationEventStore
from nanobot.providers.base import LLMResponse
from tests.agent.core_memory_synthetic import (
    CORE,
    CORRECTION,
    DETAIL_FACT,
    REQUIRED,
    REVOKED,
    SOURCE,
)
from tests.agent.test_compact_context import Script, call

BUDGET = 1000
APPROVED = json.dumps({"constraints_preserved": True, "faithful": True,
                       "no_new_authority": True, "issues": []})
REJECTED = json.dumps({"constraints_preserved": False, "faithful": True,
                       "no_new_authority": True, "issues": ["missing limit"]})


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def blocked(*_a, **_k):
        raise RuntimeError("network blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr("nanobot.agent.memory_prompt_refresh._DEBOUNCE_S", 0)


@pytest.fixture
def searches(monkeypatch):
    count = {"n": 0}
    original = memory_search.MemorySearcher.search

    def spy(self, *args, **kwargs):
        count["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(memory_search.MemorySearcher, "search", spy)
    return count


def _ws(root, text=SOURCE, core=CORE):
    (root / "memory").mkdir(parents=True)
    source = root / "memory" / "MEMORY.md"
    source.write_text(text, encoding="utf-8")
    (root / "AGENTS.md").write_text("현재 규칙: 배포하지 않는다.", encoding="utf-8")
    (root / "USER.md").write_text("사용자는 간결한 답을 선호한다.", encoding="utf-8")
    if core is not None:
        (root / "memory" / SNAPSHOT_NAME).write_bytes(
            encode_snapshot(make_snapshot(text, core, source, BUDGET)))
    return source


def _loop(root, provider, budget=BUDGET):
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=root,
                     memory_prompt_tokens=budget, memory_prompt_auto_refresh=True,
                     mcp_servers={}, tools_config=ToolsConfig(safe_mode=True), unified_session=False)


def _system(provider, index=0):
    return provider.calls[index]["messages"][0]["content"]


def _tool_texts(provider):
    return [m["content"] for c in provider.calls for m in c["messages"] if m["role"] == "tool"]


def test_core_memory_is_short_keeps_declared_conditions_and_bootstrap_files(tmp_path):
    source = _ws(tmp_path)
    raw = (tmp_path / "memory" / SNAPSHOT_NAME).read_bytes()
    assert check_candidate(SOURCE, raw, source, BUDGET, required=REQUIRED, revoked=REVOKED) == []
    assert projection_tokens(CORE, source) <= BUDGET < legacy_memory_tokens(SOURCE)
    prompt = ContextBuilder(tmp_path, memory_prompt_tokens=BUDGET).build_system_prompt()
    for options in REQUIRED:  # checked independently of any reviewer verdict
        assert any(option in prompt for option in options)
    assert DETAIL_FACT not in prompt and "완료 작업 042" not in prompt
    assert "현재 규칙: 배포하지 않는다." in prompt and "간결한 답을 선호" in prompt
    assert "use search_memory" in prompt and "errors or exclusions do not authorize" in prompt
    assert source.read_text(encoding="utf-8") == SOURCE


def test_reviewer_approval_alone_is_not_proof_of_preservation(tmp_path):
    source = _ws(tmp_path, core=None)
    weakened = CORE.replace("- 외부 메시지는 초안만 작성하고 직접 보내지 않는다.\n", "")
    revived = CORE + "\n- 배포를 진행해도 된다."
    for summary, problem in ((weakened, "required condition #5 missing"),
                             (revived, "revoked condition #2 present")):
        raw = encode_snapshot(make_snapshot(SOURCE, summary, source, BUDGET))
        assert problem in check_candidate(SOURCE, raw, source, BUDGET, required=REQUIRED,
                                          revoked=REVOKED)


async def test_simple_question_adds_no_search_or_refresh_calls(tmp_path, searches):
    _ws(tmp_path)
    provider = Script([LLMResponse(content="안녕하세요")])
    loop = _loop(tmp_path, provider)
    try:
        await loop.process_direct("안녕?")
        assert len(provider.calls) == 1 and searches["n"] == 0
        assert loop.context.memory_prompt.refresher._task is None
        assert loop.context.memory_prompt.status["mode"] == "snapshot"
    finally:
        loop.stop()


async def test_answer_already_in_conversation_needs_no_search(tmp_path, searches):
    _ws(tmp_path)
    provider = Script([LLMResponse(content="Kestrel 출시는 셋째 화요일입니다."),
                       LLMResponse(content="앞서 말씀드린 대로 셋째 화요일입니다.")])
    loop = _loop(tmp_path, provider)
    try:
        await loop.process_direct("Kestrel 출시는 셋째 화요일이야. 기억해.")
        await loop.process_direct("Kestrel 출시 언제라고 했지?")
        assert len(provider.calls) == 2 and searches["n"] == 0
        assert any("셋째 화요일" in str(m.get("content")) for m in provider.calls[1]["messages"][1:])
    finally:
        loop.stop()


async def test_detail_only_in_db_is_found_by_one_search(tmp_path):
    _ws(tmp_path)
    provider = Script([call("search_memory", query="Kestrel 추적 번호"),
                       LLMResponse(content=DETAIL_FACT)])
    loop = _loop(tmp_path, provider)
    try:
        await loop.process_direct("Kestrel 내부 추적 번호가 뭐였지?")
        assert DETAIL_FACT not in _system(provider)
        assert any(DETAIL_FACT in t and "lines" in t for t in _tool_texts(provider))
        assert len(provider.calls) == 2
    finally:
        loop.stop()


async def test_verbatim_request_returns_source_section_not_core_summary(tmp_path):
    source = _ws(tmp_path)
    search = SearchMemoryTool(str(tmp_path))
    search.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))
    await search.execute(query="보고서는 덮어쓰지 않는다")  # syncs MEMORY.md sections
    with ConversationEventStore(tmp_path).connect() as conn:
        event_id = conn.execute("SELECT event_id FROM events WHERE event_id LIKE 'memmd-%' "
                                "AND content LIKE '%보고서는 덮어쓰지%'").fetchone()[0]
    provider = Script([call("read_memory_events", event_ids=[event_id]),
                       LLMResponse(content="원문을 그대로 전달합니다.")])
    loop = _loop(tmp_path, provider)
    try:
        await loop.process_direct("보고서 덮어쓰기 정정 원문 그대로 보여줘")
        raw = next(t for t in _tool_texts(provider) if event_id in t)
        assert "이전의 덮어쓰기 허용 지시는 철회됨" in raw  # exact source wording
        assert "(이전 허용은 철회)" not in raw  # not the condensed core phrasing
        assert source.read_text(encoding="utf-8") == SOURCE
    finally:
        loop.stop()


async def test_source_change_falls_back_then_refreshes_matching_version_once(tmp_path):
    source = _ws(tmp_path)
    revised = SOURCE.replace("# 과거 승인", CORRECTION + "\n# 과거 승인")
    source.write_text(revised, encoding="utf-8")
    new_core = CORE + "\n- 정정(2026-11-20): 주간 요약은 금요일에만 보낸다."
    edit = json.dumps({"edits": [{"old": "", "new": "- 정정(2026-11-20): 주간 요약은 금요일에만 보낸다."}]})
    provider = Script([LLMResponse(content=edit), LLMResponse(content=APPROVED)])
    loader = BoundedMemoryPrompt(source, BUDGET)
    loader.refresher = MemoryPromptRefresher(source, BUDGET, lambda: (provider, "test"))
    assert loader.build(revised) is None  # stale snapshot is never used as current
    assert loader.status["mode"] == "fallback" and loader.status["reason"] == "source changed"
    assert loader.status["input_tokens"] == legacy_memory_tokens(revised) > BUDGET
    await loader.refresher._task
    projection = loader.build(revised)
    assert projection and "금요일에만" in projection
    data = json.loads((tmp_path / "memory" / SNAPSHOT_NAME).read_text())
    raw = (tmp_path / "memory" / SNAPSHOT_NAME).read_bytes()
    assert check_candidate(revised, raw, source, BUDGET, required=REQUIRED, revoked=REVOKED) == []
    assert data["refresh"]["method"] == "model-reviewed" and len(provider.calls) == 2
    assert [json.loads(c["messages"][1]["content"]).get("previous_summary") is not None
            for c in provider.calls] == [True, False]
    for _ in range(3):  # unchanged afterwards: no further generation or review
        assert loader.build(revised)
    assert len(provider.calls) == 2 and new_core.split("\n")[-1] in projection


@pytest.mark.parametrize("case", ["over_budget", "rejected", "concurrent_edit", "corrupted"])
async def test_failed_candidates_are_never_published(tmp_path, case):
    source = _ws(tmp_path)
    snapshot = tmp_path / "memory" / SNAPSHOT_NAME
    revised = SOURCE.replace("# 과거 승인", CORRECTION + "\n# 과거 승인")  # not append-only
    source.write_text(revised, encoding="utf-8")
    huge = "역사적 세부 사항. " * 3000
    edit = lambda new: LLMResponse(content=json.dumps({"edits": [{"old": "", "new": new}]}))  # noqa: E731
    if case == "over_budget":
        steps = [edit(huge), edit(huge)]
    elif case == "rejected":
        steps = [edit(CORRECTION.strip()), LLMResponse(content=REJECTED),
                 edit(CORRECTION.strip()), LLMResponse(content=REJECTED)]
    elif case == "concurrent_edit":
        steps = [edit(CORRECTION.strip()), LLMResponse(content=APPROVED)]  # approved, but stale
    else:
        snapshot.write_text("{corrupted", encoding="utf-8")
        steps = [LLMResponse(content=huge), LLMResponse(content=huge)]

    class Racing(Script):
        async def chat_with_retry(self, **kwargs):
            if case == "concurrent_edit" and not self.calls:
                source.write_text(revised + "- 동시 편집된 새 제한.\n", encoding="utf-8")
            return await super().chat_with_retry(**kwargs)

    provider = Racing(steps)
    before = snapshot.read_bytes()
    loader = BoundedMemoryPrompt(source, BUDGET)
    loader.refresher = MemoryPromptRefresher(source, BUDGET, lambda: (provider, "test"))
    assert loader.build(revised) is None
    await loader.refresher._task
    assert snapshot.read_bytes() == before  # nothing published
    # At most two drafts and two reviews; unchanged quality failures do not repeat on chat turns.
    assert len(provider.calls) == {"over_budget": 2, "rejected": 4, "concurrent_edit": 2,
                                   "corrupted": 2}[case]
    if case != "concurrent_edit":
        job = loader.refresher._task
        loader.build(revised)
        assert loader.refresher._task is job and len(provider.calls) == len(steps)
    current = source.read_text(encoding="utf-8")
    assert loader.build(current) is None  # raw fallback continues, never empty memory
    prompt = ContextBuilder(tmp_path, memory_prompt_tokens=BUDGET).build_system_prompt()
    assert current in prompt
    assert loader.status["mode"] == "fallback" and loader.status["input_tokens"] > BUDGET
    expected_reason = {"corrupted": "JSONDecodeError"}.get(case, "source changed")
    assert loader.status["reason"] == expected_reason


async def test_personal_forget_and_workspace_scope_with_core_memory(tmp_path):
    source = _ws(tmp_path / "a")
    _ws(tmp_path / "b", text=SOURCE.replace(DETAIL_FACT, "OTHER-WS-0001"))

    def tool(cls, root, chat):
        t = cls(str(root))
        t.set_context(RequestContext(channel="cli", chat_id=chat, session_key=f"cli:{chat}"))
        return t

    hit = await tool(SearchMemoryTool, tmp_path / "a", "chat-a").execute(query=DETAIL_FACT)
    target = next(line.split(": ", 1)[1] for line in hit.splitlines() if line.startswith("event_id: memmd-"))
    notice = await tool(ForgetMemoryEventsTool, tmp_path / "a", "chat-a").execute(
        event_ids=[target], mode="forget")
    assert "not a complete deletion" in notice
    mine = await tool(SearchMemoryTool, tmp_path / "a", "chat-a").execute(query=DETAIL_FACT)
    other_user = await tool(SearchMemoryTool, tmp_path / "a", "chat-b").execute(query=DETAIL_FACT)
    other_ws = await tool(SearchMemoryTool, tmp_path / "b", "chat-a").execute(query=DETAIL_FACT)
    assert target not in mine and target in other_user and target not in other_ws
    assert DETAIL_FACT not in other_ws.split("Result 1", 1)[-1] if "Result 1" in other_ws else True
    # Core memory and the source are unaffected by personal search exclusion.
    assert source.read_text(encoding="utf-8") == SOURCE
    assert ContextBuilder(tmp_path / "a", memory_prompt_tokens=BUDGET).memory_prompt.build(SOURCE)
