from nanobot.agent.repetition_guard import RepeatedSentenceGuard, trim_repeated_sentences

SENTENCE = "기존 사용자 홈 설정은 변경하지 않습니다."


def test_keeps_three_copies_and_drops_the_run() -> None:
    text = " ".join([SENTENCE] * 12)
    trimmed = trim_repeated_sentences(text)
    assert trimmed.count(SENTENCE) == 3


def test_stream_stops_feeding_after_the_limit() -> None:
    guard = RepeatedSentenceGuard()
    shown = "".join(guard.feed(SENTENCE + " ") for _ in range(8))
    assert shown.count(SENTENCE) == 3
    assert guard.feed(SENTENCE) == ""
    assert guard.stopped


def test_short_and_varied_text_is_unchanged() -> None:
    text = "키는 교체했습니다. 래퍼를 추가합니다. 홈 설정은 그대로 둡니다."
    assert trim_repeated_sentences(text) == text
