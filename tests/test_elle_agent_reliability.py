import importlib.util
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ELLE_WS = REPO / ".local" / "workspace-japanese-sentence-coach"
RELIABILITY_PATH = ELLE_WS / "scripts" / "agent_reliability.py"
DAILY_SCRIPT = ELLE_WS / "scripts" / "daily_japanese_study.py"


def load_reliability_module():
    spec = importlib.util.spec_from_file_location("agent_reliability", RELIABILITY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["agent_reliability"] = module
    spec.loader.exec_module(module)
    return module


def write_learning_data(path: Path, count: int = 7) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "language": "ja",
                "mode": "beginner_sentence_spaced_repetition",
                "daily_new_sentence_count": count,
                "review_intervals_days": [0, 1, 3, 7, 14, 30],
                "sentences": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_request_intake_is_idempotent(tmp_path):
    reliability = load_reliability_module()
    conn = reliability.connect(tmp_path / "reliability.sqlite3")
    first = reliability.submit_request(
        conn,
        request_id="req-1",
        agent="elle",
        task="daily_japanese_sentence",
        source_message_id="telegram:1:100",
        source_ref="attachment-a",
        request_kind="vocabulary_sentences",
        target_date="2026-09-18",
        timezone="Asia/Seoul",
        constraints={"words": ["切符"]},
    )
    second = reliability.submit_request(
        conn,
        request_id="req-1",
        agent="elle",
        task="daily_japanese_sentence",
        source_message_id="telegram:1:100",
        source_ref="attachment-a",
        request_kind="vocabulary_sentences",
        target_date="2026-09-18",
        timezone="Asia/Seoul",
        constraints={"words": ["切符"]},
    )
    assert first["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_daily_script_records_validation_and_fake_outbox(tmp_path):
    data = tmp_path / "learning.json"
    out = tmp_path / "out"
    vocab = tmp_path / "vocab.json"
    db = tmp_path / "reliability.sqlite3"
    write_learning_data(data)
    vocab.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requests": [
                    {
                        "id": "req-2026-09-18",
                        "target_date": "2026-09-18",
                        "status": "ready",
                        "words": ["切符"],
                        "sentences": [
                            {
                                "japanese": "駅で切符を買います。",
                                "pronunciation_ko": "에키데 킷푸오 카이마스.",
                                "meaning_ko": "역에서 표를 삽니다.",
                                "required_words": ["切符"],
                            },
                            {
                                "japanese": "ホテルで地図をもらいます。",
                                "pronunciation_ko": "호테루데 치즈오 모라이마스.",
                                "meaning_ko": "호텔에서 지도를 받습니다.",
                            },
                            {
                                "japanese": "空港で水を買います。",
                                "pronunciation_ko": "쿠-코-데 미즈오 카이마스.",
                                "meaning_ko": "공항에서 물을 삽니다.",
                            },
                            {
                                "japanese": "駅員さんに聞きます。",
                                "pronunciation_ko": "에키인산니 키키마스.",
                                "meaning_ko": "역무원에게 물어봅니다.",
                            },
                            {
                                "japanese": "バス停で待ちます。",
                                "pronunciation_ko": "바스테-데 마치마스.",
                                "meaning_ko": "버스 정류장에서 기다립니다.",
                            },
                            {
                                "japanese": "カフェで休みます。",
                                "pronunciation_ko": "카페데 야스미마스.",
                                "meaning_ko": "카페에서 쉽니다.",
                            },
                            {
                                "japanese": "店でお土産を選びます。",
                                "pronunciation_ko": "미세데 오미야게오 에라비마스.",
                                "meaning_ko": "가게에서 기념품을 고릅니다.",
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(DAILY_SCRIPT),
            "--date",
            "2026-09-18",
            "--data",
            str(data),
            "--out-dir",
            str(out),
            "--vocab-requests",
            str(vocab),
            "--reliability-db",
            str(db),
        ],
        cwd=ELLE_WS,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    audit = json.loads(result.stdout)
    assert audit["reliability_validated"] is True
    assert audit["reliability_outbox_status"] == "fake_ready"
    audio = json.loads((out / "2026-09-18_sentences.json").read_text(encoding="utf-8"))
    assert audio["items"][0]["sentence_id"] == "ja-20260918-001"

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM executions WHERE status='fake_delivery_ready'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM validations WHERE status='failed'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE status='fake_ready' AND fake=1").fetchone()[0] == 1


def test_daily_script_blocks_missing_required_word(tmp_path):
    data = tmp_path / "learning.json"
    out = tmp_path / "out"
    vocab = tmp_path / "vocab.json"
    db = tmp_path / "reliability.sqlite3"
    write_learning_data(data)
    sentences = []
    for i in range(7):
        sentences.append(
            {
                "japanese": f"ホテルで水を買います{i}。",
                "pronunciation_ko": "호테루데 미즈오 카이마스.",
                "meaning_ko": "호텔에서 물을 삽니다.",
            }
        )
    vocab.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requests": [
                    {
                        "id": "bad-req",
                        "target_date": "2026-09-19",
                        "status": "ready",
                        "words": ["薬局"],
                        "sentences": sentences,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(DAILY_SCRIPT),
            "--date",
            "2026-09-19",
            "--data",
            str(data),
            "--out-dir",
            str(out),
            "--vocab-requests",
            str(vocab),
            "--reliability-db",
            str(db),
        ],
        cwd=ELLE_WS,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode != 0
    assert "vocabulary request words not covered" in result.stderr


def test_missing_db_parent_does_not_report_acceptance(tmp_path):
    reliability = load_reliability_module()
    db_path = tmp_path / "not-a-dir" / "db.sqlite3"
    (tmp_path / "not-a-dir").write_text("file blocks directory", encoding="utf-8")
    with pytest.raises(Exception):
        reliability.connect(db_path)


def test_daily_script_dry_run_does_not_mutate_learning_data_or_create_outbox(tmp_path):
    data = tmp_path / "learning.json"
    out = tmp_path / "out"
    vocab = tmp_path / "vocab.json"
    db = tmp_path / "reliability.sqlite3"
    write_learning_data(data)
    vocab.write_text(
        json.dumps({"schema_version": 1, "requests": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    before = hashlib.sha256(data.read_bytes()).hexdigest()
    result = subprocess.run(
        [
            sys.executable,
            str(DAILY_SCRIPT),
            "--date",
            "2026-09-20",
            "--data",
            str(data),
            "--out-dir",
            str(out),
            "--vocab-requests",
            str(vocab),
            "--reliability-db",
            str(db),
            "--dry-run",
        ],
        cwd=ELLE_WS,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    after = hashlib.sha256(data.read_bytes()).hexdigest()
    audit = json.loads(result.stdout)
    assert before == after
    assert audit["dry_run"] is True
    assert audit["reliability_outbox_status"] == "dry_run"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_reliability_health_reports_stale_running_and_pending_outbox(tmp_path):
    reliability = load_reliability_module()
    db = tmp_path / "reliability.sqlite3"
    conn = reliability.connect(db)
    reliability.ensure_default_policy(conn, sentence_count=7)
    execution = reliability.begin_execution(
        conn,
        agent="elle",
        task="daily_japanese_sentence",
        target_date="2026-09-21",
        spec={"sentence_count": 7, "required_words": []},
        request_id=None,
        policy_version=1,
        lease_ms=-1,
    )
    with conn:
        reliability.create_fake_outbox(
            conn,
            execution_id=execution.id,
            channel="telegram",
            chat_id="8580974491",
            content="preview",
            media=[],
        )
    report = reliability.health_report(conn)
    assert report["stale_running"]
    assert report["pending_or_unknown_outbox"]

    result = subprocess.run(
        [sys.executable, str(RELIABILITY_PATH), "health", "--db", str(db)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["stale_running"]
    assert payload["pending_or_unknown_outbox"]
