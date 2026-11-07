"""Independent mock contrast for the follow-up path when t56 records are unavailable."""
import pytest

from .t51_harness import Session, design, final
from .test_context_t51_local import A2, CHK1, CHK2, G2, golf_turn1, spec
from .test_context_t67_local import done, no_network  # noqa: F401


@pytest.mark.asyncio
async def test_stale_first_spec_does_not_complete_followup(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    first, task_id = await golf_turn1(s)
    stale = spec(CHK2, {'퍼팅': 20, '어프로치': 20, '아이언': 20}, 60)
    wrong = design(goal='45분으로 축소', task_relation='change', task_id=task_id,
                   request_type='modify', route='execute', **stale)
    await s.turn(G2, wrong, steps=[final(first), final(first)])
    selected = s.selected[-1]
    assert selected.spec_rejections()
    assert not done(s)


@pytest.mark.asyncio
async def test_current_first_spec_completes_same_task(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    _, task_id = await golf_turn1(s)
    current = spec(CHK2, {'퍼팅': 20, '어프로치': 12.5, '아이언': 12.5}, 45)
    response = await s.turn(G2, design(goal='45분으로 축소', task_relation='change', task_id=task_id,
                                      request_type='modify', answer=A2, **current))
    assert done(s) and response == A2
    assert s.tasks()[0]['task_id'] == task_id
    assert not s.selected[-1].spec_rejections()
    assert CHK1 not in s.selected[-1].completion_reports
