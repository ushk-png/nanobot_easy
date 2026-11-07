"""State transitions with constructed model proposals; no claim about model interpretation."""
import json

import pytest

from .t51_harness import Session, design, design_response, final
from .test_context_t61_local import NO_SEARCH
from .test_context_t67_local import no_network  # noqa: F401
from .test_followup_record_replay import record, replay_step


def change_step(answer, task_id, original_request, source_id):
    def step(messages):
        payload = json.loads(messages[1]['content'])
        return design_response(design(
            goal='시간 계획 변경', route='direct', task_relation='change', task_id=task_id,
            answer=answer, request_type='modify', completion_checks=[],
            reference={'relation': 'modify_answer', 'task_id': task_id,
                       'response_id': payload['prior_context']['previous_answer']['ref_id'],
                       'basis_span': payload['request']},
            obligations={'target': 'prior_answer', 'lookup': 'unneeded', 'external_change': 'none',
                         'source_span': payload['request'],
                         'relations': [{'action': 'edit', 'target': 'prior_answer', 'necessity': 'required',
                                        'source_span': payload['request'],
                                        'target_ref': payload['prior_context']['previous_answer']['ref_id']}]},
            constraints=[{'text': original_request, 'source_ids': [source_id]}]))
    return step


def task(s, tid):
    return next(t for t in s.tasks() if t['task_id'] == tid)


def assert_values(sel, total, values):
    rows = sel.relation_checks()
    assert len(rows) == 1 and rows[0]['total'] == str(total) and rows[0]['status'] == 'pass'
    findings = sel.relation_answer_findings()
    assert findings[0]['status'] == 'pass'
    actual = findings[0]['answer_items'] | findings[0].get('answer_remainder_items', {})
    for name, expected in values.items():
        assert str(expected) in actual[name]


async def test_create_change_rechange_wrong_old_value_and_recover(tmp_path, monkeypatch):
    first, change = record('A_golf_handoff-t1'), record('A_golf_handoff-t2')
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(replay_step(first))
    await s.turn(first['request'])
    tid = s.tasks()[0]['task_id']
    sid = s.design_payloads()[0]['sources'][-1]['event_id']
    s.mock.designs.append(replay_step(change, first))
    await s.turn(change['request'])
    assert_values(s.selected[-1], 45, {'퍼팅': 20, '어프로치': 12.5, '아이언': 12.5})
    version = task(s, tid)['instruction_version']
    a50 = '총 50분: 퍼팅 20분, 어프로치 15분, 아이언 15분. 드라이버는 제외합니다.'
    s.mock.designs.append(change_step(a50, tid, first['request'], sid))
    assert await s.turn('총 50분으로 해 주세요. 퍼팅 시간은 유지해 주세요. 나머지는 반씩 나눠 주세요.') == a50
    assert_values(s.selected[-1], 50, {'퍼팅': 20, '어프로치': 15, '아이언': 15})
    assert task(s, tid)['instruction_version'] > version
    wrong = first['reply']
    s.mock.designs.append(change_step(wrong, tid, first['request'], sid))
    s.mock.steps.extend([final(wrong), final(wrong)])
    out = await s.turn('총 45분으로 줄여 주세요. 퍼팅 시간은 유지하고 나머지는 반씩 나눠 주세요.')
    assert '완료하지 못했습니다' in out and task(s, tid)['status'] != 'DONE'
    assert s.selected[-1].calculation_report()['answer_values'] == 'mismatch'
    assert not s.selected[-1].completion_supported()  # historical passes cannot complete this version
    assert all(c.get('calculation_report', {}).get('basis', {}).get('instruction_version')
               != task(s, tid)['instruction_version'] for c in task(s, tid)['completed'])
    a45 = change['design_outputs'][0]['function_arguments']['answer']
    s.mock.designs.append(change_step(a45, tid, first['request'], sid))
    assert await s.turn('총 45분으로 줄여 주세요. 퍼팅 시간은 유지하고 나머지는 반씩 나눠 주세요.') == a45
    assert task(s, tid)['status'] == 'DONE'
    assert_values(s.selected[-1], 45, {'퍼팅': 20, '어프로치': 12.5, '아이언': 12.5})


async def test_conflict_choice_rechange_choice_and_other_task(tmp_path, monkeypatch):
    original = record('S24_conflict-t1')
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(replay_step(original))
    out = await s.turn(original['request'])
    assert '50분' in out and '45분' in out
    tid = s.tasks()[0]['task_id']
    sid = s.design_payloads()[0]['sources'][-1]['event_id']
    a50 = '총 50분: 퍼팅 20분, 어프로치 15분, 아이언 15분.'
    for prompt in ('총 50분으로 할게요. 항목 시간은 유지해 주세요.',
                   '총 45분으로 다시 바꾸되 항목 시간은 그대로 유지해 주세요.',
                   '총 50분으로 선택합니다. 항목 시간은 유지해 주세요.'):
        s.mock.designs.append(change_step(a50, tid, original['request'], sid))
        answer = await s.turn(prompt)
        if '45분' in prompt:
            assert '확인이 필요합니다' in answer and task(s, tid)['status'] != 'DONE'
            assert s.selected[-1].relation_checks()[0]['status'] == 'conflict'
            assert not s.selected[-1].completion_supported()
            assert all(c.get('calculation_report', {}).get('basis', {}).get('instruction_version')
                       != task(s, tid)['instruction_version'] for c in task(s, tid)['completed'])
        else:
            assert answer == a50 and task(s, tid)['status'] == 'DONE'
            assert_values(s.selected[-1], 50, {'퍼팅': 20, '어프로치': 15, '아이언': 15})
    before = json.loads(json.dumps(task(s, tid)))
    answer = '걷기 10분, 뛰기 20분, 총 30분.'
    s.mock.designs.append(design_response(design(goal='다른 작업', answer=answer, obligations=NO_SEARCH)))
    await s.turn('새 작업입니다. 총 30분 운동을 걷기 10분, 뛰기 20분으로 짜 주세요.')
    assert len(s.tasks()) == 2
    assert_values(s.selected[-1], 30, {'걷기': 10, '뛰기': 20})
    assert task(s, tid) == before


@pytest.mark.parametrize('putting,approach,iron,done', [(15, 10, 10, True), (20, 5, 10, False)])
async def test_same_total_does_not_replace_fixed_item_oracle(tmp_path, monkeypatch, putting, approach, iron, done):
    s = Session(tmp_path, monkeypatch)
    req = '총 35분, 퍼팅 15분, 어프로치 10분, 아이언 10분 계획을 만들어 주세요.'
    answer = f'퍼팅 {putting}분, 어프로치 {approach}분, 아이언 {iron}분, 총 35분.'
    s.mock.designs.append(design_response(design(goal='계획', route='execute', obligations=NO_SEARCH)))
    s.mock.steps.extend([final(answer), final(answer)])
    await s.turn(req)
    assert (s.tasks()[0]['status'] == 'DONE') is done
    assert s.selected[-1].calculation_report()['answer_values'] == ('verified_by_relation' if done else 'mismatch')
