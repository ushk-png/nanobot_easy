"""Unmodified 90s model responses replayed locally; only newly allocated IDs are remapped.

Fixture tasks and original validation outcomes are retained as evidence. Replaying creates
state through the actual loop rather than replacing it with a successful synthetic state.
This does not test a model's natural-language interpretation.
"""
import json
from pathlib import Path

from nanobot.agent.context_selection import reference_check

from .t51_harness import Session
from .test_context_t58_local import submit
from .test_context_t67_local import no_network  # noqa: F401

FIX = Path(__file__).parent / 'fixtures/handoff90'


def record(name):
    return json.loads((FIX / f'LIVE-new-{name}.json').read_text())


def replay_step(rec, prior=None):
    original = rec['design_outputs'][0]['function_arguments']

    def step(messages):
        payload = json.loads(messages[1]['content'])
        rid = next(s['event_id'] for s in payload['sources'] if s.get('content_ref') == 'request')
        current_ids = {i for c in original.get('constraints', []) for i in c.get('source_ids', [])
                       if c['text'] in rec['request']}
        mapping = {old: rid for old in current_ids}
        if prior:
            old_user = prior['tasks'][0]['origin_event_id']
            new_user = next(s['event_id'] for s in payload['sources']
                            if s.get('event_type') == 'USER_MESSAGE' and s.get('content_ref') != 'request')
            mapping[old_user] = new_user
            ref = original.get('reference') or {}
            if ref.get('response_id'):
                mapping[ref['response_id']] = payload['prior_context']['previous_answer']['ref_id']
            mapping[prior['tasks'][0]['task_id']] = payload['prior_context']['current_task_id']
        raw = json.dumps(original, ensure_ascii=False)
        for old, new in mapping.items():
            raw = raw.replace(old, new)
        return submit(json.loads(raw))
    return step


async def test_replayed_current_source_kept_condition_is_accepted(tmp_path, monkeypatch):
    first, change = record('A_golf_handoff-t1'), record('A_golf_handoff-t2')
    assert change['validations'][0]['errors'][0]['code'] == 'reference_kept_condition_unoffered'
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(replay_step(first))
    assert await s.turn(first['request']) == first['reply']
    task_id = s.tasks()[0]['task_id']
    s.mock.designs.extend([replay_step(change, first), replay_step(change, first)])
    answer = await s.turn(change['request'])
    assert answer == change['design_outputs'][0]['function_arguments']['answer'], s.selected[-1].trace.reasons
    assert s.tasks()[0]['task_id'] == task_id and s.tasks()[0]['status'] == 'DONE'
    assert len(s.mock.requests) == 2  # no design repair/executor needed
    assert s.selected[-1].calculation_report()['answer_values'] == 'verified_by_relation'
    assert all(r['status'] == 'valid' for r in s.selected[-1].carried_report())


async def test_replayed_oversubscribed_unknown_scope_is_conflict(tmp_path, monkeypatch):
    rec = record('S24_conflict-t1')
    assert rec['relation_checks'][0]['status'] == 'insufficient_info'
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(replay_step(rec))
    answer = await s.turn(rec['request'])
    assert '50분' in answer and '45분' in answer
    assert s.tasks()[0]['status'] == 'IN_PROGRESS'
    assert s.selected[-1].relation_checks()[0]['status'] == 'conflict'


async def test_current_source_reference_cannot_claim_other_task_or_unsupported_span(tmp_path, monkeypatch):
    first, change = record('A_golf_handoff-t1'), record('A_golf_handoff-t2')
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(replay_step(first))
    await s.turn(first['request'])
    captured = {}
    base = replay_step(change, first)

    def save(messages):
        result = base(messages)
        captured['payload'] = json.loads(messages[1]['content'])
        captured['args'] = result.tool_calls[0].arguments
        return result
    s.mock.designs.extend([save, save])
    await s.turn(change['request'])
    from nanobot.agent.context_selection import parse_design_arguments
    for field, value in [('source_id', 'another-task-user'), ('source_span', 'not in user request')]:
        design = parse_design_arguments(captured['args'])
        setattr(design.reference.kept_conditions[-1], field, value)
        assert 'reference_kept_condition_unoffered' in reference_check(design, captured['payload'])[0]


def test_unknown_scope_surplus_is_conflict_but_shortfall_and_nesting_stay_unknown():
    from nanobot.agent.context_selection import evaluate_quantity_group, requirement_text_groups
    for text, expected in [
        ('총 45분. 퍼팅 20분, 어프로치 15분, 아이언 15분.', 'conflict'),
        ('총 45분. 퍼팅 15분, 어프로치 10분, 아이언 10분.', 'insufficient_info'),
        ('총 45분. 숏게임 30분 그중 퍼팅 20분, 아이언 20분.', 'insufficient_info'),
    ]:
        group = requirement_text_groups(text)[0]
        assert group['kind'] == 'unclear'
        assert evaluate_quantity_group(group)['status'] == expected


def test_frozen_sources_state_and_response_without_id_or_text_remapping():
    from nanobot.agent.context_selection import (
        build_prior_context,
        parse_design_arguments,
        verify_carried_conditions,
    )
    from nanobot.memory.models import RawEvent, event_to_dict

    first, change = record('A_golf_handoff-t1'), record('A_golf_handoff-t2')
    events = [RawEvent(**e) for e in json.loads((FIX / 'a_events.json').read_text())['events']]
    saved_task = first['tasks'][0]
    state = {'task_id': saved_task['task_id'],
             'constraint_source_ids': [c['event_id'] for c in saved_task['user_constraints']],
             'valid_constraint_candidates': [{'source_id': c['event_id'], 'text': c['text'],
                                              'status': 'user_original'}
                                             for c in saved_task['user_constraints']]}
    payload = {'request': change['request'], 'sources': [event_to_dict(e) for e in events],
               'task_state': state,
               'prior_context': build_prior_context(change['request'], events, events, [saved_task], [])}
    design = parse_design_arguments(change['design_outputs'][0]['function_arguments'])
    assert not reference_check(design, payload)[0]
    carried = verify_carried_conditions(design, payload)
    assert carried and all(c['status'] == 'valid' for c in carried)
    assert any(c.source_id == events[-1].event_id for c in design.reference.kept_conditions)
