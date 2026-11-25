"""Regression tests for the independent verifier's findings on the opt-in scope quotes (v2).

F-C2-01 quotes must not be classified as the current request; F-C2-02 /new ends the ledger;
F-C2-03 an approval is never quoted while a later message that may revoke it is unseen;
F-C2-04 limits that only shaped a completed request are not kept. Offline, fake provider.
"""

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.task_note import user_text
from nanobot.session import user_scope_messages as usm
from nanobot.session.manager import Session
from tests.agent.scope_language_heldout import HELDOUT
from tests.agent.scope_language_labels import LABELS
from tests.agent.test_user_scope_messages import (
    BIG_REPLY,
    KEY,
    Fake,
    block_of,
    converse,
    make_loop,
    text_of,
)

FILLER = ["먼저 자료를 정리해줘.", "정리가 끝났는지만 알려줘."]
FILLER_REPLIES = [BIG_REPLY, "끝났습니다."]


async def _turn(tmp_path, flag, history, replies, current, current_replies, **kwargs):
    fake = Fake([*replies, *current_replies])
    loop = make_loop(tmp_path / ("on" if flag else "of"), fake, flag=flag, **kwargs)
    await converse(loop, history)
    before = len(fake.turn_prompts())
    out = await loop.process_direct(current, session_key=KEY, chat_id="scope")
    return loop, fake, fake.turn_prompts()[before:], getattr(out, "content", out)


# ---------------------------------------------------------------- F-C2-01

@pytest.mark.asyncio
@pytest.mark.parametrize("old, current, replies", [
    # an old "do not run" quote must not switch off the promise-only guard for a new execution request
    ("배포 스크립트는 절대 실행하지 마.", "tests 폴더 테스트 실행해 줘",
     ["테스트를 실행하겠습니다.", "실행할 수 없어 미완료입니다."]),
    # the quote framing must not turn a simple question into an action request
    ("production.json은 건드리지 마.", "오늘 회의가 몇 시였지?", ["회의 시간을 확인하겠습니다.", "10시였습니다."]),
    # an old "explain" quote must not make a short answer look like a missing explanation
    ("앞으로 답변은 항상 자세히 설명해 줘.", "결론만 줘", ["결론을 정리해 드리겠습니다.", "결론: A입니다."]),
])
@pytest.mark.requires_tiktoken_encoding
async def test_request_guards_classify_only_the_current_request(tmp_path, old, current, replies):
    results = {}
    for flag in (False, True):
        _, _, prompts, final = await _turn(tmp_path, flag, [old, *FILLER], ["알겠습니다.", *FILLER_REPLIES],
                                           current, replies)
        results[flag] = (len(prompts), final)
        if flag:
            assert old in block_of(prompts[0])                  # the quote is still delivered
            assert user_text(prompts[0]) == current
    assert results[True] == results[False]


# ---------------------------------------------------------------- F-C2-02

@pytest.mark.asyncio
async def test_new_conversation_does_not_inherit_quotes_or_approvals(tmp_path):
    fake = Fake(["알겠습니다.", "확인했습니다.", "정리했습니다."])
    loop = make_loop(tmp_path, fake, flag=True)
    await converse(loop, ["production.json은 절대 변경하지 마.", "deploy.sh 실행해도 돼. 승인할게."])
    assert usm.LEDGER_KEY in loop.sessions.get_or_create(KEY).metadata
    await converse(loop, ["/new", "안녕, 오늘 할 일 정리해줘"])
    session = loop.sessions.get_or_create(KEY)
    assert usm.BLOCK_TAG not in text_of(fake.turn_prompts()[-1])
    assert "deploy.sh" not in text_of(fake.turn_prompts()[-1])
    assert usm.LEDGER_KEY not in session.metadata


def test_every_session_clear_path_drops_the_ledger():
    session = Session(key=KEY, messages=[{"role": "user", "content": "x"}])
    usm.record(session.metadata, session_key=KEY, workspace="/w", text="deploy.sh 실행해도 돼",
               timestamp="2026-11-19T08:00:00")
    session.metadata["other"] = 1
    session.clear()                                   # /new, SDK clear and retain(0) all use this
    assert usm.LEDGER_KEY not in session.metadata and session.metadata["other"] == 1
    usm.record(session.metadata, session_key=KEY, workspace="/w", text="deploy.sh 실행해도 돼",
               timestamp="2026-11-19T08:00:01")
    session.messages = [{"role": "user", "content": str(i)} for i in range(5)]
    session.retain_recent_legal_suffix(0)
    assert usm.LEDGER_KEY not in session.metadata


