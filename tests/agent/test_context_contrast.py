"""S5 contrast suite: same words, opposite obligations (selective context only).

Each case pairs a request with the design a model may return. ``expect`` is the right outcome:
``complete`` (an answer alone satisfies it; blocking or marking it incomplete is a false block) or
``work`` (needs a lookup, a file read, or an external change; delivering it as complete without
that work is a false complete). Variants:
  model      - the design a correct model returns (typed obligations with spans)
  untyped    - the same design without obligations (old models / stored proposals)
  untyped_direct / mistyped_direct - wrong designs claiming a direct answer for work requests
  wrong_numbers - a computed answer whose numbers do not hold
The executor is simulated with no tool calls, so ``work`` cases must never come out complete.

Run ``python -m tests.agent.test_context_contrast`` in any tree to print the rates.
"""
import asyncio
import copy

import pytest

from nanobot.agent.context_selection import IntentDesign

from .test_context_selection import event, plan, response, select
from .test_context_selection import env as env  # noqa: F401

GOLF_T1 = ("주말에 골프 연습을 60분 하려고 합니다. 퍼팅 20분, 어프로치 20분, 아이언 20분으로 계획해 주세요. "
           "드라이버 연습은 제외하고, 시간표와 각 연습의 목표를 하나씩 적어 주세요. 인터넷 검색은 필요 없습니다.")
GOLF_T1_ANSWER = ("0:00-20:00 퍼팅 20분 - 목표: 거리감\n20:00-40:00 어프로치 20분 - 목표: 탄도\n"
                  "40:00-60:00 아이언 20분 - 목표: 정타\n드라이버 연습은 제외했습니다.")
GOLF_T2 = ("방금 계획에서 전체 시간을 45분으로 줄여 주세요. 퍼팅 20분은 그대로 유지하고, 남은 시간은 "
           "어프로치와 아이언에 똑같이 배분해 주세요. 드라이버 제외 조건도 유지해 주세요.")
GOLF_T2_ANSWER = ("총 45분 계획\n0:00-20:00 퍼팅 20분 - 목표: 거리감\n20:00-32:30 어프로치 12.5분 - 목표: 탄도\n"
                  "32:30-45:00 아이언 12분 30초 - 목표: 정타\n드라이버 연습은 제외했습니다.")
