"""Phase 6 preserves original evidence while reducing exactly repeated old reference output."""

import json
from copy import deepcopy

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tool_output_projection import project_old_tool_duplicates
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse
from nanobot.utils.helpers import estimate_message_tokens

BODY = "Verified source line with an identifier REF-7728.\n" * 130


def pair(call_id, name="web_fetch", args='{"url":"https://example.com/reference"}', content=BODY):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function", "function": {"name": name, "arguments": args}}]},
        {"role": "tool", "name": name, "tool_call_id": call_id, "content": content},
    ]


def history(name="web_fetch", content=BODY):
    return [{"role": "system", "content": "No external messages."},
            {"role": "user", "content": "Old request; never overwrite reports."},
            *pair("old", name, content=content),
            {"role": "assistant", "content": "Completed old lookup."},
            {"role": "user", "content": "Read that source again."},
            *pair("recent", name, content=content),
            {"role": "assistant", "content": "Completed recent lookup."},
            {"role": "user", "content": "The previous sending permission is withdrawn."}]


def test_exact_duplicate_changes_only_old_model_copy_and_retains_all_user_conditions():
    original = history()
    before = deepcopy(original)
    projected = project_old_tool_duplicates(original, frozenset({"old", "recent"}))
    assert original == before and projected is not original
    changes = [i for i in range(len(original)) if projected[i] != original[i]]
    assert changes == [3]
    assert 'tool_call_id="recent"' in projected[3]["content"]
    assert projected[7]["content"] == BODY
    assert [m for m in projected if m["role"] != "tool"] == [m for m in original if m["role"] != "tool"]
    assert [m["tool_call_id"] for m in projected if m["role"] == "tool"] == ["old", "recent"]
    assert sum(estimate_message_tokens(m) for m in projected) < sum(estimate_message_tokens(m) for m in original)
    assert project_old_tool_duplicates(projected, frozenset({"old", "recent"})) is projected


@pytest.mark.parametrize("name", ["read_file", "exec", "write_file", "edit_file", "search_memory",
                                   "read_memory_events", "mcp_remote_fetch", "custom_fetch"])
def test_file_execution_memory_and_unknown_tools_are_never_folded(name):
    messages = history(name)
    assert project_old_tool_duplicates(messages, frozenset({"old", "recent"})) is messages


@pytest.mark.parametrize("change", ["different_args", "different_body", "short", "duplicate_id",
                                     "missing_call", "tagged_evidence", "failed", "not_historical"])
def test_uncertain_or_nonduplicate_results_remain_unchanged(change):
    messages = history()
    ids = frozenset({"old", "recent"})
    if change == "different_args":
        messages[6]["tool_calls"][0]["function"]["arguments"] = '{"url":"https://example.com/other"}'
    elif change == "different_body":
        messages[7]["content"] += "changed"
    elif change == "short":
        messages[3]["content"] = messages[7]["content"] = "short"
    elif change == "duplicate_id":
        messages[6]["tool_calls"][0]["id"] = messages[7]["tool_call_id"] = "old"
    elif change == "missing_call":
        messages[2]["tool_calls"] = []
    elif change == "tagged_evidence":
        messages[3]["source_event_id"] = "evidence-original"
    elif change == "failed":
        messages[3]["execution_status"] = "error"
    else:
        ids = frozenset()
    assert project_old_tool_duplicates(messages, ids) is messages


def test_hidden_runtime_controls_do_not_make_a_result_look_older():
    messages = history()[:5]
    messages.extend([{"role": "user", "_hidden_history": True, "content": "Runtime control."},
                     {"role": "user", "_hidden_history": True, "content": "Another control."},
                     *pair("current")])
    assert project_old_tool_duplicates(messages, frozenset({"old"})) is messages


def test_retained_content_is_stable_if_a_hook_changes_the_original_message():
    messages = history()
    projected = project_old_tool_duplicates(messages, frozenset({"old", "recent"}))
    messages[7]["content"] = "Later raw-history edit by a hook."
    assert projected[7]["content"] == BODY


def test_new_inflight_results_do_not_rewrite_an_existing_history_reference():
    messages = history()
    ids = frozenset({"old", "recent"})
    first = project_old_tool_duplicates(messages, ids)
    expanded = messages + pair("active-repeat")
    second = project_old_tool_duplicates(expanded, ids)
    assert second[3]["content"] == first[3]["content"]
    assert second[-1]["content"] == BODY


def test_current_run_output_is_protected_across_multiple_user_injections():
    messages = history()[:2] + pair("active")
    for i in range(3):
        messages.extend([{"role": "user", "content": f"Follow-up {i}"}, *pair(f"new-{i}")])
    assert project_old_tool_duplicates(messages, frozenset({"unrelated-history-id"})) is messages


async def test_runner_sends_reduced_input_but_retains_full_results_for_session_persistence(tmp_path):
    original = history()
    before = deepcopy(original)

    class Provider:
        calls = []

        async def chat_with_retry(self, **kwargs):
            self.calls.append(deepcopy(kwargs))
            return LLMResponse(content="Completed.")

    provider = Provider()
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=original, tools=ToolRegistry(), model="offline",
        max_iterations=1, max_tool_result_chars=20_000, workspace=tmp_path,
        context_window_tokens=500_000))
    assert result.stop_reason == "completed" and len(provider.calls) == 1
    sent = provider.calls[0]["messages"]
    assert "Duplicate prior" in next(m["content"] for m in sent if m.get("tool_call_id") == "old")
    assert next(m["content"] for m in sent if m.get("tool_call_id") == "recent") == BODY
    assert result.messages[:-1] == before and original == before
    assert not result.tool_events
    assert not (tmp_path / ".nanobot/tool-results").exists()
    # Saving the runner result still stores originals, never projected references.
    assert json.dumps(result.messages).count("Duplicate prior") == 0
