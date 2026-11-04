"""t58 §3: t56 A1->A2 replay through the real AgentLoop (MOCK provider, no network).

REAL-RECORD REPLAY: the design arguments are the verbatim raw_designs of the t56 records
(/workspace/t56/run/LIVE-new-A_golf-t{1,2}.json); only event/task ids are remapped to the ids the
replayed session assigns (read from the real assembled design input). The A2 executor text is
recovered from the recorded reply wrapper ("부분 응답(미검증): ..."): the record keeps no separate
executor output. The A1 prior state is "기존 기록에서 복원한 선행 상태" (replayed from the A1 record).
MOCK SUPPLEMENT (labelled per test): wrong totals/splits and a computed spec the model did not send.
"""
import json
from pathlib import Path

import pytest

from nanobot.agent.context_evidence import completion_kind
from nanobot.agent.context_selection import trusted_obligations
from nanobot.providers.base import LLMResponse, ToolCallRequest
from .t51_harness import Session, final

RUN = Path("/workspace/t56/run")
A1_ID, A2_ID, A1_ANS, TASK = ("E7d5a47c7e7b84926b66541e249fbbca2", "E828e5b5a183f45f5943690d71e408762",
                              "E69dd527f401b4262a9040065d28f7095", "804129d934f542c5857ce634b213805c")


def record(turn):
    p = RUN / f"LIVE-new-A_golf-t{turn}.json"
    if not p.exists():
        pytest.skip("t56 records not present")
    return json.loads(p.read_text())


def submit(args):
    return LLMResponse(content=None, usage={}, tool_calls=[ToolCallRequest(
        id="design", name="submit_context_design", arguments=args)])


def remap(raw, mapping):
    for a, b in mapping.items():
        raw = raw.replace(a, b)
    return json.loads(raw)


def a1_step(messages):
    p = json.loads(messages[1]["content"])
    rid = next(x["event_id"] for x in p["sources"] if x.get("content_ref") == "request")
    return submit(remap(record(1)["raw_designs"][0]["raw"], {A1_ID: rid}))


def a2_step(spec=None):
    def step(messages):
        p = json.loads(messages[1]["content"])
        src = p["sources"]
        rid = next(x["event_id"] for x in src if x.get("content_ref") == "request")
        user = next(x["event_id"] for x in src if x["event_type"] == "USER_MESSAGE" and x["event_id"] != rid)
        asst = next(x["event_id"] for x in src if x["event_type"] == "ASSISTANT_MESSAGE")
        args = remap(record(2)["raw_designs"][0]["raw"], {A2_ID: rid, A1_ID: user, A1_ANS: asst,
                                                           TASK: p["prior_context"]["current_task_id"]})
        if spec is not None:                       # MOCK SUPPLEMENT: the t56 model sent no completion_specs
            args["completion_specs"] = [{"check": args["completion_checks"][0], "result_kind": "computed",
                                         "expected_values": spec}]
        return submit(args)
    return step


