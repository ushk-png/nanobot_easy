"""t39 unit 5: context selection (what to read) is separate from permission (what may change)."""
from types import SimpleNamespace

from nanobot.agent.context_control import TaskControl, granted_targets


def _control(ws, *texts, **sel):
    state = {"task_id": "T1", "instruction_version": 2,
             "user_constraints": [{"event_id": f"e{i}", "sequence": i, "text": t, "kind": "user_original"}
                                  for i, t in enumerate(texts, 1)]}
    base = dict(state=state, blocked=None, current_event=None, workspace=str(ws), missing=[], evidence=[],
                observations={}, evidence_error=lambda for_mutation=True: None, trace=SimpleNamespace(request_id="r"))
    base.update(sel)
    return TaskControl(SimpleNamespace(**base))


def test_search_result_file_grants_no_change_permission(tmp_path):
    found = str(tmp_path / "secret.md")
    c = _control(tmp_path, "파일은 수정하지 마.", "이제 notes.md 파일에 적용해 줘.",
                 evidence=[{"kind": "file", "path": found, "content": "selected by search"}],
                 observations={"s1": {"name": "search_files", "status": "ok", "read_only": True, "resource_id": found}})
    assert c.authorize("write_file", found) is not None          # selected/searched, never permitted
    assert c.authorize("write_file", str(tmp_path / "notes.md")) is None   # the user's grant is honoured


def test_search_result_text_is_not_a_grant_source():
    assert granted_targets("검색 결과: secret.md, other.md") == {}


def test_selection_failure_is_not_a_prohibition(tmp_path):
    # Context selection could not find a needed source: that is missing information, not a ban.
    c = _control(tmp_path, "notes.md 파일을 수정해 줘.", missing=["background_doc"])
    assert c.authorize("write_file", str(tmp_path / "notes.md")) is None


def test_no_grant_and_no_prohibition_leaves_the_target_unrestricted_by_control(tmp_path):
    c = _control(tmp_path, "notes.md 파일을 수정해 줘.", missing=["x"],
                 evidence=[], observations={})
    assert c.authorize("write_file", str(tmp_path / "other.md")) is None   # control adds no ban of its own


def test_prohibition_comes_only_from_the_user(tmp_path):
    c = _control(tmp_path, "notes.md는 수정하지 마.")
    assert c.authorize("write_file", str(tmp_path / "notes.md")) is not None
