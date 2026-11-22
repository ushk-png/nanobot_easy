"""Phase 2 duplicate-context removal (model input only; offline, network-free, fake providers).

A: the archived summary shown as [Archived Context Summary] is not repeated in Recent Memory.
B: after update_task selects a scope, this request's guidance drops unrelated earlier reads.
"""
import copy
import json
import re
import socket
from datetime import datetime

import pytest

from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import TaskNote, TaskUpdate
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.agent.test_compact_context import Script
from tests.agent.test_task_note_followup_reuse import Script as RunScript
from tests.agent.test_task_note_verified_reads import (
    ANCHOR,
    FOLLOWUP,
    _read,
    by_name,
    history,
    names,
    prior_session,
)

KEY = "cli:direct"
SUMMARY = "Synthetic summary: report R-A has 12 items, R-B has 9 items; nothing was changed."
TAG = ContextBuilder._RECENT_MEMORY_TAG


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("network blocked in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _shown(text=SUMMARY):
    return AutoCompact._format_summary(text, datetime(2026, 1, 2, 3, 4))


def _blob(messages):
    return "\n".join(str(m.get("content")) for m in messages)


def _builder(tmp_path, entries):
    builder = ContextBuilder(tmp_path)
    for text, key in entries:
        builder.memory.append_history(text, session_key=key)
    return builder


# --- A: archived summary ------------------------------------------------------------------

def test_same_session_identical_summary_is_shown_once_with_labels(tmp_path):
    builder = _builder(tmp_path, [("Unrelated earlier note N-1.", KEY), (SUMMARY, KEY)])
    before = (tmp_path / "memory" / "history.jsonl").read_bytes()
    meta = {"_last_summary": {"text": SUMMARY, "last_active": "2026-01-02T03:04:00"}}
    saved = copy.deepcopy(meta)
    messages = builder.build_messages([], "continue", session_summary=_shown(),
                                      session_metadata=meta, session_key=KEY)
    assert _blob(messages).count(SUMMARY) == 1
    system, user = messages[0]["content"], messages[-1]["content"]
    assert "[Archived Context Summary]" in system and "Previous conversation summary" in system
    assert SUMMARY in system and TAG in user and "Unrelated earlier note N-1." in user
    assert [m["role"] for m in messages] == ["system", "user"]
    assert (tmp_path / "memory" / "history.jsonl").read_bytes() == before and meta == saved


def test_only_entry_removed_drops_empty_block_and_compact_prompt_is_same(tmp_path):
    builder = _builder(tmp_path, [(SUMMARY, KEY)])
    meta = {"_last_summary": {"text": SUMMARY, "last_active": "2026-01-02T03:04:00"}}
    messages = builder.build_messages([], "continue", session_summary=_shown(),
                                      session_metadata=meta, session_key=KEY)
    assert TAG not in messages[-1]["content"]
    compact = builder.build_compact_system_prompt(session_summary=_shown())
    assert (compact + messages[-1]["content"]).count(SUMMARY) == 1


def test_repeated_summary_keeps_earlier_occurrence_and_timestamp(tmp_path):
    builder = _builder(tmp_path, [(SUMMARY, KEY), ("An intervening archive.", KEY), (SUMMARY, KEY)])
    entries = builder.memory.read_recent_history_for_prompt(0, session_key=KEY)
    # Separate times make it clear these are distinct historical occurrences.
    entries[0]["timestamp"] = "2026-01-01 10:00"
    entries[-1]["timestamp"] = "2026-01-02 10:00"
    builder.memory.read_recent_history_for_prompt = lambda **kwargs: entries
    meta = {"_last_summary": {"text": SUMMARY, "last_active": "2026-01-02T10:00:00"}}
    messages = builder.build_messages([], "continue", session_summary=SUMMARY,
                                      session_metadata=meta, session_key=KEY)
    user = messages[-1]["content"]
    assert f"[2026-01-01 10:00] {SUMMARY}" in user
    assert f"[2026-01-02 10:00] {SUMMARY}" not in user
    assert "An intervening archive." in user
    assert _blob(messages).count(SUMMARY) == 2
    assert len(entries) == 3


@pytest.mark.parametrize("entry,key,meta_text,shown,unified", [
    (SUMMARY + " Extra open item X-7.", KEY, SUMMARY, _shown(), False),   # partial overlap
    (SUMMARY, "telegram:other", SUMMARY, _shown(), True),                 # other session
    (SUMMARY, KEY, "Different newer summary.", _shown("Different newer summary."), False),
    (SUMMARY, KEY, SUMMARY, None, False),                                 # summary not shown
    (SUMMARY, KEY, SUMMARY, _shown(SUMMARY + " tail"), False),            # shown text differs
])
def test_unconfirmed_or_partial_duplicates_are_kept(tmp_path, entry, key, meta_text, shown, unified):
    builder = _builder(tmp_path, [(entry, key)])
    meta = {"_last_summary": {"text": meta_text, "last_active": "2026-01-02T03:04:00"}}
    messages = builder.build_messages([], "continue", session_summary=shown, session_metadata=meta,
                                      session_key=KEY, unified_session=unified)
    assert TAG in messages[-1]["content"] and entry in messages[-1]["content"]


@pytest.mark.parametrize("compact", [False, True])
async def test_normal_compact_and_restart_paths_send_summary_once(tmp_path, compact):
    def make(provider):
        return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                         compact_context=compact, tools_config=ToolsConfig(safe_mode=True),
                         mcp_servers={}, unified_session=False)

    first = Script([LLMResponse(content=SUMMARY), LLMResponse(content="ok-1")])
    loop = make(first)
    session = loop.sessions.get_or_create(KEY)
    summary = await loop.consolidator.archive(
        [{"role": "user", "content": "compare"}, {"role": "assistant", "content": "done"}],
        session_key=KEY)
    loop.consolidator._persist_last_summary(session, summary)
    loop.auto_compact._summaries[KEY] = (summary, datetime(2026, 1, 2, 3, 4))  # hot path
    history_file = tmp_path / "memory" / "history.jsonl"
    before = history_file.read_bytes()
    await loop.process_direct("continue the table", session_key=KEY)
    loop.stop()
    restarted = Script([LLMResponse(content="ok-2")])  # cold path: metadata after restart
    loop2 = make(restarted)
    await loop2.process_direct("continue", session_key=KEY)
    loop2.stop()
    for call in (first.calls[-1], restarted.calls[-1]):
        assert _blob(call["messages"]).count(SUMMARY) == 1
        assert "[Archived Context Summary]" in call["messages"][0]["content"]
    assert len(first.calls) == 2 and len(restarted.calls) == 1
    assert history_file.read_bytes() == before
    assert make(Script([])).sessions.get_or_create(KEY).metadata["_last_summary"]["text"] == SUMMARY


