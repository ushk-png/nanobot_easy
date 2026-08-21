"""Tests for ``goal_state`` session metadata helpers."""

from __future__ import annotations

from nanobot.session.goal_state import (
    GOAL_STATE_KEY,
    discard_legacy_goal_state_key,
    goal_state_runtime_lines,
    goal_state_ws_blob,
    mark_sustained_goal_user_approval,
    message_confirms_sustained_goal,
    objective_requires_user_approval,
    parse_goal_state,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
    sustained_goal_waits_for_user,
)
from nanobot.session.manager import SessionManager
from nanobot.session.turn_continuation import (
    should_finalize_on_max_iterations,
    should_route_followup_to_pending,
)


def test_runtime_lines_empty_when_no_metadata():
    assert goal_state_runtime_lines(None) == []
    assert goal_state_runtime_lines({}) == []


def test_runtime_lines_empty_when_completed():
    meta = {
        GOAL_STATE_KEY: {"status": "completed", "objective": "was doing X"},
    }
    assert goal_state_runtime_lines(meta) == []


def test_runtime_lines_include_objective_when_active():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Ship the fix.",
            "ui_summary": "fix",
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert "Goal (active):" in lines
    assert "Ship the fix." in lines
    assert any("Summary: fix" in ln for ln in lines)


def test_runtime_lines_mark_approval_gated_goal_as_waiting():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Create a draft only after confirming with the user.",
            "requires_user_approval": True,
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert "Goal (waiting for explicit user approval):" in lines
    assert any("Do not continue this goal" in line for line in lines)
    assert "Goal (active):" not in lines


def test_runtime_lines_read_legacy_thread_goal_key():
    meta = {"thread_goal": {"status": "active", "objective": "Legacy key.", "ui_summary": "L"}}
    lines = goal_state_runtime_lines(meta)
    assert "Legacy key." in lines


def test_goal_state_key_takes_precedence_over_legacy():
    meta = {
        GOAL_STATE_KEY: {"status": "active", "objective": "New key wins.", "ui_summary": "n"},
        "thread_goal": {"status": "active", "objective": "Ignored.", "ui_summary": "o"},
    }
    lines = goal_state_runtime_lines(meta)
    assert "New key wins." in lines
    assert "Ignored." not in "".join(lines)


def test_discard_legacy_goal_state_key():
    meta: dict = {"thread_goal": {"x": 1}, GOAL_STATE_KEY: {"status": "active"}}
    discard_legacy_goal_state_key(meta)
    assert "thread_goal" not in meta
    assert GOAL_STATE_KEY in meta


def test_parse_goal_state_accepts_json_string():
    assert parse_goal_state('{"status":"active","objective":"x"}') == {
        "status": "active",
        "objective": "x",
    }


def test_sustained_goal_waits_for_user_detects_staged_approval_objective():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": (
                "Create the draft step-by-step. Step 1 reports overlap; "
                "later steps require explicit user instruction before proceeding."
            ),
        },
    }
    assert sustained_goal_waits_for_user(meta) is True


def test_sustained_goal_waits_for_user_ignores_normal_active_goal():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Implement the feature and run focused tests.",
        },
    }
    assert sustained_goal_waits_for_user(meta) is False


def test_manual_approval_goal_disables_internal_continuation():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": (
                "1단계를 수행하고 보고한다. 다음 단계는 사용자가 명시적으로 "
                "2를 수행해줘라고 지시할 때만 진행한다."
            ),
        },
    }

    assert should_finalize_on_max_iterations(
        pending_queue_available=True,
        session_metadata=meta,
        message_metadata={},
    ) is True


def test_only_after_confirming_goal_requires_user_approval():
    objective = (
        "Implement the first-stage standalone prototype, but only after confirming "
        "the exact implementation target with the user."
    )
    assert objective_requires_user_approval(objective) is True


def test_korean_grok_build_approval_phrase_requires_user_approval():
    objective = "구현 승인 대기 상태다. 진행 문구: 그록 빌드로 진행해"
    assert objective_requires_user_approval(objective) is True


def test_wait_for_explicit_approval_before_editing_requires_user_approval():
    objective = (
        "Modify nanobot skills after user approval. First explain the proposed "
        "skill content and wait for explicit approval before editing files or "
        "creating drafts."
    )
    assert objective_requires_user_approval(objective) is True


def test_approval_gated_goal_bypasses_pending_queue():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Implement only after confirming the user's approval before proceeding.",
            "requires_user_approval": True,
        },
    }
    assert should_route_followup_to_pending(meta) is False


def test_user_approval_message_reopens_sustained_goal_continuation():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Implement only after confirming the user's approval before proceeding.",
            "requires_user_approval": True,
        },
    }
    assert mark_sustained_goal_user_approval(meta, "그록 빌드로 진행해") is True
    assert meta[GOAL_STATE_KEY]["user_approval_received"] is True
    assert sustained_goal_waits_for_user(meta) is False
    assert should_route_followup_to_pending(meta) is True


def test_runtime_lines_mark_approved_gate_without_reasking():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Install after explicit approval.",
            "requires_user_approval": True,
        },
    }
    assert mark_sustained_goal_user_approval(meta, "진행해줘") is True

    lines = "\n".join(goal_state_runtime_lines(meta))

    assert "Goal (active):" in lines
    assert "same approval again" in lines
    assert "Goal (waiting for explicit user approval):" not in lines


def test_non_approval_question_does_not_reopen_sustained_goal():
    assert message_confirms_sustained_goal("왜 진행해야 해?") is False


def test_goal_state_ws_blob_inactive_when_missing_or_completed():
    assert goal_state_ws_blob(None) == {"active": False}
    assert goal_state_ws_blob({}) == {"active": False}
    assert goal_state_ws_blob({GOAL_STATE_KEY: {"status": "completed", "objective": "x"}}) == {
        "active": False,
    }


def test_goal_state_ws_blob_active_shape():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Build feature.",
            "ui_summary": "feat",
        },
    }
    assert goal_state_ws_blob(meta) == {
        "active": True,
        "waiting_for_user": False,
        "ui_summary": "feat",
        "objective": "Build feature.",
    }


def test_sustained_goal_active_false_when_missing_or_completed():
    assert sustained_goal_active(None) is False
    assert sustained_goal_active({}) is False
    assert sustained_goal_active({GOAL_STATE_KEY: {"status": "completed", "objective": "x"}}) is False


def test_sustained_goal_active_true_when_active():
    meta = {GOAL_STATE_KEY: {"status": "active", "objective": "Run long task."}}
    assert sustained_goal_active(meta) is True


def test_sustained_goal_active_respects_legacy_thread_goal_key():
    meta = {"thread_goal": {"status": "active", "objective": "Legacy."}}
    assert sustained_goal_active(meta) is True


def test_runner_wall_llm_timeout_uses_metadata_override(tmp_path):
    sm = SessionManager(tmp_path)
    assert (
        runner_wall_llm_timeout_s(
            sm,
            "cli:test",
            metadata={GOAL_STATE_KEY: {"status": "active", "objective": "x"}},
        )
        == 0.0
    )
    assert runner_wall_llm_timeout_s(sm, "cli:test", metadata={}) is None


def test_runner_wall_llm_timeout_reads_session_when_metadata_missing(tmp_path):
    sm = SessionManager(tmp_path)
    sess = sm.get_or_create("c:d")
    sess.metadata = {GOAL_STATE_KEY: {"status": "active", "objective": "z"}}
    assert runner_wall_llm_timeout_s(sm, "c:d") == 0.0
    sess.metadata = {}
    assert runner_wall_llm_timeout_s(sm, "c:d") is None
