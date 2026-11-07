"""t39 unit 6: ITEMS patterns only extract candidates / check missing items."""
from types import SimpleNamespace

import pytest

from nanobot.agent.context_items import item_coverage, need_items, requested_items
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401
from .test_context_t37_museum import museum


def test_unrecognized_wording_does_not_delete_a_requirement(monkeypatch):
    sel, _ = museum(monkeypatch)
    sel.current_event = SimpleNamespace(content="그 박물관 다녀오기 전에 알아둘 것 정리해 줘", sequence=5, event_id="u5")
    sel._relations = None
    assert requested_items(sel.request_text) == []          # nothing recognised
    need = sel.design.information_needs[0]
    assert need_items(sel.request_text, need, sel.design.information_needs) == []
    reasons = sel.unmet_reasons()
    assert any(r.startswith("information_missing:" + need.id) for r in reasons if need.required)
    assert not sel.completion_supported()


def test_recognised_item_is_only_a_missing_item_check():
    cov = item_coverage("관람료 알려줘", {}, set())
    assert cov == {"fee": {"status": "missing"}}


@pytest.mark.asyncio
async def test_matched_word_alone_creates_no_obligation(env):
    text = "'관람료'와 '휴관'이라는 단어의 차이를 설명해 줘"
    event(env, text)
    _, _, sel = await select(env, plan(answer="관람료는 요금, 휴관은 쉬는 날"), request=text)   # MOCK design
    assert sel.design.information_needs == [] and sel.required_results()[0]["kind"] == "answer"
    assert not sel.relations().requires_work and sel.completion_supported()


@pytest.mark.asyncio
async def test_matched_word_alone_blocks_nothing(env):
    text = "관람료 계산법 설명해 줘: 성인 2명 x 5000원"
    event(env, text)
    _, _, sel = await select(env, plan(answer="10000원"), request=text)   # MOCK design
    assert sel.blocked is None and sel.completion_supported()