# --- B: task guidance scope ---------------------------------------------------------------

def _update(note, paths, *, source="files", status="active", intent="continue"):
    return TaskUpdate.model_validate({
        "request_id": note.request_id, "intent": intent, "goal": "shorter summary",
        "targets": [{"path": p, "unit": "lines", "start": 1, "end": 8,
                     "selection_reason": "previously analysed"} for p in paths],
        "source": source, "analysis_complete": status == "completed", "status": status,
        "explanation": "scope", "final_answer": "done" if status == "completed" else "",
        "planned_steps": [] if status == "completed" else ["check"]})


def _payload(message):
    return json.loads(message["content"].rsplit("\n", 1)[1])


async def _note(tmp_path, **kwargs):
    metadata = await prior_session(tmp_path)
    note = TaskNote([*history(metadata), {"role": "user", "content": FOLLOWUP}], tmp_path,
                    metadata, verified_reads=True, **kwargs)
    return note, [{"role": "user", "content": FOLLOWUP}, note.guidance()]


async def test_undecided_and_followup_scope_keep_earlier_candidates(tmp_path):
    note, messages = await _note(tmp_path)
    assert note.narrow_guidance(messages) is messages  # update not yet recorded
    note.update = _update(note, [])  # files requested but target still unresolved
    assert note.narrow_guidance(messages) is messages
    assert names(_payload(messages[-1])["verified_reads"]) == {"sample.md", "anchor.md", "reference.log"}


