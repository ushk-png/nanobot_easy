"""C2 v3 regressions: R-C2-05 (truncation/budget hide a later revocation), F2-C2-01 (mixed
limit + grant), F2-C2-02 (standing "하지 말고" limits), R-C2-06 (timestamp formats), F2-C2-04
(merged tail order and save boundary), F2-C2-03 (completed requests quoted as follow-ups).

The central property is checked on the block text that reaches the model, with grant
membership taken from the generator, not from the module's own patterns.
"""

import asyncio
import random
import re

import pytest

from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session import user_scope_messages as usm
from nanobot.session.manager import SessionManager
from tests.agent.scope_language_contrast_v3 import CONTRAST
from tests.agent.test_user_scope_messages import (
    BIG_REPLY,
    KEY,
    Fake,
    block_of,
    converse,
    make_loop,
    text_of,
)

W = "/w"


def ledger_for(texts, stamps=None):
    metadata: dict = {}
    rows = []
    for i, text in enumerate(texts):
        ts = stamps[i] if stamps else f"2026-11-19T01:{i:02}:00"
        usm.record(metadata, session_key=KEY, workspace=W, text=text, timestamp=ts)
        rows.append({"role": "user", "content": text, "timestamp": ts})
    return metadata, rows


def block(metadata, rows, history=(), current="다음 작업"):
    return usm.build_block(metadata, session_key=KEY, workspace=W, history=list(history),
                           current_text=current, session_messages=rows) or ""


# ------------------------------------------------------------------ R-C2-05

@pytest.mark.parametrize("length", [499, 500, 501, 900])
def test_revocation_at_the_end_of_a_long_message_is_never_hidden_behind_its_approval(length):
    approval = "production.json 수정해도 돼."
    ending = "앞서 production.json 변경 승인 철회합니다."
    revocation = ("일반적인 설명입니다. " * 80)[: length - len(ending)] + ending
    assert len(revocation) == length
    metadata, rows = ledger_for([approval, revocation])
    text = block(metadata, rows)
    assert ending in text                                    # head + tail keeps the closing words
    if length > usm.MAX_ENTRY_CHARS:
        assert approval not in text and "may grant approval are not quoted" in text
        assert "characters not shown" in text
    else:
        assert approval in text                              # shown in full: nothing hidden after it


def test_an_approval_is_withheld_when_a_later_quote_is_cut_by_the_display_budget():
    approval = "deploy.sh 실행해도 돼."
    later = [f"{i}번 파일은 건드리지 마." + "가" * 380 for i in range(12)]
    metadata, rows = ledger_for([approval, *later])
    text = block(metadata, rows)
    assert "older quoted message(s) are omitted" in text
    assert approval not in text


def test_storage_cap_and_shown_cap_boundaries():
    limits = [f"{i}번 파일은 건드리지 마." for i in range(usm.MAX_STORED + 1)]
    metadata, rows = ledger_for(limits)
    ledger = metadata[usm.LEDGER_KEY]
    assert len(ledger["entries"]) == usm.MAX_STORED and ledger["dropped"] == 1
    text = block(metadata, rows)
    assert text.count(" user: ") == usm.MAX_SHOWN and f"{usm.MAX_STORED + 1 - usm.MAX_SHOWN} older" in text
    # an approval stored but dropped from the ledger is never shown; the next one, with only
    # visible limits after it, is shown in full
    metadata, rows = ledger_for(["sample.json 수정해도 돼.", *limits[:3]])
    assert "sample.json 수정해도 돼." in block(metadata, rows)


def test_ledger_present_but_not_displayed_content_does_not_count_as_seen():
    approval = "production.json 수정해도 돼."
    metadata, rows = ledger_for([approval, "그건 좀 아닌 것 같아", "표로 바꿔줘.", "제목을 붙여줘.",
                                 "요약 길이를 줄여줘."])
    # the 4th plain message is past the follow-up window, so it is not in the ledger
    assert len(metadata[usm.LEDGER_KEY]["entries"]) == 4
    assert approval not in block(metadata, rows)
    assert approval in block(metadata, rows, history=[{"role": "user", "content": "요약 길이를 줄여줘."}])


GRANT = "수정해도 돼"


