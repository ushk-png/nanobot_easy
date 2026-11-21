"""Prompt projections must save tokens without changing persisted evidence or user scope."""
import json
from copy import deepcopy

import pytest

from nanobot.agent.task_note import TaskNote
from nanobot.session.history_visibility import task_result_for_replay
from nanobot.session.manager import Session
from tests.agent.test_task_note import read, target, update
from tests.agent.test_task_note_verified_reads_v3 import pair


def payload(note):
    return json.loads(note.guidance()["content"].split("\n")[-1])


@pytest.mark.parametrize("prompt", [
    "new.md 파일을 읽고 요약해줘.",
    "작업공간에 new.md를 만들고 지정 문구를 저장해줘. 이미 있으면 덮어쓰지 말고 알려줘.",
])
async def test_unrelated_cached_reads_stay_on_disk_but_not_in_named_task_prompt(tmp_path, prompt):
    (tmp_path / "old.md").write_text("old\n" * 20)
    (tmp_path / "new.md").write_text("new\n")
    metadata = {}
    old = TaskNote([{"role": "user", "content": "old.md 전체를 읽어줘."}], tmp_path, metadata)
    for number in range(1, 21):
        await read(old, tmp_path, name="old.md", offset=number, limit=1)
    original = deepcopy(old.evidence)

    note = TaskNote([{"role": "user", "content": prompt}],
                    tmp_path, metadata)
    assert payload(note)["cached_reads"] == []
    # Inspect the explicit read scope separately; a save request still needs a real write.
    if "저장" in prompt:
        note.revise([{"role": "user", "content": "new.md 파일을 읽어줘."}])
    result = json.loads(note.accept(update(note, targets=[target(path="new.md")])))
    assert result["evidence"] == []
    assert "coverage_unconfirmed:new.md" in result["defects"]
    assert note.evidence == original
    assert metadata["task_note"]["evidence"] == original

    await read(note, tmp_path, name="new.md")
    result = json.loads(note.accept(update(note, targets=[target(path="new.md")])))
    assert result["defects"] == []
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["target"] == str((tmp_path / "new.md").resolve())
    assert len(note.evidence) == 21


async def test_followup_can_select_old_proofs_after_prompt_filtering(tmp_path):
    (tmp_path / "old.md").write_text("old\n")
    metadata = {}
    old = TaskNote([{"role": "user", "content": "old.md 파일을 읽어줘."}], tmp_path, metadata)
    await read(old, tmp_path, name="old.md")
    note = TaskNote([{"role": "user", "content": "방금 읽은 파일로 계속해줘."}], tmp_path, metadata)
    assert payload(note)["cached_reads"]
    result = json.loads(note.accept(update(note, targets=[target(path="old.md")])))
    assert result["defects"] == []
    assert result["evidence"]
    result = json.loads(note.accept(update(note, source="none", targets=[])))
    assert result["evidence"] == []
    assert note.evidence  # Changing the prompt view never discards the original proof.


@pytest.mark.parametrize("tagged", [False, True])
def test_old_recovery_control_is_not_a_new_user_instruction(tagged):
    old = {"role": "user", "_hidden_history": True,
           "content": "Completion defects (all): old task. Do not repeat successful work."}
    if tagged:
        old.update(_task_control=True, content="A future runtime-only correction format.")
    records = [
        {"role": "user", "content": "Do not change the file; the old approval is withdrawn."},
        old,
        {"role": "assistant", "content": "The file was preserved."},
        {"role": "user", "content": "Completion defects (all): this is literal quoted user data."},
        {"role": "user", "_hidden_history": True, "content": "Earlier user constraints: no writes."},
    ]
    original = deepcopy(records)
    session = Session("test:scope", messages=records)
    history = session.get_history(max_messages=100, max_tokens=16000)
    assert not any(m["content"] == old["content"] for m in history)
    assert history[0]["content"] == records[0]["content"]
    assert any(m["content"] == records[-2]["content"] for m in history)
    assert any(m["content"] == records[-1]["content"] for m in history)
    assert session.messages == original


async def test_slim_replay_keeps_tool_pairs_read_visibility_and_evidence_membership(tmp_path):
    (tmp_path / "old.md").write_text("one\ntwo\n")
    metadata = {}
    note = TaskNote([{"role": "user", "content": "old.md 파일을 읽어줘."}], tmp_path, metadata)
    text = await read(note, tmp_path, name="old.md")
    accepted = note.accept(update(note, targets=[target(path="old.md")]))
    records = [{"role": "user", "content": "old.md 파일을 읽어줘."}]
    records += pair("read-old", "read_file", {"path": "old.md"}, str(text))
    records += pair("accept-old", "update_task", {"request_id": note.request_id}, accepted)
    original = deepcopy(records)
    history = Session("test:proof", messages=records).get_history(max_tokens=16000)
    result = next(m for m in history if m.get("name") == "update_task")
    assert json.loads(result["content"])["request_id"] == note.request_id
    assert json.loads(result["content"])["evidence"] == []
    assert result["tool_call_id"] == "accept-old"
    assert any("1| one" in str(m.get("content")) for m in history)
    follow = TaskNote([*history, {"role": "user", "content": "그 파일로 계속해줘."}],
                      tmp_path, metadata, verified_reads=True)
    summary = payload(follow)["verified_reads"]
    assert len(summary) == 1
    assert summary[0]["whole_file_verified"]
    assert summary[0]["text_in_context"] == [[1, 2]]
    assert records == original


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"request_id": 7, "evidence": [1]}'])
def test_unrecognised_old_tool_results_are_not_rewritten(content):
    message = {"role": "tool", "name": "update_task", "content": content}
    assert task_result_for_replay(message) == content
    ordinary = {**message, "name": "read_file"}
    assert task_result_for_replay(ordinary) == content
