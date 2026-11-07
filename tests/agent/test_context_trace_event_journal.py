"""High-volume diagnostics must not block model streaming or replace call state."""
import asyncio
import json
import threading
from unittest.mock import patch

from nanobot.agent.context_trace import ContextTrace
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.task_context import TaskContextStore


def trace_at(path):
    store = ConversationEventStore(path)
    TaskContextStore(store)
    trace = ContextTrace('local-event-journal')
    trace.bind(store)
    return trace, store, trace.begin({})


async def test_thousands_of_events_do_not_rewrite_snapshots_and_survive_restart(tmp_path):
    trace, store, aid = trace_at(tmp_path)
    with patch.object(trace, 'persist', wraps=trace.persist) as persist:
        for i in range(5500):
            trace.mark('stream_event_received', attempt=aid, event_sequence=i,
                       event_type='response.output_text.delta', item_type='none')
        assert persist.call_count == 0
        await trace.flush_events()
        assert persist.call_count == 0
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM context_model_events').fetchone()[0] == 5500
    recovered = ContextTrace(trace.request_id)
    recovered.bind(store)
    recovered.recover_abandoned()
    assert recovered.calls[0]['status'] == 'result_unknown'
    assert len(recovered.calls[0]['milestones']) == 5500
    recovered.recover_abandoned()
    assert len(recovered.calls[0]['milestones']) == 5500
    # Idempotent replay cannot duplicate a journal row.
    await asyncio.to_thread(trace._append_events, [(aid, 0, trace.calls[0]['milestones'][0])])
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM context_model_events').fetchone()[0] == 5500


async def test_slow_event_database_does_not_delay_cancellation_or_overwrite_final_status(tmp_path):
    trace, store, aid = trace_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = trace._append_events

    def slow(batch):
        entered.set()
        assert release.wait(3)
        original(batch)

    trace._append_events = slow
    trace.mark('stream_event_received', attempt=aid, event_sequence=1)
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
        async def cancel_me():
            await asyncio.Event().wait()
        task = asyncio.create_task(cancel_me())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await asyncio.wait_for(task, .2)
        except asyncio.CancelledError:
            pass
        assert task.cancelled()
        trace.end(aid, None, .1, status='cancelled')
        trace.mark_timeout()
    finally:
        release.set()
        await trace.flush_events()
    with store.connect() as conn:
        row = conn.execute('SELECT status, payload FROM context_model_attempts').fetchone()
    assert row[0] == 'timed_out'
    assert json.loads(row[1])['provider_usage'] is None
    assert len(json.loads(row[1])['milestones']) == 1


async def test_failed_background_append_keeps_events_in_terminal_snapshot(tmp_path):
    trace, store, aid = trace_at(tmp_path)

    def fail(batch):
        raise OSError('synthetic storage failure')

    trace._append_events = fail
    trace.mark('stream_event_received', attempt=aid, event_sequence=1)
    await trace.flush_events()
    assert len(trace.event_queue) == 1
    trace.end(aid, None, .1, status='cancelled')
    with store.connect() as conn:
        saved = json.loads(conn.execute('SELECT payload FROM context_model_attempts').fetchone()[0])
    assert saved['status'] == 'cancelled'
    assert saved['milestones'][0]['event_sequence'] == 1


async def test_non_stream_milestones_are_queued_not_persisted_and_lifecycle_rows_are_immediate(tmp_path):
    trace, store, aid = trace_at(tmp_path)
    with store.connect() as conn:
        assert conn.execute('SELECT status FROM context_model_attempts').fetchone()[0] == 'in_flight'
    with patch.object(trace, 'persist', wraps=trace.persist) as persist, \
            patch('nanobot.agent.context_trace.json.dumps', wraps=json.dumps) as dumps:
        for phase in ('request_dispatch', 'tls_established', 'first_output', 'terminal_event',
                      'result_extracted', 'cleanup_started', 'cleanup_finished'):
            trace.mark(phase, attempt=aid, status='ok')
        # No snapshot and no serialisation on the caller's thread per milestone.
        assert persist.call_count == 0
        assert dumps.call_count == 0
    assert len(trace.event_queue) == 7
    trace.end(aid, None, .1, status='cancelled')
    with store.connect() as conn:
        status, payload = conn.execute('SELECT status, payload FROM context_model_attempts').fetchone()
    assert status == 'cancelled'
    assert [m['phase'] for m in json.loads(payload)['milestones']][-1] == 'cleanup_finished'
    await trace.flush_events()
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM context_model_events').fetchone()[0] == 7


async def test_late_milestone_after_end_is_folded_into_attempt_row_without_regressing_status(tmp_path):
    trace, store, aid = trace_at(tmp_path)
    trace.end(aid, None, .1, status='cancelled')
    trace.mark_timeout()
    trace.mark('cleanup_finished', attempt=aid, status='closed')
    await trace.flush_events()
    with store.connect() as conn:
        status, payload = conn.execute('SELECT status, payload FROM context_model_attempts').fetchone()
    assert status == 'timed_out'
    assert json.loads(payload)['milestones'][-1]['phase'] == 'cleanup_finished'


async def test_marks_do_not_block_event_loop_while_journal_write_is_slow(tmp_path):
    trace, store, aid = trace_at(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = trace._append_events

    def slow(batch):
        entered.set()
        assert release.wait(3)
        original(batch)

    trace._append_events = slow
    trace.mark('first_output', attempt=aid)
    await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for i in range(200):
        trace.mark('stream_event_received', attempt=aid, event_sequence=i)
    await asyncio.sleep(0)
    assert loop.time() - started < 0.5
    release.set()
    await trace.flush_events()
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM context_model_events').fetchone()[0] == 201
