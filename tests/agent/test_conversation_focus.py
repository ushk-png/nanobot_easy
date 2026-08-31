from __future__ import annotations

import json
from pathlib import Path

from nanobot.session.conversation_focus import (
    CONVERSATION_FOCUS_KEY,
    CORRECTION_LOG_RELATIVE_PATH,
    focus_runtime_lines,
    has_referential_text,
    is_correction_text,
    update_conversation_focus,
)


def test_korean_referential_text_is_detected() -> None:
    assert has_referential_text("그거 다시 확인해줘") is True
    assert has_referential_text("오늘 날씨 알려줘") is False


def test_korean_correction_text_is_detected() -> None:
    assert is_correction_text("내 말은 포맷 문제라는 거야") is True
    assert is_correction_text("요약해줘") is False


def test_focus_tracks_previous_referent_for_short_followup(tmp_path: Path) -> None:
    metadata = {}
    history = [
        {"role": "user", "content": "PageAgent 설치 관련 전체 삭제하자. 릴레이는 삭제하지 말고."},
        {"role": "assistant", "content": "PageAgent 설정을 제거했습니다."},
    ]

    focus = update_conversation_focus(
        metadata,
        user_text="다 수행했어?",
        history=history,
        workspace=tmp_path,
        session_key="telegram:1",
    )

    assert metadata[CONVERSATION_FOCUS_KEY] is focus
    assert focus["slots"]["action"] == "verify"
    labels = [r["label"] for r in focus["last_referents"]]
    assert any("PageAgent" in label for label in labels)
    assert focus["confidence"]["level"] in {"medium", "high"}


def test_focus_preserves_objective_on_referential_followup(tmp_path: Path) -> None:
    metadata = {
        CONVERSATION_FOCUS_KEY: {
            "objective": "PageAgent remove",
            "last_referents": [{"label": "PageAgent", "type": "topic"}],
        }
    }

    focus = update_conversation_focus(
        metadata,
        user_text="그거 다시 해줘",
        history=[],
        workspace=tmp_path,
        session_key="telegram:1",
    )

    assert focus["objective"] == "PageAgent remove"
    assert focus["slots"]["target"] == "PageAgent"


def test_correction_turn_is_logged(tmp_path: Path) -> None:
    metadata = {
        CONVERSATION_FOCUS_KEY: {
            "objective": "일정 내용 확인",
            "current_intent": "일정 조회",
        }
    }
    focus = update_conversation_focus(
        metadata,
        user_text="내 말은 포맷이 문제라는 얘기야",
        history=[{"role": "assistant", "content": "일정이 없습니다."}],
        workspace=tmp_path,
        session_key="telegram:1",
    )

    path = tmp_path / CORRECTION_LOG_RELATIVE_PATH
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["trigger"] == "user_correction"
    assert record["session_key"] == "telegram:1"
    assert record["new_focus"]["objective"] == focus["objective"]


def test_runtime_lines_are_compact_metadata() -> None:
    metadata = {
        CONVERSATION_FOCUS_KEY: {
            "objective": "PageAgent 관련 설치/설정 제거",
            "current_intent": "삭제 완료 여부 확인",
            "last_referents": [{"label": ".local/config.json:mcpServers.page-agent", "type": "config_block"}],
            "missing_slots": [],
            "confidence": {"level": "high", "reason": "직전 작업 완료 여부를 물음"},
            "clarification_policy": "저위험 답변은 가장 그럴듯한 해석으로 진행하고 가정 명시",
        }
    }

    lines = focus_runtime_lines(metadata)

    joined = "\n".join(lines)
    assert "Conversation Focus Snapshot" in joined
    assert "metadata only" in joined
    assert "Objective: PageAgent" in joined
    assert len(joined) < 1000


def test_agent_b_schedule_followup_tracks_event_and_requires_confirmation(tmp_path: Path) -> None:
    metadata = {}
    history = [
        {"role": "user", "content": "다음 주 회의 일정 보여줘"},
        {"role": "assistant", "content": "다음 주 회의 일정은 월요일 10시입니다."},
    ]

    focus = update_conversation_focus(
        metadata,
        user_text="그 일정 취소해줘",
        history=history,
        workspace=tmp_path,
        session_key="telegram:agent_b",
    )

    assert focus["slots"]["action"] == "remove"
    assert focus["slots"]["domain"] == "schedule"
    assert focus["slots"]["target_type"] in {"schedule_event", "schedule_range", "topic"}
    assert "일정" in focus["clarification_policy"]


def test_agent_a_japanese_sentence_followup_tracks_learning_slots(tmp_path: Path) -> None:
    metadata = {}
    history = [
        {"role": "assistant", "content": "1. 今日は天気がいいです。\n발음: 쿄오와 텐키가 이이데스\n뜻: 오늘은 날씨가 좋아요."},
    ]

    focus = update_conversation_focus(
        metadata,
        user_text="그 문장 다시 읽어줘. 이번엔 천천히",
        history=history,
        workspace=tmp_path,
        session_key="telegram:agent_a",
    )

    assert focus["slots"]["action"] == "repeat"
    assert focus["slots"]["domain"] == "japanese_learning"
    assert focus["slots"]["voice_mode"] == "slow"
    assert any(ref["type"] in {"japanese_sentence", "learning_topic"} for ref in focus["last_referents"])


def test_agent_a_current_japanese_text_is_extracted(tmp_path: Path) -> None:
    metadata = {}

    focus = update_conversation_focus(
        metadata,
        user_text="今日は天気がいいです 뜻만 알려줘",
        history=[],
        workspace=tmp_path,
        session_key="telegram:agent_a",
    )

    assert focus["slots"]["domain"] == "japanese_learning"
    assert focus["slots"]["response_part"] == "korean_meaning_only"
    assert "今日は天気" in focus["slots"]["current_japanese_sentence"]