GOLF_T2_WRONG = GOLF_T2_ANSWER.replace("12.5분", "15분").replace("12분 30초", "10분")
GOLF_EXPECTED = {"values": {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, "total": 45, "excluded": ["드라이버"]}
PRIOR_GOLF = [("user", GOLF_T1), ("assistant", GOLF_T1_ANSWER)]
PRIOR_TCP = [("user", "TCP와 UDP 차이를 알려줘"), ("assistant", "TCP는 연결 지향, UDP는 비연결 ...")]


def ob(span=None, **kw):
    return {"obligations": {"source_span": span, **kw}}


def computed(check="45분 배분"):
    return {"completion_checks": [check], "completion_specs": [
        {"check": check, "result_kind": "computed", "expected_values": GOLF_EXPECTED}]}


EXEC = {"route": "execute", "answer": None}
WRONG_DIRECT = {"route": "direct", "answer": "완료했습니다. 결과는 위와 같습니다."}

# (id, group, prior, request, expect, model design overrides, executor answer)
CASES = [
    ("search_do", "검색해 줘/하지 마", [], "최신 비트코인 가격을 검색해 줘", "work",
     {**EXEC, "capabilities_needed": ["web_search"], **ob("검색해 줘", lookup="required")}, "약 1억 원입니다."),
    ("search_dont", "검색해 줘/하지 마", [], "검색하지 말고 TCP와 UDP 차이를 설명해 줘", "complete",
     {"answer": "TCP는 연결 지향 ...", **ob("검색하지 말고", lookup="forbidden")}, None),
    ("search_unneeded", "필요 없어/필요하면", [], "인터넷 검색은 필요 없습니다. 리스트와 튜플 차이를 알려주세요.",
     "complete", {"answer": "리스트는 가변 ...", **ob("검색은 필요 없습니다", lookup="unneeded")}, None),
    ("search_conditional", "필요 없어/필요하면", [], "TCP와 UDP 차이를 알려줘. 필요하면 검색해", "complete",
     {"answer": "TCP는 연결 지향 ...", **ob("필요하면 검색해", lookup="conditional")}, None),
    ("edit_answer", "답변에서 수정/파일 수정", PRIOR_GOLF, "방금 답변에서 아이언 목표를 수정해 줘", "complete",
     {"request_type": "modify", "answer": GOLF_T1_ANSWER.replace("정타", "탄도 조절"), **ob(target="prior_answer")},
     None),
    ("edit_file", "답변에서 수정/파일 수정", [], "notes.md 파일을 수정해 줘", "work",
     {**EXEC, "request_type": "modify", **ob("파일을 수정해 줘", target="external_resource",
                                              external_change="required")}, "수정했습니다."),
    ("test_howto", "테스트 방법/실행", [], "pytest로 테스트하는 방법을 설명해 줘", "complete",
     {"answer": "pytest -q 로 실행합니다 ...", "completion_checks": ["테스트 방법 설명"]}, None),
    ("test_run", "테스트 방법/실행", [], "pytest 테스트를 실행해 줘", "work",
     {**EXEC, "request_type": "execute", "completion_checks": ["테스트 실행 결과"],
      **ob("테스트를 실행해 줘", target="external_resource", external_change="required")}, "모두 통과했습니다."),
    ("quoted_search_fail", "quoted '검색 실패'", [], "로그에 '검색 실패'라고 나오는 이유를 설명해 줘", "complete",
     {"answer": "검색 백엔드 응답이 없을 때 ...", **ob(lookup="unneeded")}, None),
    ("golf_t1", "드라이버 제외 유지/추가", [], GOLF_T1, "complete",
     {"request_type": "other", "answer": GOLF_T1_ANSWER, **ob("인터넷 검색은 필요 없습니다", lookup="unneeded",
                                                              keep_constraints=["드라이버 연습은 제외"])}, None),
    ("golf_t2_keep", "드라이버 제외 유지/추가", PRIOR_GOLF, GOLF_T2, "complete",
     {"request_type": "modify", "answer": GOLF_T2_ANSWER, **computed(),
      **ob(target="prior_answer", keep_constraints=["드라이버 제외 조건도 유지"])}, None),
    ("golf_add_driver", "드라이버 제외 유지/추가", PRIOR_GOLF, "방금 계획에 드라이버 연습 10분을 추가해 줘", "complete",
     {"request_type": "modify", "answer": GOLF_T1_ANSWER + "\n60:00-70:00 드라이버 10분",
      **ob(target="prior_answer", change_constraints=["드라이버 연습 10분을 추가"])}, None),
    ("continue_explain", "계속 설명해줘", PRIOR_TCP, "계속 설명해줘", "complete",
     # the earlier explanation is DONE; continuing it is a new answer turn, not resumed work
     {"request_type": "explain", "task_relation": "new", "answer": "이어서, 흐름 제어는 ..."}, None),
    ("para_no_web", "paraphrase", [], "웹은 찾아보지 말고 아는 대로 HTTP와 HTTPS 차이만 설명해", "complete",
     {"answer": "HTTPS는 TLS ...", **ob("웹은 찾아보지 말고", lookup="forbidden")}, None),
    ("para_google", "paraphrase", [], "구글링해서 오늘 서울 날씨 알려줘", "work",
     {**EXEC, "capabilities_needed": ["web_search"], **ob("구글링해서", lookup="required")}, "맑음입니다."),
    ("para_summary", "paraphrase", PRIOR_TCP, "방금 답변을 한 줄로 요약해 줘", "complete",
     {"request_type": "modify", "answer": "TCP는 신뢰, UDP는 속도.", **ob(target="prior_answer")}, None),
    ("combo_no_search_save", "combination", [], "검색은 하지 말고, 결과를 report.md 파일에 저장해 줘", "work",
     {**EXEC, "request_type": "modify", **ob("report.md 파일에 저장해 줘", lookup="forbidden",
                                              target="external_resource", external_change="required")},
     "저장했습니다."),
    ("combo_read_file", "combination", [], "config.yaml 내용을 설명해 줘", "work",
     {**EXEC, "information_needs": [], "capabilities_needed": ["read_file"]}, "설정은 ..."),
    ("instr_change_no_search", "instruction change",
     [("user", "TCP와 UDP 차이를 검색해 줘"), ("assistant", "요청을 완료하지 못했습니다.")],
     "아니, 검색하지 말고 아는 대로 설명해 줘", "complete",
     {"task_relation": "change", "answer": "TCP는 연결 지향 ...", **ob("검색하지 말고", lookup="forbidden")}, None),
    ("instr_change_search", "instruction change", PRIOR_TCP, "아니, 이번엔 최신 RFC를 검색해서 확인해 줘", "work",
     {**EXEC, "task_relation": "change", "capabilities_needed": ["web_search"],
      **ob("검색해서 확인해 줘", lookup="required")}, "RFC 9293입니다."),
    ("deploy", "safety", [], "서버에 배포해 줘", "work",
     {**EXEC, "request_type": "execute", **ob("배포해 줘", target="external_resource", external_change="required")},
     "배포했습니다."),
    # ---- patch 14: paraphrase families (relation-level, no per-phrase rule) ----
    ("para_cond_si", "paraphrase conditional", [], "TCP와 UDP 차이를 알려줘. 필요 시 서치해 줘", "complete",
     {"answer": "TCP는 연결 지향 ...", **ob("필요 시 서치해 줘", lookup="conditional")}, None),
    ("para_cond_unknown", "paraphrase conditional", [], "모르면 찾아보고 HTTP/2 장점을 알려줘", "complete",
     {"answer": "HTTP/2는 다중화 ...", **ob("모르면 찾아보고", lookup="conditional")}, None),
    ("para_cond_en", "paraphrase conditional", [], "Explain TCP vs UDP; if needed, look it up", "complete",
     {"answer": "TCP is connection oriented ...", **ob("if needed, look it up", lookup="conditional")}, None),
    ("para_lookup_web", "paraphrase lookup", [], "웹에서 찾아보고 오늘 환율 알려줘", "work",
     {**EXEC, "capabilities_needed": ["web_search"], **ob("웹에서 찾아보고", lookup="required")}, "1,380원입니다."),
    ("para_lookup_naver", "paraphrase lookup", [], "네이버에 쳐보고 오늘 날씨 알려줘", "work",
     {**EXEC, "capabilities_needed": ["web_search"], **ob("네이버에 쳐보고", lookup="required")}, "맑음."),
    ("para_lookup_en", "paraphrase lookup", [], "look it up and tell me the latest Python version", "work",
     {**EXEC, "capabilities_needed": ["web_search"], **ob("look it up", lookup="required")}, "3.14."),
    ("para_answer_only", "paraphrase answer-only edit", PRIOR_GOLF, "파일은 수정하지 말고 답변만 수정해 줘", "complete",
     {"request_type": "modify", "answer": GOLF_T1_ANSWER.replace("정타", "탄도"), **ob(target="prior_answer")}, None),
    ("para_answer_only2", "paraphrase answer-only edit", PRIOR_GOLF, "파일은 건드리지 말고 위 답변만 고쳐 줘", "complete",
     {"request_type": "modify", "answer": GOLF_T1_ANSWER.replace("정타", "탄도"), **ob(target="prior_answer")}, None),
    ("para_answer_edit_short", "paraphrase answer-only edit", PRIOR_GOLF, "답변에서 아이언 부분 수정해 줘", "complete",
     {"request_type": "modify", "answer": GOLF_T1_ANSWER.replace("정타", "탄도"), **ob(target="prior_answer")}, None),
    ("deictic_doc", "deictic external target", [], "아까 만든 문서를 수정해", "work",
     {**EXEC, "request_type": "modify", **ob("문서를 수정해", target="external_resource", external_change="required")},
     "수정했습니다."),
    ("deictic_setting", "deictic external target", [], "그 설정 바꿔 줘", "work",
     {**EXEC, "request_type": "modify", **ob("설정 바꿔 줘", target="external_resource", external_change="required")},
     "바꿨습니다."),
]


def variants(case):
    _, _, _, _, expect, model, answer = case
    out = {"model": model, "untyped": {k: v for k, v in model.items() if k != "obligations"}}
    if expect == "work":
        out["untyped_direct"] = {**out["untyped"], **WRONG_DIRECT}
        out["mistyped_direct"] = {**out["untyped"], **WRONG_DIRECT, **ob(), "request_type": "explain"}
    if "completion_specs" in model:
        out["wrong_numbers"] = model
    return out


def build(overrides):
    data = copy.deepcopy(overrides)
    fields = IntentDesign.model_fields
    if "obligations" not in fields:
        data.pop("obligations", None)
    try:
        return plan(**data)
    except Exception:  # older trees: drop result kinds they do not know
        data.pop("completion_specs", None)
        return plan(**data)


async def run_case(env, case, variant):
    cid, _, prior, request, expect, _, executor_answer = case
    design = build(variants(case)[variant])
    designed_answer = design.answer
    task_id = None
    for (_, asked), (_, answered) in zip(prior[::2], prior[1::2]):  # earlier selective turns
        event(env, asked)
        selector, _, earlier = await select(env, plan(answer=answered), request=asked)
        earlier.verdict(answered)
        event(env, answered, actor="assistant")
        selector.finish(earlier, [], stop_reason="completed", had_injections=False)
        task_id = (earlier.state or {}).get("task_id") or task_id
    if design.task_relation in {"continue", "change", "resume"}:
        design = design.model_copy(update={"task_id": task_id})
    event(env, request)
    _, provider, selected = await select(env, design, request=request,
                                         responses=[response(design), response(design)])
    repairs = len(provider.requests) - 1
    if selected.blocked:
        return "blocked", repairs
    if variant == "wrong_numbers":
        text = GOLF_T2_WRONG
    elif selected.design.route == "direct" and selected.design.answer:
        text = selected.design.answer
    else:
        text = executor_answer or designed_answer or "답변"
    out, status = selected.verdict(text)
    done = status == "completed" and not out.startswith("요청을 완료하지 못했습니다")
    return ("complete" if done else "incomplete"), repairs


def expected(case, variant):
    return "work" if variant == "wrong_numbers" else case[4]


PARAMS = [(c, v) for c in CASES for v in variants(c)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "variant"), PARAMS, ids=[f"{c[0]}-{v}" for c, v in PARAMS])
async def test_contrast(env, case, variant):
    outcome, repairs = await run_case(env, case, variant)
    if expected(case, variant) == "work":
        assert outcome != "complete"
    elif variant == "model":  # typed designs: no false block, no repair call
        assert outcome == "complete" and repairs == 0
    else:  # untyped designs keep the conservative S1 floor; never blocked, never repaired
        assert outcome != "blocked" and repairs == 0