# ---------------------------------------------------------------- F-C2-03

APPROVAL = "production.json 수정해도 돼"


@pytest.mark.asyncio
@pytest.mark.requires_tiktoken_encoding
async def test_known_revocation_phrase_is_quoted_with_the_approval(tmp_path):
    revocation = "아까 production.json 건은 없던 걸로 하자"
    _, _, prompts, _ = await _turn(tmp_path, True, [APPROVAL, revocation, *FILLER],
                                   ["확인했습니다.", "알겠습니다.", *FILLER_REPLIES], "이제 다음 작업 하자", ["네."])
    block = block_of(prompts[0])
    assert block.index(APPROVAL) < block.index(revocation)


@pytest.mark.asyncio
@pytest.mark.requires_tiktoken_encoding
async def test_unknown_revocation_phrase_is_kept_as_a_follow_up(tmp_path):
    revocation = "그건 좀 아닌 것 같아"                   # no pattern matches it
    assert not usm.should_record(revocation)
    _, _, prompts, _ = await _turn(tmp_path, True, [APPROVAL, revocation, *FILLER],
                                   ["확인했습니다.", "알겠습니다.", *FILLER_REPLIES], "이제 다음 작업 하자", ["네."])
    block = block_of(prompts[0])
    assert block.index(APPROVAL) < block.index(revocation)


@pytest.mark.asyncio
@pytest.mark.requires_tiktoken_encoding
async def test_approval_is_withheld_when_a_later_message_is_unseen(tmp_path):
    later = ["다음 파일 봐줘", "그 다음도", "하나 더", "그건 좀 아닌 것 같아"]   # 4th is past the follow-up window
    _, _, prompts, _ = await _turn(tmp_path, True, ["config.yaml은 건드리지 마.", APPROVAL, *later, *FILLER],
                                   ["네.", "확인했습니다.", "네.", "네.", "네.", "알겠습니다.", *FILLER_REPLIES],
                                   "남은 승인된 작업이 있으면 해줘", ["네."])
    first = prompts[0]
    block = block_of(first)
    assert "그건 좀 아닌 것 같아" not in text_of(first)        # the revocation itself is gone
    assert APPROVAL not in text_of(first)                       # so the approval is not quoted
    assert "earlier message(s) that may grant approval are not quoted" in block
    assert "config.yaml은 건드리지 마." in block                 # limits stay quoted


@pytest.mark.asyncio
async def test_approval_is_withheld_after_compaction_deleted_later_rows(tmp_path):
    fake = Fake(["확인했습니다.", "네.", "네.", "네.", "네.", "네."])
    loop = make_loop(tmp_path, fake, flag=True)
    await converse(loop, [APPROVAL, "1", "2", "3", "그건 좀 아닌 것 같아", "5"])
    await loop.consolidator.compact_idle_session(KEY, max_suffix=2)
    await converse(loop, ["승인된 작업 해줘"])
    final = fake.turn_prompts()[-1]
    assert APPROVAL not in text_of(final)


def test_approval_with_nothing_unseen_after_it_is_quoted():
    metadata, rows = {}, []
    for i, text in enumerate(["production.json 절대 변경하지 마", APPROVAL]):
        ts = f"2026-11-19T08:00:0{i}"
        usm.record(metadata, session_key=KEY, workspace="/w", text=text, timestamp=ts)
        rows.append({"role": "user", "content": text, "timestamp": ts})
    block = usm.build_block(metadata, session_key=KEY, workspace="/w", history=[], current_text="",
                            session_messages=rows)
    assert block.index("production.json 절대 변경하지 마") < block.index(APPROVAL)
    # Without the session rows nothing can be ruled out: the approval is withheld.
    block = usm.build_block(metadata, session_key=KEY, workspace="/w", history=[], current_text="")
    assert APPROVAL not in block and "production.json 절대 변경하지 마" in block


