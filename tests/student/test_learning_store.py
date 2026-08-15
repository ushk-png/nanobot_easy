from nanobot.student import ReviewQueueStore, StudyLogEntry, append_study_log


def test_review_queue_dedupes_by_subject_and_concept(tmp_path):
    store = ReviewQueueStore(tmp_path / "review_queue.jsonl")

    first = store.upsert(
        subject="생명과학",
        concept="삼투압",
        source="day1.pdf",
        due_date="2026-07-29",
        registered_at="2026-07-26T00:00:00+00:00",
    )
    second = store.upsert(
        subject=" 생명과학 ",
        concept="삼투압",
        source="day2.pdf",
        due_date="2026-08-02",
        registered_at="2026-07-27T00:00:00+00:00",
    )

    rows = store.load()
    assert len(rows) == 1
    assert first["key"] == second["key"]
    assert rows[0]["due_date"] == "2026-08-02"
    assert len(rows[0]["review_history"]) == 2


def test_review_queue_due_date_filter(tmp_path):
    store = ReviewQueueStore(tmp_path / "review_queue.jsonl")
    store.upsert(subject="수학", concept="함수", due_date="2026-07-26")
    store.upsert(subject="수학", concept="미분", due_date="2026-07-30")

    due = store.due("2026-07-26")

    assert [row["concept"] for row in due] == ["함수"]


def test_append_study_log_writes_jsonl(tmp_path):
    path = tmp_path / "study_log.jsonl"

    append_study_log(path, StudyLogEntry(subject="과학", concept="확산", next_action="복습"))

    text = path.read_text(encoding="utf-8")
    assert '"subject":"과학"' in text
    assert '"concept":"확산"' in text