def _generate(rng, n):
    kinds = rng.choices(["grant", "revoke", "unknown", "limit", "mixed", "plain", "long_revoke", "long_plain"],
                        weights=[3, 2, 2, 2, 2, 3, 1, 1], k=n)
    texts = []
    for i, kind in enumerate(kinds):
        tag = f"(#{i})"                     # delimited: "#1" must not match inside "#13"
        texts.append({
            "grant": f"production.json {GRANT} {tag}",
            "revoke": f"아까 승인은 철회할게 {tag}",
            "unknown": f"그건 좀 아닌 것 같아 {tag}",
            "limit": f"production.json은 건드리지 마 {tag}",
            "mixed": f"production.json은 절대 건드리지 마 {tag}. sample.json은 {GRANT} {tag}.",
            "plain": f"표로 바꿔줘 {tag}",
            "long_revoke": "설명입니다. " * rng.randint(60, 90) + f"앞서 승인 철회합니다 {tag}",
            "long_plain": "설명입니다. " * rng.randint(60, 90) + f"끝 {tag}",
        }[kind])
    return kinds, texts


LINE = re.compile(r"^\d+\. \[[^\]]*\] user(?: \(([^)]*)\))?: (.*)$")
STAMPS = [lambda i: f"2026-11-19T01:{i:02}:00", lambda i: f"2026-11-19T01:{i:02}:00+00:00",
          lambda i: f"2026-11-19T10:{i:02}:00+09:00", lambda i: "2026-11-19T01:00:00",
          lambda i: f"2026-11-19T01:{59 - i:02}:00", lambda i: "garbage", lambda i: ""]


def test_property_no_grant_is_shown_while_a_later_message_is_not_fully_visible():
    rng = random.Random(20261119)
    shown_grants = withheld = excerpts = 0
    for _ in range(2500):
        n = rng.randint(2, 18)
        kinds, texts = _generate(rng, n)
        stamps = [rng.choice(STAMPS)(i) for i in range(n)]
        metadata, rows = ledger_for(texts, stamps)
        deleted = rng.randint(0, 2) if rng.random() < 0.25 else 0
        kept_rows = rows[deleted:]
        window = rng.randint(0, 3)
        history = [m for r in kept_rows[len(kept_rows) - window:] for m in (r, {"role": "assistant", "content": "네"})]
        replayed = {r["content"] for r in kept_rows[len(kept_rows) - window:]} if window else set()
        text = block(metadata, kept_rows, history=history)
        lines = text.splitlines()
        full_bodies = {m.group(2) for m in map(LINE.match, lines)
                       if m and "limit parts only" not in (m.group(1) or "") and "characters not shown" not in m.group(2)}
        for i, kind in enumerate(kinds):
            grant_part = f"{GRANT} (#{i})"
            if grant_part not in text:
                continue
            shown_grants += 1
            assert i >= deleted, (texts, text)                      # rows after it were deleted
            for j in range(i + 1, n):
                assert texts[j] in replayed or texts[j] in full_bodies, (i, j, texts, text)
        withheld += "may grant approval are not quoted" in text
        excerpts += "limit parts only" in text
        for ln in lines:
            if "limit parts only" in ln:
                assert GRANT not in ln, ln
    assert shown_grants > 200 and withheld > 200 and excerpts > 50, (shown_grants, withheld, excerpts)


# ------------------------------------------------------------------ F2-C2-01

@pytest.mark.parametrize("first, kept, hidden", [
    ("production.json은 절대 건드리지 마. sample.json은 수정해도 돼.", "production.json은 절대 건드리지 마.",
     "sample.json은 수정해도 돼"),
    ("sample.json은 수정해도 되지만 production.json은 건드리지 마.", "production.json은 건드리지 마.",
     "sample.json은 수정해도"),
    ("production.json 수정 금지는 풀어도 돼.", None, "풀어도 돼"),
])
def test_withheld_grant_keeps_only_its_limit_parts(first, kept, hidden):
    metadata, rows = ledger_for([first, "그건 좀 아닌 것 같아", "표로 바꿔줘.", "제목을 붙여줘.", "요약 길이를 줄여줘."])
    text = block(metadata, rows)
    assert hidden not in text and "may grant approval are not quoted" in text
    if kept:
        assert f"“{kept}”" in text and "limit parts only" in text