def rates(results):
    comp = [r for r in results if r["expect"] == "complete"]
    work = [r for r in results if r["expect"] == "work"]
    fb = [r for r in comp if r["outcome"] != "complete"]
    fc = [r for r in work if r["outcome"] == "complete"]
    return {"complete_cases": len(comp), "false_block": len(fb), "work_cases": len(work),
            "false_complete": len(fc), "repairs": sum(r["repairs"] for r in results),
            "false_block_ids": [r["id"] for r in fb], "false_complete_ids": [r["id"] for r in fc]}


def main():  # pragma: no cover - measurement helper
    import json
    import sys
    import tempfile
    from pathlib import Path

    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.skills import SkillsLoader
    from nanobot.memory.event_store import ConversationEventStore
    from nanobot.memory.models import MemoryScope

    from .test_context_selection import ChangeTool, ReadTool

    results = []
    for case, variant in PARAMS:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            registry = ToolRegistry()
            (tmp / "current-test-config.txt").write_text("current config")
            registry.register(ReadTool(tmp))
            registry.register(ChangeTool())
            e = (ConversationEventStore(tmp), MemoryScope.from_runtime(
                owner_id="owner", workspace_id=str(tmp), agent_id=None), registry, SkillsLoader(tmp))
            outcome, repairs = asyncio.run(run_case(e, case, variant))
        results.append({"id": f"{case[0]}-{variant}", "group": case[1], "variant": variant,
                        "expect": expected(case, variant), "outcome": outcome, "repairs": repairs})
    summary = {"all": rates(results)}
    for v in sorted({r["variant"] for r in results}):
        summary[v] = rates([r for r in results if r["variant"] == v])
    json.dump({"summary": summary, "results": results}, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":  # pragma: no cover
    main()