async def test_selected_target_drops_unrelated_evidence_in_model_copy_only(tmp_path):
    note, messages = await _note(tmp_path)
    saved, ledger = copy.deepcopy(messages), copy.deepcopy(note.evidence)
    note.update = _update(note, ["anchor.md"])
    narrowed = note.narrow_guidance(messages)
    shown = _payload(narrowed[-1])
    assert names(shown["verified_reads"]) == {"anchor.md"}
    assert shown["cached_reads"] == []  # only reference.log 61-80 were candidates
    assert narrowed[-1]["content"].rsplit("\n", 1)[0] == messages[-1]["content"].rsplit("\n", 1)[0]
    assert messages == saved and note.evidence == ledger and len(narrowed) == len(messages)
    assert note.narrow_guidance(narrowed) == narrowed  # idempotent, nothing appended
    assert note.narrow_guidance(messages) == narrowed  # stable across calls
    assert len(narrowed[-1]["content"]) < len(messages[-1]["content"])


async def test_malformed_cached_target_does_not_crash_prompt_optimization(tmp_path):
    note, messages = await _note(tmp_path)
    payload = _payload(messages[-1])
    payload["cached_reads"].append({"target": ["unexpected", "shape"]})
    prefix = messages[-1]["content"].rsplit("\n", 1)[0]
    messages[-1] = {**messages[-1], "content": prefix + "\n" + json.dumps(payload)}
    original = copy.deepcopy(messages)
    note.update = _update(note, ["anchor.md"])
    narrowed = note.narrow_guidance(messages)
    assert {"target": ["unexpected", "shape"]} in _payload(narrowed[-1])["cached_reads"]
    assert messages == original


async def test_target_change_is_recomputed_from_original_candidates(tmp_path):
    note, messages = await _note(tmp_path)
    note.update = _update(note, ["anchor.md"])
    assert names(_payload(note.narrow_guidance(messages)[-1])["verified_reads"]) == {"anchor.md"}
    note.update = _update(note, ["reference.log"], intent="change")
    shown = _payload(note.narrow_guidance(messages)[-1])
    assert names(shown["verified_reads"]) == {"reference.log"}
    assert names(shown["cached_reads"]) == {"reference.log"} and len(shown["cached_reads"]) == 20


async def test_no_target_differs_from_undecided_and_keeps_current_request_reads(tmp_path):
    note, _ = await _note(tmp_path)
    await _read(note, tmp_path, "anchor.md")  # read in the current request
    messages = [{"role": "user", "content": FOLLOWUP}, note.guidance()]
    note.update = _update(note, [], source="none")
    shown = _payload(note.narrow_guidance(messages)[-1])
    assert shown["verified_reads"] == []
    assert shown["cached_reads"] and all(e["request_id"] == note.request_id
                                         for e in shown["cached_reads"])


async def test_feedback_cancellation_and_other_control_messages_are_untouched(tmp_path):
    note, messages = await _note(tmp_path)
    marker = f"current request_id={note.request_id}"
    feedback = {"role": "user", "_hidden_history": True, "_task_control": True,
                "content": "Completion defects (all): x.\n" + note.guidance()["content"]}
    revoked = {"role": "user", "_hidden_history": True, "_task_control": True,
               "content": f"Approval withdrawn by the user; {marker}. Do not write anchor.md."}
    stale = {**note.guidance(), "content": note.guidance()["content"].replace(
        note.request_id, "0" * 32)}
    extra = [feedback, revoked, stale]
    note.update = _update(note, ["anchor.md"], intent="withdraw", status="cancelled")
    narrowed = note.narrow_guidance([*messages, *extra])
    assert narrowed[2:] == extra
    assert names(_payload(narrowed[1])["verified_reads"]) == {"anchor.md"}


async def test_truncated_read_visibility_is_not_overstated_after_narrowing(tmp_path):
    metadata = await prior_session(tmp_path)
    read = "\n".join(f"{i}| {line}" for i, line in enumerate(ANCHOR.splitlines(), 1))
    old = history(metadata, read_results=[("anchor.md", 1, 8, read)])
    note = TaskNote(old + [{"role": "user", "content": "anchor.md 요약해줘"}], tmp_path,
                    metadata, verified_reads=True)
    note.update = _update(note, ["anchor.md"])
    snipped = [{"role": "user", "content": "anchor.md 요약해줘"}, note.guidance()]
    final = note.refresh_visible_reads(note.narrow_guidance(snipped))
    entry = by_name(_payload(final[-1])["verified_reads"])["anchor.md"]
    assert entry["text_in_context"] == []
    visible = note.refresh_visible_reads(note.narrow_guidance([*old, *snipped]))
    assert by_name(_payload(visible[-1])["verified_reads"])["anchor.md"]["text_in_context"] == [[1, 8]]


