"""Provider-boundary replay; real validation, task persistence and follow-up processing.

The only fixed identity is the recorded inbound user event ID, assigned at append,
not by injecting task state. Follow-up proposals and negative variants are mocks.
"""
import copy
import json
from pathlib import Path

import pytest

from nanobot.providers.base import LLMResponse, ToolCallRequest

from .t51_harness import Session, design, design_response, final
from .test_context_t67_local import no_network  # noqa: F401
from .test_followup_sequences import change_step, task

RECORD = json.loads((Path(__file__).parent / 'fixtures/conflict_route/recorded_conflict.json').read_text())
ARGS = RECORD['design_outputs'][0]['function_arguments']
EVENT_ID = ARGS['constraints'][0]['source_ids'][0]
ANSWER = '총 50분: 퍼팅 20분, 어프로치 15분, 아이언 15분.'


def session(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    persist = s.loop._persist_user_message_early

    def recorded_id(msg, state, **kwargs):
        if msg.content == RECORD['request']:
            kwargs['memory_event_id'] = EVENT_ID
        return persist(msg, state, **kwargs)

    monkeypatch.setattr(s.loop, '_persist_user_message_early', recorded_id)
    return s


def original_response():
    return LLMResponse(content=RECORD['raw_designs'][0]['raw'], finish_reason='stop', usage={},
                       tool_calls=[ToolCallRequest(id='recorded-submit', name='submit_context_design',
                                                   arguments=copy.deepcopy(ARGS))])


async def ask(s):
    response = original_response()
    s.mock.designs.append(response)
    result = await s.turn(RECORD['request'])
    assert response.tool_calls[0].arguments == ARGS
    return result


async def test_recorded_direct_conflict_becomes_bound_question_without_repair(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await ask(s)
    assert '45분' in out and '50분' in out and '조정' in out and '알려' in out
    assert out.count('사용자 조건 확인') == 1 and 'completion_evidence' not in out
    assert len(s.mock.requests) == 1  # no repair, no executor
    state = s.tasks()[0]
    assert state['status'] == 'IN_PROGRESS'
    assert state['user_constraints'][0]['text'] == RECORD['request']
    assert s.selected[-1].design.route == 'clarify'
    q = state['quantity_questions'][-1]
    assert q['task_id'] == state['task_id'] and q['instruction_version'] == state['instruction_version']
    assert q['request_event_id'] == EVENT_ID and q['response_id']
    assert q['text'] == out
    assert q['relations'][0]['status'] == 'conflict'
    assert q['relations'][0]['items'] == {'퍼팅': '20', '어프로치': '15', '아이언': '15'}
    assert q['choices'] == [{'kind': 'total', 'value': '50', 'unit': '분'},
                            {'kind': 'items', 'total': '45', 'unit': '분'}]
    sid = state['task_id']
    s.mock.designs.append(change_step(ANSWER, sid, RECORD['request'], EVENT_ID))
    assert await s.turn('총 50분으로 할게요. 항목 시간은 유지해 주세요.') == ANSWER
    state = task(s, sid)
    assert state['instruction_version'] == q['instruction_version'] + 1
    assert state['status'] == 'DONE'
    rows = s.selected[-1].relation_checks()
    assert len(rows) == 1 and rows[0]['status'] == 'pass' and rows[0]['total'] == '50'
    assert rows[0]['items'] == q['relations'][0]['items'] and rows[0]['superseded']['total'] == '45'
    assert s.selected[-1].calculation_report()['answer_values'] == 'verified_by_relation'
    assert len(s.mock.requests) == 2


@pytest.mark.parametrize('case', ['no_conflict', 'no_total', 'nested', 'mixed_units', 'non_time',
                                 'source_error', 'external_change', 'lookup', 'required_need'])
async def test_no_recovery_without_the_full_proof(tmp_path, monkeypatch, case):
    s = session(tmp_path, monkeypatch)
    args = copy.deepcopy(ARGS)
    request = RECORD['request']
    if case == 'no_conflict':
        request = request.replace('총 45분', '총 50분')
    elif case == 'no_total':
        request = request.replace('총 45분', '골프')
    elif case == 'nested':
        request = request.replace('어프로치 15분', '그중 어프로치 15분')
    elif case == 'mixed_units':
        request = request.replace('아이언 15분', '아이언 15초')
    elif case == 'non_time':
        request = request.replace('분', '원')
    elif case == 'external_change':
        request += ' 계획을 파일에 저장해 주세요.'
        args['obligations'].update(external_change='required', source_span='계획을 파일에 저장해 주세요.')
    elif case == 'lookup':
        request = request.replace('인터넷 검색은 하지 마세요.', '인터넷 검색을 해 주세요.')
        args['obligations'].update(lookup='required', source_span='인터넷 검색을 해 주세요.')
    elif case == 'required_need':
        args['information_needs'] = [{'id': 'n1', 'question': 'required context', 'required': True,
                                     'source_kind': 'memory', 'scope': 'current_task', 'query': 'context',
                                     'source_ids': [], 'acceptance': 'loaded context'}]

    def step(messages):
        payload = json.loads(messages[1]['content'])
        a = copy.deepcopy(args)
        source = next(x['event_id'] for x in payload['sources'] if x.get('content_ref') == 'request')
        a['constraints'] = [{'text': request, 'source_ids': ['foreign-event' if case == 'source_error' else source]}]
        if case in {'no_conflict', 'no_total', 'nested', 'mixed_units', 'non_time'}:
            a['obligations'] = {'target': 'answer', 'lookup': 'unneeded', 'external_change': 'none'}
        return LLMResponse(content=None, tool_calls=[ToolCallRequest(id='negative', name='submit_context_design', arguments=a)])

    s.mock.designs.extend([step, step])
    out = await s.turn(request)
    assert '완료하지 못했습니다' in out
    assert s.selected[-1].blocked == 'design_validation_failed'
    assert not s.tasks() and len(s.mock.requests) == 2
    assert 'code_quantity_conflict_to_clarify' not in s.selected[-1].trace.reasons


@pytest.mark.parametrize('wrong', [False, True])
async def test_normal_direct_and_wrong_fixed_values_keep_their_checks(tmp_path, monkeypatch, wrong):
    s = Session(tmp_path, monkeypatch)
    request = '총 35분, 퍼팅 15분, 어프로치 10분, 아이언 10분 계획을 만들어 주세요.'
    answer = ('퍼팅 20분, 어프로치 5분, 아이언 10분, 총 35분.' if wrong else
              '퍼팅 15분, 어프로치 10분, 아이언 10분, 총 35분.')
    s.mock.designs.append(design_response(design(goal='시간 계획', answer=answer,
        obligations={'target': 'answer', 'lookup': 'unneeded', 'external_change': 'none'})))
    if wrong:
        s.mock.steps.extend([final(answer), final(answer)])
    out = await s.turn(request)
    assert (s.tasks()[0]['status'] == 'DONE') is not wrong
    assert s.selected[-1].calculation_report()['answer_values'] == ('mismatch' if wrong else 'verified_by_relation')
    if not wrong:
        assert out == answer and len(s.mock.requests) == 1
    assert not s.tasks()[0].get('quantity_questions')


async def test_empty_clarify_still_rejected(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    response = design_response(design(goal='확인', route='clarify', unresolved=[], answer=None))
    s.mock.designs.extend([response, response])
    await s.turn('어떻게 할까요?')
    assert s.selected[-1].blocked == 'design_validation_failed'
    assert 'empty_clarification' in s.selected[-1].trace.reasons and not s.tasks()


@pytest.mark.parametrize('wrong', [False, True])
async def test_question_originals_survive_omitted_model_constraints(tmp_path, monkeypatch, wrong):
    s = session(tmp_path, monkeypatch)
    await ask(s)
    tid = s.tasks()[0]['task_id']
    base = change_step(ANSWER, tid, RECORD['request'], EVENT_ID)
    answer = '총 50분: 퍼팅 25분, 어프로치 10분, 아이언 15분.' if wrong else ANSWER

    def omitted(messages):
        response = base(messages)
        response.tool_calls[0].arguments.update(constraints=[], answer=answer)
        return response

    s.mock.designs.append(omitted)
    if wrong:
        s.mock.steps.extend([final(answer), final(answer)])
    await s.turn('총 50분으로 할게요. 항목 시간은 유지해 주세요.')
    assert (task(s, tid)['status'] == 'DONE') is not wrong
    assert s.selected[-1].calculation_report()['answer_values'] == ('mismatch' if wrong else 'verified_by_relation')
    assert s.selected[-1].relation_checks()[0]['items'] == {'퍼팅': '20', '어프로치': '15', '아이언': '15'}


def linked_question(tid):
    def step(messages):
        p = json.loads(messages[1]['content'])
        return design_response(design(goal='시간 조건 확인', task_relation='change', task_id=tid,
            answer=None, unresolved=['model text is not the question'],
            reference={'relation': 'modify_answer', 'task_id': tid,
                       'response_id': p['prior_context']['previous_answer']['ref_id']},
            constraints=[{'text': RECORD['request'], 'source_ids': [EVENT_ID]}],
            obligations={'target': 'prior_answer', 'lookup': 'unneeded', 'external_change': 'none',
                         'relations': [{'action': 'edit', 'target': 'prior_answer', 'necessity': 'required',
                                        'source_span': p['request'],
                                        'target_ref': p['prior_context']['previous_answer']['ref_id']}]}))
    return step


def stale_choice(tid, response_id):
    base = change_step(ANSWER, tid, RECORD['request'], EVENT_ID)

    def step(messages):
        response = base(messages)
        response.tool_calls[0].arguments['reference']['response_id'] = response_id
        return response
    return step


async def test_old_question_cannot_authorize_a_new_requirement_version(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    await ask(s)
    tid = s.tasks()[0]['task_id']
    old = s.tasks()[0]['quantity_questions'][-1]
    s.mock.designs.append(change_step(ANSWER, tid, RECORD['request'], EVENT_ID))
    await s.turn('총 50분으로 할게요. 항목 시간은 유지해 주세요.')
    s.mock.designs.append(linked_question(tid))
    question = await s.turn('총 45분으로 해 주세요. 항목 시간은 유지해 주세요.')
    assert '45분' in question and '50분' in question
    before = copy.deepcopy(task(s, tid))
    current = before['quantity_questions'][-1]
    assert current['instruction_version'] > old['instruction_version']
    stale = stale_choice(tid, old['response_id'])
    s.mock.designs.extend([stale, stale])
    await s.turn('총 50분으로 할게요. 항목 시간은 유지해 주세요.')
    assert 'quantity_question_reference_mismatch' in s.selected[-1].trace.reasons
    assert s.selected[-1].blocked == 'design_validation_failed' and task(s, tid) == before


async def test_question_of_one_task_cannot_update_another(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    await ask(s)
    original = copy.deepcopy(s.tasks()[0])
    s.mock.designs.append(design_response(design(goal='다른 작업', answer='걷기 10분, 뛰기 20분, 총 30분.')))
    await s.turn('새 작업입니다. 총 30분 운동을 걷기 10분, 뛰기 20분으로 짜 주세요.')
    other = copy.deepcopy(next(t for t in s.tasks() if t['task_id'] != original['task_id']))
    wrong = stale_choice(other['task_id'], original['quantity_questions'][0]['response_id'])
    s.mock.designs.extend([wrong, wrong])
    await s.turn('총 50분으로 할게요. 항목 시간은 유지해 주세요.')
    assert 'quantity_question_reference_mismatch' in s.selected[-1].trace.reasons
    assert task(s, original['task_id']) == original and task(s, other['task_id']) == other