@pytest.mark.parametrize("limit", ["production.json 배포는 진행하지 마.", "괜찮아 보여도 production.json은 건드리지 마.",
                                   "앞서 production.json 변경 승인 철회합니다."])
def test_limits_containing_grant_words_are_not_treated_as_grants(limit):
    assert not usm.approval_like(limit)
    metadata, rows = ledger_for([limit, "그건 좀 아닌 것 같아", "표로 바꿔줘.", "제목을 붙여줘.", "요약 길이를 줄여줘."])
    assert limit in block(metadata, rows)


def test_plain_work_is_not_blocked_by_a_blanket_reapproval_rule():
    metadata, rows = ledger_for(["production.json은 건드리지 마.", "sample.json 수정해도 돼."])
    text = block(metadata, rows)
    assert "sample.json 수정해도 돼." in text and "may grant approval" not in text


# ------------------------------------------------------------------ F2-C2-02

@pytest.mark.parametrize("text, expected, axis", CONTRAST)
def test_standing_and_one_off_contrast_cases(text, expected, axis):
    assert usm.should_record(text) is expected, axis


# ------------------------------------------------------------------ R-C2-06

@pytest.mark.parametrize("stamps", [
    ["2026-11-19T01:00:00", "2026-11-19T01:01:00+00:00"],
    ["2026-11-19T01:01:00+09:00", "2026-11-19T01:00:00"],
    ["not a time", ""],
    ["2026-11-19T01:00:00", "2026-11-19T01:00:00"],
    ["2026-11-19T02:00:00", "2026-11-19T01:00:00"],
])
def test_timestamp_formats_never_raise_and_order_comes_from_rows(stamps):
    metadata, rows = ledger_for(["production.json 수정해도 돼.", "아까 승인은 철회할게"], stamps)
    text = block(metadata, rows)
    assert "아까 승인은 철회할게" in text
    assert text.index("production.json 수정해도 돼.") < text.index("아까 승인은 철회할게")


def test_entries_out_of_row_order_make_the_order_unknown():
    metadata, rows = ledger_for(["production.json 수정해도 돼.", "production.json은 건드리지 마."])
    text = block(metadata, list(reversed(rows)))
    assert "production.json 수정해도 돼." not in text and "건드리지 마" in text


def test_missing_rows_mean_unknown_order():
    metadata, _ = ledger_for(["production.json 수정해도 돼."])
    assert "production.json 수정해도 돼." not in (usm.build_block(
        metadata, session_key=KEY, workspace=W, history=[], current_text="x") or "")


# ------------------------------------------------------------------ loop level

FILL = ["먼저 자료를 정리해줘.", "분석 내용 전체를 길게 써줘.", "정리가 끝났는지만 알려줘."]
FILL_REPLIES = ["네.", BIG_REPLY, "끝났습니다."]


@pytest.mark.requires_tiktoken_encoding
def test_merged_tail_keeps_current_request_last_and_is_saved_once(tmp_path):
    fake = Fake(["알겠습니다.", *FILL_REPLIES, "네."])
    loop = make_loop(tmp_path, fake, flag=True)
    asyncio.run(converse(loop, ["production.json은 건드리지 마.", *FILL]))
    session = loop.sessions.get_or_create(KEY)
    session.add_message("user", "(중단된 이전 질문) 표 정리해줘")      # dangling user tail
    loop.sessions.save(session)
    before = [dict(m) for m in session.messages]
    asyncio.run(converse(loop, ["이어서 결론만 줘"]))
    prompt = fake.turn_prompts()[-1]
    quote = next(i for i, m in enumerate(prompt) if m.get(usm.QUOTE_MESSAGE_META))
    assert quote < len(prompt) - 1 and "이어서 결론만 줘" in text_of(prompt[quote + 1:])
    after = loop.sessions.get_or_create(KEY).messages
    assert not any(usm.BLOCK_TAG in str(m.get("content")) for m in after)
    added = after[len(before):]
    users = [m for m in added if m.get("role") == "user"]
    assert sum("이어서 결론만 줘" in str(m.get("content")) for m in after) == 1, users