def _control(targets, status):
    def response(kwargs):
        text = next(m["content"] for m in kwargs["messages"] if isinstance(m.get("content"), str)
                    and m["content"].startswith("[Task evidence control; current request_id="))
        request_id = re.search(r"current request_id=([a-f0-9]+)", text)[1]
        args = {"request_id": request_id, "intent": "continue", "goal": "shorter summary",
                "targets": [{"selection_reason": "previously analysed", **t} for t in targets],
                "source": "files", "analysis_complete": status == "completed", "status": status,
                "explanation": "reuse", "final_answer": "요약입니다." if status == "completed" else "",
                "planned_steps": [] if status == "completed" else ["reuse earlier reads"]}
        return LLMResponse(content="", tool_calls=[ToolCallRequest(
            id=f"c{len(kwargs['messages'])}", name="update_task", arguments=args)])
    return response


def _guidance(call):
    return next(m for m in call["messages"] if isinstance(m.get("content"), str)
                and m["content"].startswith("[Task evidence control; current request_id="))


async def _run(tmp_path, metadata, steps, verified_reads=False):
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(WriteFileTool(workspace=tmp_path))
    provider = RunScript(steps)
    spec = AgentRunSpec(initial_messages=[*history(metadata), {"role": "user", "content": FOLLOWUP}],
                        tools=tools, model="test", max_iterations=12, max_tool_result_chars=8000,
                        workspace=tmp_path, task_note_enabled=True, task_note_metadata=metadata,
                        task_note_verified_reads=verified_reads)
    return await AgentRunner(provider).run(spec), provider


LOG_ONLY = [{"path": "reference.log", "unit": "lines", "start": 1, "end": 80}]


def _ledger_shape(metadata):
    runs = {}
    return [(e["target"].rsplit("/", 1)[-1], e["start"], e["end"],
             runs.setdefault(e["request_id"], len(runs))) for e in metadata["task_note"]["evidence"]]


async def _ledger_without_narrowing(tmp_path, monkeypatch, verified_reads):
    with monkeypatch.context() as patch:
        patch.setattr(TaskNote, "narrow_guidance", lambda self, messages: messages)
        metadata = await prior_session(tmp_path)
        await _run(tmp_path, metadata, [_control(LOG_ONLY, "active"), _control(LOG_ONLY, "completed")],
                   verified_reads=verified_reads)
    return _ledger_shape(metadata)


@pytest.mark.parametrize("verified_reads", [False, True])
async def test_runner_reuses_evidence_with_narrowed_guidance_and_no_extra_calls(
        tmp_path, monkeypatch, verified_reads):
    (tmp_path / "base").mkdir()
    expected_ledger = await _ledger_without_narrowing(tmp_path / "base", monkeypatch, verified_reads)
    tmp_path = tmp_path / "new"
    tmp_path.mkdir()
    metadata = await prior_session(tmp_path)
    result, provider = await _run(tmp_path, metadata, [_control(LOG_ONLY, "active"),
                                                       _control(LOG_ONLY, "completed")],
                                  verified_reads=verified_reads)
    assert result.stop_reason == "completed" and result.final_content == "요약입니다."
    assert len(provider.calls) == 2 and result.tools_used == ["update_task", "update_task"]
    first, second = (_payload(_guidance(c)) for c in provider.calls)
    assert names(second["cached_reads"]) == {"reference.log"}
    if verified_reads:
        assert len(first["verified_reads"]) == 3 and names(second["verified_reads"]) == {"reference.log"}
    guidance_count = [sum(1 for m in c["messages"] if m.get("_task_control")) for c in provider.calls]
    assert guidance_count[0] == guidance_count[1] == 1  # updated in place, never appended
    assert _ledger_shape(metadata) == expected_ledger  # stored ledger handling is unchanged


async def test_runner_changed_file_still_forces_a_real_read(tmp_path):
    metadata = await prior_session(tmp_path)
    (tmp_path / "reference.log").write_text(
        (tmp_path / "reference.log").read_text().replace("REF-03", "REF-3X"))
    full = LLMResponse(content="", tool_calls=[ToolCallRequest(
        id="full", name="read_file", arguments={"path": "reference.log"})])
    result, provider = await _run(tmp_path, metadata, [_control(LOG_ONLY, "completed"), full,
                                                       _control(LOG_ONLY, "completed")])
    assert result.stop_reason == "completed"
    assert result.tools_used.count("read_file") == 1 and len(provider.calls) == 3
