"""Mixed allow/forbid requests must preserve both scopes, without a shell escape."""

import pytest

from nanobot.agent.task_note import TaskNote
from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.filesystem import EditFileTool
from nanobot.agent.write_scope import protected_files
from tests.agent.test_task_note import call, control, run, target, update

MEMORY_REQUEST = (
    "기억에 있는 ‘샘플도토리’ 프로젝트의 확인번호를 SNAP-8842로 바꿔줘. "
    "외부 전송 금지는 유지하고 다른 기억은 건드리지 마. "
    "검토본은 직접 수정하지 말고, 원문 변경 결과만 짧게 알려줘."
)


def active_note(tmp_path, request=MEMORY_REQUEST, paths=("memory/MEMORY.md",)):
    note = TaskNote([{"role": "user", "content": request}], tmp_path, {})
    note.accept(update(note, targets=[target(p, action="write") for p in paths],
                       status="active", analysis_complete=False))
    return note


@pytest.mark.parametrize("instruction", [
    "a.md를 수정해줘. b.md는 수정하지 마.",
    "a.md를 수정해줘. `b.md`는 직접 수정하지 말고 결과만 알려줘.",
    "a.md를 수정해줘, b.md는 수정하지 마.",
    "a.md를 수정해줘. b.md와 c.md는 수정하지 마.",
    "Edit a.md but do not edit b.md.",
    "Edit a.md. Do not write the b.md.",
    "Edit a.md. Do not edit b.md and c.md.",
    "a.md를 수정해줘. b.md는 읽기 전용이야.",
])
def test_named_ban_allows_only_independently_selected_write(tmp_path, instruction):
    note = active_note(tmp_path, instruction, paths=("a.md", "b.md"))
    assert note.mutation_error("edit_file", {"path": "a.md"}) is None
    assert note.mutation_error("edit_file", {"path": "b.md"})
    assert note.mutation_error("write_file", {"path": "unselected.md"})


def test_memory_aliases_have_fixed_paths_not_model_selected_aliases(tmp_path):
    note = active_note(tmp_path, paths=("memory/MEMORY.md", "memory/prompt_snapshot.json"))
    assert note.mutation_error("edit_file", {"path": "memory/MEMORY.md"}) is None
    assert note.mutation_error("write_file", {"path": "memory/prompt_snapshot.json"})
    assert protected_files(MEMORY_REQUEST, tmp_path) == [str(tmp_path / "memory/prompt_snapshot.json")]
    reverse = active_note(tmp_path, "기억 검토본을 수정해줘. 원문은 수정하지 마.",
                          paths=("memory/MEMORY.md", "memory/prompt_snapshot.json"))
    assert reverse.mutation_error("edit_file", {"path": "memory/MEMORY.md"})
    assert reverse.mutation_error("edit_file", {"path": "memory/prompt_snapshot.json"}) is None


@pytest.mark.parametrize("instruction", [
    "수정하지 말고 검토만 해줘.",
    "a.md를 수정해줘. 어떤 파일도 수정하지 마.",
    "a.md를 수정해줘. 저장 금지.",
    "Edit a.md. Review only.",
    "Edit a.md. Do not write anything.",
    "a.md를 수정해줘. 검토본은 직접 수정하지 마.",  # unnamed outside memory context
    "a.md를 수정해줘. 그 파일은 수정하지 마.",
    "a.md를 수정해줘. b.md는 수정하지 마. 수정하지 말고 검토만 해.",
    "a.md를 수정해줘. 모든 파일 b.md는 수정하지 마.",
    "b.md는 수정하지 마.",
    "Do not edit b.md.",
    "edit.md를 읽어줘. b.md는 수정하지 마.",
    "Read edit.md. Do not write b.md.",
])
def test_global_or_unresolved_ban_cannot_be_waived_by_selected_targets(tmp_path, instruction):
    note = active_note(tmp_path, instruction, paths=("a.md", "b.md"))
    assert protected_files(instruction, tmp_path) is None
    error = note.mutation_error("write_file", {"path": "a.md"})
    assert error and "clarification" in error
    assert note.mutation_error("exec", {"command": "anything", "target": "a.md"})


@pytest.mark.parametrize("name,params", [
    ("exec", {"command": "touch memory/prompt_snapshot.json", "target": "memory/MEMORY.md"}),
    ("exec_job", {"action": "start", "target": "memory/MEMORY.md"}),
    ("spawn", {"task": "change memory"}),
    ("delegate", {"target": "memory/MEMORY.md"}),
    ("mcp_edit", {"path": "memory/MEMORY.md"}),
])
def test_other_mutating_tools_cannot_use_declared_target_to_bypass_ban(tmp_path, name, params):
    error = active_note(tmp_path).mutation_error(name, params)
    assert error and "clarification" in error


def test_every_patch_path_is_checked_before_any_write(tmp_path):
    note = active_note(tmp_path, paths=("memory/MEMORY.md", "memory/prompt_snapshot.json"))
    good = {"path": "memory/MEMORY.md", "action": "add", "new_text": "allowed"}
    bad = {"path": "memory/prompt_snapshot.json", "action": "add", "new_text": "forbidden"}
    assert note.mutation_error("apply_patch", {"edits": [good]}) is None
    assert note.mutation_error("apply_patch", {"edits": [good, bad]})
    assert note.mutation_error("apply_patch", {"edits": [good, {}]})
    assert note.mutation_error("apply_patch", {"edits": [good, None]})