def test_reload_cross_session_and_workspace(tmp_path):
    fake = Fake(["알겠습니다.", *FILL_REPLIES, "네.", "네."])
    loop = make_loop(tmp_path, fake, flag=True)
    asyncio.run(converse(loop, ["production.json은 건드리지 마.", *FILL]))
    reloaded = SessionManager(loop.sessions.workspace if hasattr(loop.sessions, "workspace") else tmp_path)
    session = reloaded.get_or_create(KEY)
    assert session.metadata.get(usm.LEDGER_KEY)
    asyncio.run(converse(loop, ["다음 작업 하자"], key="cli:other"))
    assert usm.BLOCK_TAG not in text_of(fake.turn_prompts()[-1])
    assert usm.build_block(session.metadata, session_key=KEY, workspace="/elsewhere", history=[],
                           current_text="x", session_messages=session.messages) is None


@pytest.mark.requires_tiktoken_encoding
def test_new_after_several_turns_and_compaction(tmp_path):
    fake = Fake(["알겠습니다.", "네.", *FILL_REPLIES, "네.", "네.", "네."])
    loop = make_loop(tmp_path, fake, flag=True)
    asyncio.run(converse(loop, ["deploy.sh 실행해도 돼.", "production.json은 건드리지 마.", *FILL]))
    session = loop.sessions.get_or_create(KEY)
    del session.messages[:2]                                    # compaction deletes from the front
    loop.sessions.save(session)
    asyncio.run(converse(loop, ["다음 작업"]))
    text = block_of(fake.turn_prompts()[-1])
    assert "deploy.sh 실행해도 돼." not in text and "production.json은 건드리지 마." in text
    asyncio.run(converse(loop, ["/new", "안녕", "오늘 할 일 정리해줘"]))
    assert usm.BLOCK_TAG not in text_of(fake.turn_prompts()[-1])


class Reader(Fake):
    """Scripted reader, not a model: on a turn it re-runs any quoted request line that is not
    marked as handled. Shows only what the block lets a literal reader do."""

    async def chat(self, **kwargs):
        prompt = text_of(kwargs["messages"])
        if kwargs.get("tools") and usm.BLOCK_TAG in prompt and not self.replies:
            body = prompt[prompt.index(usm.BLOCK_TAG):prompt.index(usm.BLOCK_END)]
            if any("run-1" in ln and "handled in its own turn" not in ln for ln in body.splitlines()):
                self.replies = [_append_run(), "다시 추가했습니다."]
        return await super().chat(**kwargs)


def _append_run():
    return LLMResponse(content="", tool_calls=[ToolCallRequest(
        id="w1", name="write_file", arguments={"path": "log.txt", "content": "start\nrun-1\n",
                                               "intent_summary": "append run-1", "target": "log.txt",
                                               "scope": "once", "reversible": True})])


@pytest.mark.requires_tiktoken_encoding
def test_completed_write_is_not_redone_and_the_follow_up_is_kept(tmp_path):
    """F2-C2-03: the completed request stays quoted (revocations after an approval still need
    it), marked as handled; a literal reader does not run it again."""
    (tmp_path / "log.txt").write_text("start\n")
    fake = Reader(["알겠습니다.", _append_run(), "추가했습니다.", *FILL_REPLIES])
    loop = make_loop(tmp_path, fake, flag=True)
    asyncio.run(converse(loop, ["log.txt는 수정해도 돼.", "log.txt 끝에 'run-1' 한 줄을 추가해줘.", *FILL]))
    assert (tmp_path / "log.txt").read_text() == "start\nrun-1\n"
    (tmp_path / "log.txt").write_text("start\nrun-1\nrun-1-checked\n")   # any rewrite would drop this
    asyncio.run(converse(loop, ["다음 작업: 결론만 요약해줘"]))
    text = block_of(fake.turn_prompts()[-1])
    assert (tmp_path / "log.txt").read_text() == "start\nrun-1\nrun-1-checked\n"
    line = next(ln for ln in text.splitlines() if "run-1" in ln)
    assert "follow-up; handled in its own turn" in line and "do not start it again" in text
