"""Terminal events, extraction, disposal and cancellation: no external requests."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nanobot.providers.call_observation import _cleanup_tasks, observer
from nanobot.providers.openai_responses.parsing import (
    consume_sdk_stream,
    consume_sse_with_reasoning,
)
from nanobot.providers.xai_grok_provider import _request_xai


def terminal(status='completed', output=None):
    return {'type': 'response.' + status, 'response': {'status': status, 'output': output or [],
        'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}}


def call(arguments='{"value": 2}'):
    return {'type': 'function_call', 'call_id': 'c1', 'id': 'fc1', 'name': 'submit_context_design', 'arguments': arguments}


class Events:
    status_code = 200

    def __init__(self, events, hold=False):
        self.events = events
        self.hold = hold
        self.waiting = asyncio.Event()
        self.gate = asyncio.Event()
        self.reached_eof = False

    async def aiter_lines(self):
        for event in self.events:
            yield 'data: ' + json.dumps(event)
            yield ''
        if self.hold:
            self.waiting.set()
            await self.gate.wait()
        self.reached_eof = True


@pytest.mark.parametrize('hold', [False, True])
async def test_completed_snapshot_returns_without_waiting_for_eof(hold):
    response = Events([terminal(output=[call()])], hold=hold)
    async with asyncio.timeout(0.2):
        text, calls, status, usage, _ = await consume_sse_with_reasoning(response)
    assert calls[0].arguments == {'value': 2}
    assert status == 'stop'
    assert usage['total_tokens'] == 7
    assert not response.reached_eof


async def test_final_snapshot_replaces_partial_buffer_and_duplicate_done_items():
    item = call('{"value":')
    events = [
        {'type': 'response.output_item.added', 'item': item},
        {'type': 'response.function_call_arguments.delta', 'call_id': 'c1', 'delta': ' 1}'},
        {'type': 'response.output_item.done', 'item': call('{"value": 1}')},
        {'type': 'response.output_item.done', 'item': call('{"value": 1}')},
        terminal(output=[call('{"value": 2}')])]
    _, calls, status, _, _ = await consume_sse_with_reasoning(Events(events, hold=True))
    assert status == 'stop'
    assert len(calls) == 1
    assert calls[0].arguments == {'value': 2}


async def test_done_arguments_override_unfinished_buffer_without_final_snapshot():
    events = [{'type': 'response.output_item.added', 'item': call('{"value":')},
              {'type': 'response.output_item.done', 'item': call()},
              terminal()]
    _, calls, _, _, _ = await consume_sse_with_reasoning(Events(events))
    assert calls[0].arguments == {'value': 2}


@pytest.mark.parametrize('events', [[], [{'type': 'response.output_text.delta', 'delta': 'partial'}],
    [{'type': 'response.output_item.done', 'item': call()}]])
async def test_eof_without_terminal_event_is_not_success(events):
    with pytest.raises(RuntimeError, match='missing_terminal_event'):
        await consume_sse_with_reasoning(Events(events))


async def test_incomplete_terminal_is_not_success_and_does_not_wait_for_eof():
    _, _, reason, _, _ = await consume_sse_with_reasoning(Events([terminal('incomplete', [call()])], hold=True))
    assert reason == 'length'


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
async def test_failure_terminal_raises_without_waiting_for_eof(status):
    with pytest.raises(RuntimeError, match='Response failed'):
        await consume_sse_with_reasoning(Events([terminal(status)], hold=True))


async def test_terminal_cannot_finalize_a_partially_buffered_tool_call():
    with pytest.raises(RuntimeError, match='incomplete_tool_result'):
        await consume_sse_with_reasoning(Events([
            {'type': 'response.output_item.added', 'item': call('{"value":')}, terminal()]))


async def test_cancel_before_terminal_propagates():
    response = Events([{'type': 'response.output_text.delta', 'delta': 'partial'}], hold=True)
    task = asyncio.create_task(consume_sse_with_reasoning(response))
    await response.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_sdk_complete_snapshot_has_the_same_contract():
    async def events():
        yield SimpleNamespace(**terminal(output=[call()]))
        await asyncio.sleep(100)
    async with asyncio.timeout(0.2):
        _, calls, status, _, _ = await consume_sdk_stream(events())
    assert status == 'stop'
    assert calls[0].arguments == {'value': 2}


class FakeClient:
    def __init__(self, response, close_behavior='normal'):
        self.response = response
        self.close_behavior = close_behavior
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.stream_closed = False
        self.client_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.client_closed = True

    def stream(self, *args, **kwargs):
        client = self
        class Context:
            async def __aenter__(self):
                trace = kwargs.get('extensions', {}).get('trace')
                if trace:
                    await trace('connection.connect_tcp.complete', {})
                    await trace('connection.start_tls.complete', {})
                return client.response
            async def __aexit__(self, *args):
                client.closing.set()
                if client.close_behavior == 'hold':
                    await client.release.wait()
                if client.close_behavior == 'error':
                    raise OSError('synthetic disposal error')
                client.stream_closed = True
        return Context()


@pytest.mark.parametrize('behavior', ['normal', 'hold', 'error', 'timeout'])
async def test_http_cleanup_cannot_override_or_delay_completed_result(monkeypatch, behavior):
    client = FakeClient(Events([terminal(output=[call()])], hold=True),
                        'hold' if behavior == 'timeout' else behavior)
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', lambda **kw: client)
    if behavior == 'timeout':
        monkeypatch.setattr('nanobot.providers.call_observation.CLEANUP_TIMEOUT_SECONDS', 0.01)
    milestones = []
    token = observer.set(lambda phase, **fields: milestones.append((phase, fields)))
    try:
        async with asyncio.timeout(0.2):
            result = await _request_xai('https://synthetic.invalid', {}, {})
        assert result[1][0].arguments == {'value': 2}
        assert result[2] == 'stop'
        await client.closing.wait()
        if behavior == 'hold':
            assert not client.client_closed
            client.release.set()
        await asyncio.gather(*list(_cleanup_tasks))
        assert client.client_closed
        assert ('cleanup_finished', {'status': 'timed_out' if behavior == 'timeout' else
                'error' if behavior == 'error' else 'closed'}) in milestones
    finally:
        observer.reset(token)


async def test_cancel_after_result_during_cleanup_preserves_result(monkeypatch):
    client = FakeClient(Events([terminal(output=[call()])]), 'hold')
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', lambda **kw: client)
    task = asyncio.create_task(_request_xai('https://synthetic.invalid', {}, {}))
    result = await task
    await client.closing.wait()
    assert task.cancel() is False
    assert task.result() == result
    assert not client.client_closed
    client.release.set()
    await asyncio.gather(*list(_cleanup_tasks))


@pytest.mark.parametrize('stall', ['receive', 'callback'])
async def test_event_trace_distinguishes_receive_wait_from_handler_cancellation(stall):
    records = []
    token = observer.set(lambda phase, **fields: records.append({'phase': phase, **fields}))
    entered = asyncio.Event()

    async def callback(text):
        entered.set()
        await asyncio.Event().wait()

    source = Events([{'type': 'response.output_text.delta', 'delta': 'PRIVATE_TEXT'}], hold=True)
    task = asyncio.create_task(consume_sse_with_reasoning(
        source, on_content_delta=callback if stall == 'callback' else None))
    try:
        await asyncio.wait_for(entered.wait() if stall == 'callback' else source.waiting.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        observer.reset(token)
    processed = [r for r in records if r['phase'] == 'stream_event_processed']
    assert processed[0]['status'] == ('cancelled' if stall == 'callback' else 'processed')
    waits = [r for r in records if r['phase'] == 'stream_receive_wait_finished']
    assert waits[-1]['status'] == ('received' if stall == 'callback' else 'cancelled')
    assert 'PRIVATE_TEXT' not in json.dumps(records)


async def test_event_trace_has_item_types_and_terminal_processing_without_eof_wait():
    records = []
    token = observer.set(lambda phase, **fields: records.append({'phase': phase, **fields}))
    try:
        await consume_sse_with_reasoning(Events([
            {'type': 'response.output_item.done', 'item': {'type': 'reasoning', 'summary': []}},
            {'type': 'response.output_item.done', 'item': call()},
            terminal(output=[call()])], hold=True))
    finally:
        observer.reset(token)
    received = [r for r in records if r['phase'] == 'stream_event_received']
    processed = [r for r in records if r['phase'] == 'stream_event_processed']
    assert [r['item_type'] for r in received] == ['reasoning', 'function_call', 'none']
    assert [r['event_sequence'] for r in received] == [1, 2, 3]
    assert len(processed) == 3
    assert all(r['status'] == 'processed' and r['processing_ms'] >= 0 for r in processed)
    assert not any(r.get('event_sequence') == 4 for r in records)
