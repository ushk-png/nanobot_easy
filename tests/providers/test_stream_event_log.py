"""Lightweight, content-free stream event log (one summary milestone per stream)."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from nanobot.providers import call_observation
from nanobot.providers.call_observation import StreamEventLog, observer
from nanobot.providers.openai_responses import parsing
from nanobot.providers.openai_responses.parsing import consume_sse_with_reasoning
from tests.agent.test_context_selection import env as env  # noqa: F401
from tests.agent.test_context_selection import event
from tests.providers.test_response_stream_lifecycle import Events, FakeClient, call, terminal

SECRET = 'PRIVATE_PAYLOAD_TEXT'


def fc_stream(deltas=5):
    item = {**call(), 'arguments': ''}
    return [
        {'type': 'response.created', 'response': {'status': 'in_progress'}},
        {'type': 'response.output_item.added', 'item': {'type': 'reasoning', 'id': 'rs1', 'summary': []}},
        {'type': 'response.output_item.done', 'item': {'type': 'reasoning', 'id': 'rs1', 'summary': []}},
        {'type': 'response.output_item.added', 'item': item},
        *[{'type': 'response.function_call_arguments.delta', 'call_id': 'c1', 'item_id': 'fc1',
           'delta': SECRET} for _ in range(deltas)],
        {'type': 'response.function_call_arguments.done', 'call_id': 'c1', 'arguments': '{"value": 2}'},
        {'type': 'response.output_item.done', 'item': call()},
        terminal(output=[call()]),
    ]


async def run(events, hold=False):
    records = []
    token = observer.set(lambda phase, **fields: records.append({'phase': phase, **fields}))
    try:
        result = await consume_sse_with_reasoning(Events(events, hold=hold))
    finally:
        observer.reset(token)
    return result, records


def logs(records):
    return [r for r in records if r['phase'] == 'stream_event_log']


async def test_event_and_item_types_are_recorded_in_order_without_payload():
    result, records = await run(fc_stream())
    assert result[1][0].arguments == {'value': 2}
    [entry] = logs(records)
    assert entry['status'] == 'completed' and entry['count'] == 12
    log = entry['stream_log']
    assert [e[1:3] for e in log['events']] == [
        ['created', 'none'], ['output_item.added', 'reasoning'], ['output_item.done', 'reasoning'],
        ['output_item.added', 'function_call'], ['function_call_arguments.delta', 'function_call'],
        ['function_call_arguments.done', 'none'], ['output_item.done', 'function_call'], ['completed', 'none']]
    offsets = [e[3] for e in log['events']]
    assert offsets == sorted(offsets) and all(o >= 0 for o in offsets)
    assert log['items_added'] == {'reasoning': 1, 'function_call': 1}
    assert log['items_done'] == {'reasoning': 1, 'function_call': 1}
    [fc] = log['function_calls']
    added, args_first, args_done, item_done, deltas = fc
    assert added <= args_first <= args_done <= item_done and deltas == 5
    assert log['function_args_started'] == log['function_args_completed'] == 1
    assert log['last_event'][:2] == ['completed', 'none']
    assert -0.001 <= time.time() - log['call_started_wall'] < 60  # rounded to ms
    dumped = json.dumps(records)
    assert SECRET not in dumped and 'c1' not in json.dumps(log) and 'fc1' not in json.dumps(log)


async def test_event_sequence_numbers_follow_stream_order():
    _, records = await run(fc_stream(deltas=3))
    log = logs(records)[0]['stream_log']
    assert [e[0] for e in log['events']] == [1, 2, 3, 4, 5, 8, 9, 10]
    assert log['events_seen'] == 10


async def test_high_frequency_deltas_are_coalesced_per_item():
    events = [{'type': 'response.output_text.delta', 'item_id': 'msg1', 'output_index': 0, 'delta': 'x'}
              for _ in range(10_000)]
    _, records = await run(events + [terminal()])
    log = logs(records)[0]['stream_log']
    assert [e[1] for e in log['events']] == ['output_text.delta', 'completed']
    [series] = log['deltas']
    kind, item_type, first, last, count = series
    assert (kind, item_type, count) == ('output_text.delta', 'message', 10_000) and first <= last
    assert log['dropped'] == 0
    assert len(json.dumps(log)) < 2_000


def test_per_call_caps_bound_memory_and_count_drops():
    log = StreamEventLog()
    for i in range(1_000):
        log.record({'type': 'response.output_item.added', 'item': {'type': 'message', 'id': f'm{i}'}})
        log.record({'type': 'response.output_text.delta', 'item_id': f'm{i}', 'delta': 'x'})
        log.record({'type': 'response.function_call_arguments.delta', 'call_id': f'c{i}', 'delta': 'x'})
    summary = log.summary()
    assert len(summary['events']) == call_observation.STREAM_LOG_MAX_EVENTS
    assert len(summary['deltas']) == call_observation.STREAM_LOG_MAX_DELTA_KEYS
    assert len(summary['function_calls']) == call_observation.STREAM_LOG_MAX_FUNCTION_CALLS
    assert summary['dropped'] > 0 and summary['events_seen'] == 3_000
    assert len(json.dumps(summary)) < 40_000


def test_unknown_event_and_item_types_are_not_copied():
    log = StreamEventLog()
    log.record({'type': SECRET, 'item': {'type': SECRET}})
    assert SECRET not in json.dumps(log.summary())
    assert log.summary()['events'][0][1:3] == ['unknown', 'other']


async def test_no_serialisation_or_extra_milestone_per_event(monkeypatch):
    dumps = []

    class CountingJson:
        def __getattr__(self, name):
            return getattr(json, name)

        def dumps(self, *a, **k):
            dumps.append(1)
            return json.dumps(*a, **k)

    # Only the parser's own serialisation is counted (not the fake SSE source).
    monkeypatch.setattr(parsing, 'json', CountingJson())
    for n in (10, 2_000):
        dumps.clear()
        events = [{'type': 'response.output_text.delta', 'item_id': 'm', 'delta': 'x'} for _ in range(n)]
        _, records = await run(events + [terminal()])
        # first_event / first_output sizes and the result hash only: independent of n.
        assert len(dumps) <= 3
        assert len(logs(records)) == 1


async def test_no_recorder_without_an_observer(monkeypatch):
    created = []
    monkeypatch.setattr(parsing, 'stream_event_log', lambda: created.append(1))
    await consume_sse_with_reasoning(Events([terminal()]))
    assert created == [1]
    assert call_observation.stream_event_log() is None


@pytest.mark.parametrize('ending,status', [
    ([{'type': 'response.failed'}], 'failed'),
    ([], 'eof'),
])
async def test_summary_is_emitted_on_failure_and_eof(ending, status):
    with pytest.raises(RuntimeError):
        await run([{'type': 'response.created'}] + ending)
    records = []
    token = observer.set(lambda phase, **fields: records.append({'phase': phase, **fields}))
    try:
        with pytest.raises(RuntimeError):
            await consume_sse_with_reasoning(Events([{'type': 'response.created'}] + ending))
    finally:
        observer.reset(token)
    [entry] = logs(records)
    assert entry['status'] == status


async def test_timeout_progress_shows_how_far_the_stream_got():
    records = []
    token = observer.set(lambda phase, **fields: records.append({'phase': phase, **fields}))
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await consume_sse_with_reasoning(Events(fc_stream(deltas=4)[:8], hold=True))
    finally:
        observer.reset(token)
    [entry] = logs(records)
    log = entry['stream_log']
    assert entry['status'] == 'cancelled'
    assert log['last_event'][:2] == ['function_call_arguments.delta', 'function_call']
    assert log['items_added'] == {'reasoning': 1, 'function_call': 1}
    assert log['items_done'] == {'reasoning': 1}
    assert log['function_args_started'] == 1 and log['function_args_completed'] == 0
    assert log['since_last_event_ms'] >= 150
    assert log['function_calls'][0][4] == 4


def test_record_overhead_is_bounded_for_10k_events():
    events = ([{'type': 'response.output_item.added', 'item': {'type': 'message', 'id': 'm'}}]
              + [{'type': 'response.output_text.delta', 'item_id': 'm', 'delta': 'x'}] * 10_000)
    log = StreamEventLog()
    started = time.perf_counter()
    for e in events:
        log.record(e)
    log.summary()
    elapsed = time.perf_counter() - started
    # Typically ~10 ms; generous bound for slow CI.
    assert elapsed < 0.5


async def test_design_deadline_journals_progress_summary_off_loop(env, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from nanobot.agent.context_selection import ContextSelector
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.call_observation import _cleanup_tasks
    from nanobot.providers.xai_grok_provider import XAIGrokProvider

    client = FakeClient(Events(fc_stream(deltas=4)[:8], hold=True))
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', lambda **kw: client)
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.get_xai_oauth_token',
                        lambda **kw: SimpleNamespace(access='synthetic-token'))
    event(env, '2+2?')
    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True, design_timeout_seconds=1))
    selected = await selector.select(provider=XAIGrokProvider(hosted_search=False),
        model='xai-grok/grok-4.7', request='2+2?', scope=env[1], session_id='s',
        registry=env[2], skills=env[3], rules='Mandatory permissions', request_id='mock-stream-log')
    assert selected.blocked == 'design_timeout_initial' and selected.design is None
    call_row = selected.trace.calls[0]
    assert call_row['status'] == 'timed_out'
    phases = [m['phase'] for m in call_row['milestones']]
    assert phases.count('stream_event_log') == 1
    assert phases.index('stream_event_log') < phases.index('design_deadline_expired')
    await selected.trace.flush_events()
    with env[0].connect() as db:
        rows = db.execute(
            "SELECT a.status, json_extract(e.payload, '$.status'),"
            " json_extract(e.payload, '$.stream_log.last_event[0]'),"
            " json_extract(e.payload, '$.stream_log.function_args_started'),"
            " json_extract(e.payload, '$.stream_log.function_args_completed')"
            " FROM context_model_events e JOIN context_model_attempts a USING(attempt_id)"
            " WHERE a.request_id='mock-stream-log'"
            " AND json_extract(e.payload, '$.phase')='stream_event_log'").fetchall()
    assert [tuple(r) for r in rows] == [('timed_out', 'cancelled', 'function_call_arguments.delta', 1, 0)]
    await asyncio.gather(*list(_cleanup_tasks))