def test_withholding_never_shows_an_approval_older_than_an_unseen_message():
    import random
    random.seed(11)
    shown_approvals = withheld_blocks = 0
    kinds = ["production.json 금지 ", "production.json 수정해도 돼 ", "plain "]
    for _ in range(1500):
        metadata, rows = {}, []
        for i in range(random.randint(1, 30)):
            text = random.choice(kinds) + "x" * random.choice([5, 300]) + f" #{i}"
            ts = f"2026-11-19T08:{i // 60:02}:{i % 60:02}"
            usm.record(metadata, session_key=KEY, workspace="/w", text=text, timestamp=ts)
            rows.append({"role": "user", "content": text, "timestamp": ts})
        keep = random.randint(0, len(rows))
        history = rows[len(rows) - keep:]
        cut = random.randint(0, len(rows))                       # rows deleted from the front
        block = usm.build_block(metadata, session_key=KEY, workspace="/w", history=history,
                                current_text="", session_messages=rows[cut:])
        recorded = {e["text"] for e in metadata.get(usm.LEDGER_KEY, {}).get("entries", [])}
        visible = {r["content"] for r in history}
        unseen = [i for i, r in enumerate(rows) if r["content"] not in visible
                  and (r["content"] not in recorded or i < cut)]
        for i, row in enumerate(rows):
            if "수정해도 돼" in row["content"] and f"user: {row['content']}\n" in (block or ""):
                assert not any(j > i for j in unseen), (i, unseen)
                assert i >= cut                                      # nothing deleted after it
                shown_approvals += 1
        withheld_blocks += "may grant approval are not quoted" in (block or "")
    assert shown_approvals > 100 and withheld_blocks > 100           # both paths exercised


# ---------------------------------------------------------------- F-C2-04 and recall

def test_limits_that_only_shaped_a_completed_request_are_not_kept():
    q1 = "대한민국의 수도는 어디야? 도구를 사용하지 말고 한 문장으로 답해줘."
    q2 = "나노봇 설정을 저장하는 방법만 간략히 설명해줘. 실제 설정이나 파일은 변경하지 마."
    q4 = ("sample.md를 read_file로 한 번에 한 줄씩, 총 80번 나누어 읽어줘. 다른 방식으로 전체 읽기를 "
          "대체하지 말고, 마지막에 첫 줄·40번째 줄·마지막 줄을 확인해줘.")
    assert not usm.should_record(q1) and not usm.should_record(q4)
    assert usm.should_record(q2)                                     # sentence-final standing limit
    # A named file or a persistence word keeps a connective limit standing.
    assert usm.should_record("production.json은 건드리지 말고 sample.json만 고쳐줘")
    assert usm.should_record("config.yaml 수정하지 말고 요약해줘")
    assert usm.should_record("앞으로 도구를 사용하지 말고 답해줘")


KNOWN_MISSES = {"그건 좀 아닌 것 같아", "production.json 얘기는 다시 원점이야"}
KNOWN_FALSE = {"주문 취소 방법 알려줘"}


def test_labeled_set_decisions_are_pinned():
    misses = {t for _, t, exp in LABELS if exp and not usm.should_record(t)}
    false_pos = {t for _, t, exp in LABELS if not exp and usm.should_record(t)}
    assert misses == KNOWN_MISSES and false_pos == KNOWN_FALSE


def test_heldout_set_is_reported_not_tuned():
    # Pinned only so a change is noticed; the held-out numbers are reported in report-v2.md.
    recall = sum(usm.should_record(t) for _, t, exp in HELDOUT if exp) / sum(exp for *_, exp in HELDOUT)
    assert 0.7 <= recall < 1.0


# ---------------------------------------------------------------- placement and persistence

def test_quote_message_placement_and_merged_history_tail(tmp_path):
    builder = ContextBuilder(tmp_path)
    block = "[Earlier user messages — x]\n1. user: q\n[/Earlier user messages]"
    standalone = builder.build_messages(history=[{"role": "assistant", "content": "a"}], current_message="now",
                                        earlier_user_messages=block)
    assert standalone[-2].get(usm.QUOTE_MESSAGE_META) and standalone[-1]["content"].startswith("now")
    merged = builder.build_messages(history=[{"role": "user", "content": "pending"}], current_message="now",
                                    earlier_user_messages=block)
    # v3 (F2-C2-04): the quote goes before the merged tail, so the current request stays last.
    assert merged[-2].get(usm.QUOTE_MESSAGE_META) and "now" in merged[-1]["content"]
    assert merged[-1]["content"].startswith("pending") and len(merged) == 3


def test_quote_message_is_never_persisted(tmp_path):
    loop = make_loop(tmp_path, Fake(), flag=True)
    session = Session(key=KEY)
    quote = usm.quote_message("[Earlier user messages — x]\nq\n[/Earlier user messages]")
    loop._save_turn(session, [{"role": "system", "content": "s"}, quote, {"role": "user", "content": "now"},
                              {"role": "assistant", "content": "ok"}], 1)
    assert [m["content"] for m in session.messages] == ["now", "ok"]
