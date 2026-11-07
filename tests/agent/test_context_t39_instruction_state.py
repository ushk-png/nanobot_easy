"""t39 unit 2: one shared instruction state read by design validation, execution check, completion."""
from types import SimpleNamespace

from nanobot.agent.context_control import TaskControl
from nanobot.agent.context_selection import (IntentDesign, SelectedContext, instruction_entries,
                                             validate_meaning)
from nanobot.agent.context_trace import ContextTrace
from tests.agent.test_context_t37_museum import args


def _sel(tmp_path, texts):
    sel = SelectedContext(trace=ContextTrace("t39"))
    sel.scope = SimpleNamespace(workspace_ids=[str(tmp_path)])
    sel.state = {"task_id": "T", "instruction_version": 1, "session_id": "s", "executions": [],
                 "user_constraints": [{"event_id": f"e{i}", "sequence": i, "text": t, "kind": "user_original"}
                                      for i, t in enumerate(texts, 1)]}
    sel.control = TaskControl(sel)
    return sel


def _ingest(sel, seq, text):
    sel.control.ingest_event(SimpleNamespace(event_id=f"f{seq}", sequence=seq, content=text, ts=None))


def _design_entries(sel, request):
    payload = {"request": request, "sources": [{"event_id": c["event_id"], "sequence": c["sequence"]}
                                               for c in sel.state["user_constraints"]],
               "task_state": {"valid_constraint_candidates": [
                   {"source_id": c["event_id"], "text": c["text"], "status": "user_original"}
                   for c in sel.state["user_constraints"]]}}
    return instruction_entries(payload)


def test_followup_permission_reaches_every_path(tmp_path):
    from nanobot.agent.context_control import instruction_scope
    sel = _sel(tmp_path, ["파일은 수정하지 마."])
    assert sel.control.authorize("write_file", "notes.md") is not None
    _ingest(sel, 5, "이제 notes.md 파일에 적용해 줘.")
    sel.state.update(instruction_status="ACTIVE", status="ACTIVE", blockers=[])   # reconciled
    # execution check
    assert sel.control.authorize("write_file", "notes.md") is None
    # design validation reads the same stored instruction (user_constraints), not a separate copy
    assert instruction_scope(_design_entries(sel, "계속해 줘")) == {"change": {"notes.md"}}
    assert sel.instruction_reads["execution_check"]["instruction_version"] == 2


def test_followup_cancel_reaches_every_path(tmp_path):
    sel = _sel(tmp_path, ["notes.md 파일을 수정해 줘."])
    _ingest(sel, 5, "취소해.")
    assert sel.control.cancelled
    assert sel.control.authorize("write_file", "notes.md") is not None              # execution check
    assert any("취소" in c["text"] for c in sel.state["user_constraints"])            # design validation input
    sel.design = IntentDesign.model_validate(args("p2-file-r1-t1"))
    text, status = sel.verdict("완료했습니다.")
    assert status == "cancelled"                                                     # completion


def test_change_checked_under_older_version_cannot_complete(tmp_path):
    sel = _sel(tmp_path, ["notes.md 파일을 수정해 줘."])
    sel.note_instruction_read("execution_check")
    sel.observations["w"] = {"tool_call_id": "w", "name": "write_file", "status": "ok", "read_only": False,
                             "resource_id": str(tmp_path / "notes.md")}
    sel.state["instruction_version"] = 2                       # a later instruction arrived
    assert sel.stale_instruction_reads() == ["execution_check"]
    sel.design = IntentDesign.model_validate(args("p2-file-r1-t1"))
    assert sel.completion_supported() is False
    assert sel.instruction_reads["completion"]["instruction_version"] == 2
