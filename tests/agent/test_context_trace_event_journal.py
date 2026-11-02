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
