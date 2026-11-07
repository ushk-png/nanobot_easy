"""Offline contrasts: a total-only computed spec must not certify fixed items."""
import pytest

from .test_context_t58_local import submit
from .test_context_t67_local import BASE, done, no_network  # noqa: F401
from .test_context_t68_local import run

REQ = '총 35분 연습 계획을 짜 주세요. 퍼팅 15분, 어프로치 10분, 아이언 10분으로 고정하세요.'


@pytest.mark.parametrize('route', ['direct', 'execute'])
@pytest.mark.parametrize('answer,expected_done', [
    ('퍼팅 15분, 어프로치 10분, 아이언 10분. 전체 35분.', True),
    ('퍼팅 20분, 어프로치 5분, 아이언 10분. 전체 35분.', False),
    ('퍼팅 연습에 배정한 시간은 20분\n어프로치 연습에 배정한 시간은 5분\n'
     '아이언 연습에 배정한 시간은 10분\n전체 35분.', False),
    ('퍼팅 연습에 배정한 시간은 15분\n어프로치 연습에 배정한 시간은 10분\n'
     '아이언 연습에 배정한 시간은 10분\n전체 35분.', False),
])
async def test_total_spec_does_not_replace_fixed_items(tmp_path, monkeypatch, answer, expected_done, route):
    from .t51_harness import final
    step = submit({**BASE, 'route': route, 'answer': answer if route == 'direct' else None, 'completion_checks': ['전체 합계'],
                   'completion_specs': [{'check': '전체 합계', 'result_kind': 'computed',
                                         'expected_values': {'values': {'전체': 35}, 'total': 35}}]})
    s, out, sel = await run(tmp_path, monkeypatch, step, steps=[final(answer), final(answer)], req=REQ)
    print({'done': done(s), 'calls': len(s.mock.requests), 'findings': sel.relation_answer_findings(),
           'report': sel.calculation_report()['answer_values']})
    assert done(s) == expected_done
    if '배정한' in answer:
        assert sel.relation_answer_unverified()
        assert sel.calculation_report()['answer_values'] == 'unverified'
        assert len(s.mock.requests) == (1 if route == 'direct' else 2)