SPEC = {"values": {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, "total": 45, "unit": "분"}


def executor_text():
    return record(2)["reply"].split("부분 응답(미검증): ", 1)[1]


def wrong_total():
    return executor_text().replace("12.5", "15").replace("45분", "50분").replace("25분", "30분")


def wrong_split():
    return (executor_text().replace("어프로치 12.5", "어프로치 15").replace("아이언 12.5", "아이언 10")
            .replace("12.5분씩", "15분과 10분으로"))


async def replay(tmp_path, monkeypatch, answer=None, spec=None):
    s = Session(tmp_path, monkeypatch)
    s.mock.designs.append(a1_step)
    a1 = await s.turn(record(1)["request"])
    assert a1 == record(1)["reply"]               # the restored prior state reproduces the A1 reply
    s.mock.designs.append(a2_step(spec))
    s.mock.steps.extend([final(answer or executor_text()), final(answer or executor_text())])
    out = await s.turn(record(2)["request"])
    return s, out


@pytest.mark.asyncio
async def test_real_record_replay_reproduces_a2_incomplete_and_locates_the_break(tmp_path, monkeypatch):
    s, out = await replay(tmp_path, monkeypatch)
    rec2 = record(2)
    sel = s.selected[-1]
    req = sel.current_event.content
    # linkage, step by step (REAL-RECORD REPLAY)
    assert rec2["raw_designs"][0]["raw"].count('"completion_specs"') == 0       # 1. spec never created
    assert sel.design.completion_specs == [] and len(sel.design.completion_checks) == 4
    assert sel.design.route == "execute"                                       # direct, answer null -> execute
    assert out.startswith("요청을 완료하지 못했습니다")
    assert executor_text() in out                                              # 2. handoff kept; answer correct
    # 3. t58 first broken link: the obligations carry lookup=forbidden whose span is the *kept* turn-1
    #    prohibition, not in this request. t59 fix (defect 1, not an expectation tweak): the span is
    #    verified against the original A1 user event of the same task, so the obligations are trusted.
    assert sel.design.obligations.lookup == "forbidden" and sel.design.obligations.external_change == "none"
    assert sel.design.obligations.source_span not in req and sel.design.obligations.source_span in record(1)["request"]
    assert trusted_obligations(sel.design, req) is not None
    assert {r["status"] for r in sel.carried_report()} == {"valid"}
    # 4. t59 (defect 2): the checks are answer kinds now, but the calculation check has no spec, so a
    #    non-empty answer does not pass it; the loop returned once and the executor (recorded text
    #    repeated) stated no calculation -> honest incomplete with the reason, never a pass
    assert {completion_kind(c, sel) for c in sel.design.completion_checks} == {"answer_content"}
    assert sel.calc_spec_missing() == [sel.design.completion_checks[0]]
    assert sel.completion_supported() is False and "계산 검증 필요·명세 없음" in out
    assert sel.calculation_status()["overall"] == "unverified"                 # reporting only
    assert [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]                # the A1 task, reopened by A2
    assert len(s.mock.requests) == 4 and s.mock.designs == []                  # t56 sends (1 + 2) + 1 fix


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [wrong_total, wrong_split], ids=["wrong_total", "wrong_split"])
async def test_mock_wrong_answers_never_complete_on_the_recorded_design(tmp_path, monkeypatch, make):
    """MOCK SUPPLEMENT (answer text altered). Recorded design, no spec."""
    s, out = await replay(tmp_path, monkeypatch, answer=make())
    assert out.startswith("요청을 완료하지 못했습니다")
    assert [t["status"] for t in s.tasks()] == ["IN_PROGRESS"]
    assert s.selected[-1].completion_supported() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [wrong_total, wrong_split], ids=["wrong_total", "wrong_split"])
async def test_mock_spec_wrong_answers_are_caught_in_code(tmp_path, monkeypatch, make):
    """MOCK SUPPLEMENT (computed spec added + answer altered)."""
    s, out = await replay(tmp_path, monkeypatch, answer=make(), spec=SPEC)
    sel = s.selected[-1]
    assert sel.calculation_status()["overall"] == "mismatch"
    assert sel.completion_supported() is False and out.startswith("요청을 완료하지 못했습니다")


@pytest.mark.asyncio
async def test_mock_spec_correct_answer_completes(tmp_path, monkeypatch):
    """MOCK SUPPLEMENT (computed spec added; the t56 correct answer text unchanged).
    t58 strict xfail removed in t59: fixed by carried-condition provenance (defect 1)."""
    s, out = await replay(tmp_path, monkeypatch, spec=SPEC)
    sel = s.selected[-1]
    assert sel.calculation_status()["checks"][sel.design.completion_checks[0]] == "pass"
    assert sel.completion_supported() is True and not out.startswith("요청을 완료하지 못했습니다")