def test_exact_paths_basename_and_symlink_cannot_hide_protected_file(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "prompt_snapshot.json").write_text("protected")
    (tmp_path / "alias.json").symlink_to(memory / "prompt_snapshot.json")
    note = active_note(tmp_path, paths=("memory/MEMORY.md", "alias.json"))
    assert note.mutation_error("edit_file", {"path": "alias.json"})
    note = active_note(tmp_path, "a.md를 수정해줘. b.md는 수정하지 마.",
                       paths=("data.md", "other/b.md"))
    assert note.mutation_error("edit_file", {"path": "data.md"}) is None
    assert note.mutation_error("edit_file", {"path": "other/b.md"})


def test_scoped_ban_does_not_hide_required_write_or_override_withdrawal(tmp_path):
    note = active_note(tmp_path, "a.md를 수정해줘. b.md는 수정하지 마.", paths=())
    assert "write_target_unresolved" in note.defects()
    note.accept(update(note, targets=[target("a.md", action="write")], intent="withdraw",
                       status="cancelled"))
    assert note.mutation_error("edit_file", {"path": "a.md"})
    note.revise([{"role": "user", "content": "수정하지 말고 검토만 해."}])
    note.accept(update(note, targets=[target("a.md", action="write")], status="active"))
    assert note.mutation_error("edit_file", {"path": "a.md"})


def test_explicit_directory_scope_is_not_a_string_prefix(tmp_path):
    note = active_note(tmp_path, 'a.md를 수정해줘. "private/"는 수정하지 마.',
                       paths=("private/a.md", "private-other/a.md"))
    assert note.mutation_error("edit_file", {"path": "private/a.md"})
    assert note.mutation_error("edit_file", {"path": "private-other/a.md"}) is None


async def test_original_memory_request_completes_with_real_edit_and_preserves_snapshot(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    old = "- 테스트 프로젝트 이름은 샘플도토리, 확인번호는 SNAP-7218이다. 이 프로젝트의 자료는 외부로 전송하지 않는다."
    new = old.replace("SNAP-7218", "SNAP-8842")
    original = "# 기존 기억\n다른 기억은 유지한다.\n\n" + old + "\n"
    source = memory / "MEMORY.md"
    source.write_text(original)
    snapshot = memory / "prompt_snapshot.json"
    snapshot.write_text('{"untouched": true}\n')
    before = snapshot.read_bytes()
    selection = [target("memory/MEMORY.md", action="write")]
    result, provider, _ = await run(tmp_path, [
        control(targets=selection, status="active", analysis_complete=False),
        call("read_file", path="memory/MEMORY.md"),
        call("edit_file", path="memory/MEMORY.md", old_text=old, new_text=new,
             intent_summary="확인번호만 변경한다.", target="memory/MEMORY.md",
             scope="persistent", reversible=True),
        call("read_file", path="memory/MEMORY.md"),
        control(targets=selection, final_answer="확인번호를 SNAP-8842로 변경했습니다."),
    ], request=MEMORY_REQUEST, write_tool=EditFileTool(workspace=tmp_path))
    assert result.stop_reason == "completed", result.final_content
    assert len(provider.calls) == 5
    assert source.read_text() == original.replace(old, new)
    assert snapshot.read_bytes() == before


async def test_unresolved_scope_returns_question_without_writing(tmp_path):
    request = "a.md를 수정해줘. 그 파일은 수정하지 마."
    question = "a.md가 수정 금지 대상인가요, 수정해도 되는 대상인가요?"
    result, _, _ = await run(tmp_path, [
        control(targets=[target("a.md", action="write")], status="active", analysis_complete=False),
        call("write_file", path="a.md", content="must not be written"),
        control(targets=[], source="none", status="clarification", analysis_complete=False,
                final_answer=question, explanation="수정 허용 대상과 금지 대상이 불명확합니다."),
    ], request=request)
    assert result.stop_reason == "incomplete"
    assert question in result.final_content
    assert not (tmp_path / "a.md").exists()


async def test_patch_with_one_protected_file_changes_neither_file(tmp_path):
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("original\n")
    question = "b.md는 수정 금지 대상입니다. a.md만 수정하면 될까요?"
    result, _, _ = await run(tmp_path, [
        control(targets=[target(p, action="write") for p in ("a.md", "b.md")],
                status="active", analysis_complete=False),
        call("apply_patch", edits=[{"path": p, "action": "add", "new_text": "unwanted"}
                                   for p in ("a.md", "b.md")],
             intent_summary="fixture", target="a.md", scope="persistent", reversible=True),
        control(targets=[], source="none", status="clarification", analysis_complete=False,
                final_answer=question, explanation="허용된 파일과 금지된 파일이 한 패치에 포함됐습니다."),
    ], request="a.md를 수정해줘. b.md는 수정하지 마.",
        write_tool=ApplyPatchTool(workspace=tmp_path))
    assert result.stop_reason == "incomplete"
    assert question in result.final_content
    assert all((tmp_path / p).read_text() == "original\n" for p in ("a.md", "b.md"))
