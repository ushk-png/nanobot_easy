from nanobot.config.schema import Config


def test_student_mode_config_aliases() -> None:
    config = Config(
        studentMode={
            "mode": "student",
            "coachName": "민서 선생님",
            "reviewTeacherName": "엘르",
            "reviewQueuePath": "study/review.jsonl",
        },
        tools={"safeMode": True},
    )

    assert config.student_mode.mode == "student"
    assert config.student_mode.coach_name == "민서 선생님"
    assert config.student_mode.review_teacher_name == "엘르"
    assert config.student_mode.review_queue_path == "study/review.jsonl"
    assert config.tools.safe_mode is True

    dumped = config.model_dump(mode="json", by_alias=True)
    assert dumped["studentMode"]["coachName"] == "민서 선생님"
    assert dumped["studentMode"]["reviewQueuePath"] == "study/review.jsonl"
    assert dumped["tools"]["safeMode"] is True
