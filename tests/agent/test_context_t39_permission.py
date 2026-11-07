"""t39 unit 1: permission targets are full normalized identifiers + action + scope + version."""
import asyncio
from types import SimpleNamespace

from nanobot.agent.context_control import TaskControl, target_identifier


def _control(ws, *texts):
    state = {"task_id": "T1", "instruction_version": 3,
             "user_constraints": [{"event_id": f"e{i}", "sequence": i, "text": t, "kind": "user_original"}
                                  for i, t in enumerate(texts, 1)]}
    sel = SimpleNamespace(state=state, blocked=None, current_event=None, workspace=str(ws),
                          evidence_error=lambda for_mutation=True: None, trace=SimpleNamespace(request_id="r"))
    return TaskControl(sel)


def test_same_name_in_other_directory_is_a_different_target(tmp_path):
    (tmp_path / "docs").mkdir()
    c = _control(tmp_path, "파일은 수정하지 마.", "이제 docs/notes.md 파일에 적용해 줘.")
    assert c.authorize("write_file", str(tmp_path / "docs" / "notes.md")) is None
    assert c.authorize("write_file", "docs/notes.md") is None                 # same identifier, relative
    assert c.authorize("write_file", str(tmp_path / "notes.md")) is not None  # same basename, other dir
    assert c.authorize("write_file", str(tmp_path / "other" / "notes.md")) is not None


def test_bare_name_grant_does_not_open_a_nested_same_name_file(tmp_path):
    c = _control(tmp_path, "파일은 수정하지 마.", "이제 notes.md 파일에 적용해 줘.")
    assert c.authorize("write_file", str(tmp_path / "notes.md")) is None
    assert c.authorize("write_file", str(tmp_path / "docs" / "notes.md")) is not None


def test_target_outside_workspace_scope_is_not_permitted(tmp_path):
    c = _control(tmp_path / "ws", "파일은 수정하지 마.", "이제 ../notes.md 파일에 적용해 줘.")
    assert c.authorize("write_file", str(tmp_path / "notes.md")) is not None


def test_symlink_alias_is_one_identifier(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    assert target_identifier("alias/x.md", tmp_path) == target_identifier("real/x.md", tmp_path)


def test_check_records_task_version_action_and_full_target(tmp_path):
    c = _control(tmp_path, "파일은 수정하지 마.", "이제 docs/notes.md 파일에 적용해 줘.")
    c.authorize("write_file", "docs/notes.md")
    assert c.last_check == {"task_id": "T1", "instruction_version": 3, "action": "change",
                            "target": str((tmp_path / "docs" / "notes.md").resolve())}


def test_recheck_immediately_before_execution_uses_full_target(tmp_path):
    c = _control(tmp_path, "파일은 수정하지 마.", "이제 docs/notes.md 파일에 적용해 줘.")
    ran = []

    async def op():
        ran.append(1)
        return "ok", {"status": "ok"}, None

    async def drain():
        return []
    call = SimpleNamespace(id="c1", name="write_file", arguments={"path": str(tmp_path / "notes.md")})
    result, event, _ = asyncio.run(c.run_mutation(call, SimpleNamespace(), op, drain))
    assert event["status"] == "blocked" and ran == []
