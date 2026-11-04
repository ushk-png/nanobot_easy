"""Obligations are not decided by bare words or by request_type (selective context only)."""
import pytest

from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401

GOLF_T1 = ("주말에 골프 연습을 60분 하려고 합니다. 퍼팅 20분, 어프로치 20분, 아이언 20분으로 계획해 주세요. "
           "드라이버 연습은 제외하고, 시간표와 각 연습의 목표를 하나씩 적어 주세요. 인터넷 검색은 필요 없습니다.")
GOLF_T2 = ("방금 계획에서 전체 시간을 45분으로 줄여 주세요. 퍼팅 20분은 그대로 유지하고, 남은 시간은 "
           "어프로치와 아이언에 똑같이 배분해 주세요. 드라이버 제외 조건도 유지해 주세요.")


@pytest.mark.asyncio
async def test_negated_search_word_does_not_create_an_obligation(env):
    event(env, GOLF_T1)
    _, provider, selected = await select(env, plan(answer="시간표: 퍼팅 20분 ..."), request=GOLF_T1)
    assert selected.blocked is None and len(provider.requests) == 1
    assert selected.answer_only() and selected.completion_supported()
    text, status = selected.verdict("시간표: 퍼팅 20분 ...")
    assert status == "completed" and not text.startswith("요청을 완료하지 못했습니다")


@pytest.mark.asyncio
async def test_modify_previous_answer_is_direct_without_repair(env):
    event(env, GOLF_T2)
    _, provider, selected = await select(env, plan(request_type="modify", answer="퍼팅 20분, 어프로치 12.5분"),
                                         request=GOLF_T2)
    assert selected.blocked is None and len(provider.requests) == 1
    assert selected.design.route == "direct"
    assert selected.answer_only() and selected.completion_supported()


@pytest.mark.asyncio
async def test_imperative_file_change_still_needs_execution(env):
    event(env, "config.json 파일을 수정해 주세요")
    _, provider, selected = await select(env, plan(request_type="modify", answer="수정했습니다"),
                                         request="config.json 파일을 수정해 주세요")
    assert len(provider.requests) == 1  # validator class: rerouted, never repaired
    assert selected.design.route == "execute"
    assert not selected.answer_only() and not selected.completion_supported()


# ---- stage 2: typed obligations, code floors, lookup gate ----
from nanobot.agent.context_selection import (  # noqa: E402
    IntentDesign, Obligations, normalize_obligations, trusted_obligations, validate_meaning,
)


def _payload(request, prior=True, clues=()):
    sources = [{"event_id": "cur", "actor": "user", "content": request, "sequence": 3}]
    if prior:
        sources[:0] = [{"event_id": "u1", "actor": "user", "content": "골프 60분 계획, 드라이버 연습은 제외", "sequence": 1},
                       {"event_id": "a1", "actor": "assistant", "content": "퍼팅 20분 ...", "sequence": 2}]
    return {"request": request, "sources": sources, "capabilities": [{"name": "web_search"}],
            "task_candidates": [], "references": {}, "known_material_clues": list(clues)}


def _ob(**kw):
    return Obligations(**kw)


def test_old_designs_without_obligations_still_validate():
    data = plan().model_dump()
    data.pop("obligations")
    assert IntentDesign.model_validate(data).obligations is None


def test_prior_answer_edit_is_direct_but_untyped_stays_rerouted():
    req = "위 답변에서 아이언 부분을 수정해 주세요"
    typed = plan(request_type="modify", answer="수정된 계획 전체",
                 obligations=_ob(target="prior_answer"))
    assert validate_meaning(typed, _payload(req)) == []
    # Relation level: "위 답변 ... 수정" is an edit of the real previous answer even untyped.
    assert validate_meaning(plan(answer="수정된 계획 전체"), _payload(req)) == []
    # Without an earlier answer in the input the prior_answer target is unresolved: reroute, no repair.
    assert "relation_requires_work" in validate_meaning(typed, _payload(req, prior=False))
    assert "relation_requires_work" in validate_meaning(plan(answer="x"), _payload(req, prior=False))


def test_external_change_and_required_lookup_cannot_be_direct():
    req = "config.yaml 파일을 수정해 주세요"
    d = plan(answer="x", obligations=_ob(target="external_resource", external_change="required",
                                         source_span="파일을 수정해 주세요"))
    assert "unsafe_direct" in validate_meaning(d, _payload(req))
    d = plan(answer="x", obligations=_ob(lookup="required", source_span="최신 가격을 검색해"))
    assert "unsafe_direct" in validate_meaning(d, _payload("최신 가격을 검색해 줘"))


def test_named_file_not_in_input_blocks_direct_even_untyped():
    assert "unsafe_direct" in validate_meaning(plan(answer="설명"), _payload("settings.toml 설명해줘", prior=False))
    assert "unsafe_direct" not in validate_meaning(
        plan(answer="설명"), _payload("settings.toml 설명해줘", prior=False, clues=["settings.toml"]))


def test_span_is_required_to_trust_non_default_obligations():
    d = plan(obligations=_ob(lookup="conditional"))
    assert trusted_obligations(d, "필요하면 검색해") is None
    d = plan(obligations=_ob(lookup="conditional", source_span="필요하면 검색해"))
    assert trusted_obligations(d, "필요하면 검색해") is not None
    d = plan(obligations=_ob(lookup="conditional", source_span="invented text here"))
    assert trusted_obligations(d, "필요하면 검색해") is None


def test_forbidden_lookup_conflicts_with_web_capability():
    d = plan(route="execute", answer=None, capabilities_needed=["web_search"],
             obligations=_ob(lookup="forbidden", source_span="검색하지 마"))
    assert "lookup_conflict" in validate_meaning(d, _payload("검색하지 마"))


def test_unsupported_keep_change_constraints_are_dropped_not_repaired():
    d = plan(obligations=_ob(keep_constraints=["드라이버 제외 조건도 유지", "벙커 연습 추가"]))
    assert normalize_obligations(d, _payload("드라이버 제외 조건도 유지해 주세요.")) == 1
    assert d.obligations.keep_constraints == ["드라이버 제외 조건도 유지"]


@pytest.mark.asyncio
async def test_lookup_gate_blocks_web_tools_only_when_forbidden(env):
    event(env, "인터넷 검색은 하지 마세요. 설명만 해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None,
        obligations=_ob(lookup="forbidden", source_span="인터넷 검색은 하지 마세요")),
        request="인터넷 검색은 하지 마세요. 설명만 해줘")
    assert selected.lookup_gate("web_search") and selected.lookup_gate("web_fetch")
    assert selected.lookup_gate("read_file") is None


@pytest.mark.asyncio
async def test_typed_answer_only_follows_obligations_and_mutation_floor(env):
    req = "TCP와 UDP 차이를 알려줘. 필요하면 검색해"
    event(env, req)
    _, provider, selected = await select(env, plan(answer="TCP는 ...",
        obligations=_ob(lookup="conditional", source_span="필요하면 검색해")), request=req)
    assert len(provider.requests) == 1
    assert selected.design.route == "direct"   # "필요하면 검색해" is a conditional lookup, not a duty
    assert selected.answer_only()               # and the typed obligation needs no tool proof
    selected.observations["w1"] = {"read_only": False, "status": "ok"}
    assert not selected.answer_only()           # floor: a state change happened


@pytest.mark.asyncio
async def test_typed_external_change_needs_execution(env):
    req = "notes.md 파일에 요약을 저장해 주세요"
    event(env, req)
    _, _, selected = await select(env, plan(route="execute", answer=None,
        obligations=_ob(target="external_resource", external_change="required",
                        source_span="요약을 저장해 주세요")), request=req)
    assert not selected.answer_only() and not selected.completion_supported()


# ---- stage 3: result-kind completion, computed checks in code ----
from nanobot.agent.context_evidence import completion_kind, validate_completion  # noqa: E402
from nanobot.agent.context_selection import CompletionSpec, computed_mismatch  # noqa: E402

GOLF_EXPECTED = {"values": {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, "total": 45, "excluded": ["드라이버"],
                 "not_allocated": ["드라이버"]}   # t36: "the user removed it" is declared, not inferred
GOLF_ANSWER = ("총 45분 계획\n0:00-20:00 퍼팅 20분 - 목표: 2m 퍼트 성공률\n"
               "20:00-32:30 어프로치 12.5분 - 목표: 거리감\n32:30-45:00 아이언 12분 30초 - 목표: 정타\n"
               "드라이버 연습은 제외했습니다.")


@pytest.mark.parametrize(("answer", "reason"), [
    (GOLF_ANSWER, None),
    (GOLF_ANSWER.replace("어프로치 12.5분", "어프로치 13분").replace("20:00-32:30", "20:00-33:00"),
     "computed_value_not_in_answer:어프로치"),
    (GOLF_ANSWER.replace("총 45분", "총 시간").replace("32:30-45:00", "32:30-끝"), "computed_total_not_in_answer"),
    (GOLF_ANSWER + "\n드라이버 5분", "computed_excluded_item_allocated:드라이버"),
])
def test_computed_values_checked_against_answer(answer, reason):
    assert computed_mismatch(GOLF_EXPECTED, answer) == reason


def test_inconsistent_declared_values_fail_without_repair():
    bad = {"values": {"퍼팅": 20, "어프로치": 15, "아이언": 15}, "total": 45}
    assert computed_mismatch(bad, GOLF_ANSWER) == "computed_values_do_not_sum_to_total"


@pytest.mark.asyncio
async def test_golf_t2_computed_spec_completes_only_when_numbers_hold(env):
    event(env, GOLF_T2)
    design = plan(request_type="modify", answer=GOLF_ANSWER, completion_checks=["45분 배분"],
                  completion_specs=[CompletionSpec(check="45분 배분", result_kind="computed",
                                                   expected_values=GOLF_EXPECTED)])
    _, _, selected = await select(env, design, request=GOLF_T2)
    text, status = selected.verdict(GOLF_ANSWER)
    assert status == "completed"
    _, _, selected = await select(env, design, request=GOLF_T2)
    wrong = GOLF_ANSWER.replace("12.5분", "15분").replace("12분 30초", "10분")
    text, status = selected.verdict(wrong)
    assert status == "incomplete" and "45분 배분" in text


@pytest.mark.asyncio
async def test_completion_kind_spec_then_obligations_then_legacy(env):
    event(env, "설명해줘")
    _, _, selected = await select(env, plan(completion_checks=["테스트 방법 설명"]), request="설명해줘")
    assert completion_kind("테스트 방법 설명", selected) == "test_result"  # untyped legacy rule kept
    selected.design.obligations = Obligations()
    assert completion_kind("테스트 방법 설명", selected) == "answer_content"
    selected.design.obligations = Obligations(lookup="required", source_span="설명해줘")
    assert completion_kind("테스트 방법 설명", selected) == "sourced"
    selected.design.completion_specs = [CompletionSpec(check="테스트 방법 설명", result_kind="semantic")]
    assert completion_kind("테스트 방법 설명", selected) == "semantic"


@pytest.mark.asyncio
async def test_sourced_check_needs_a_successful_read(env):
    req = "최신 파이썬 버전을 검색해서 알려줘"
    event(env, req)
    _, _, selected = await select(env, plan(route="execute", answer=None, completion_checks=["버전 확인"],
        obligations=Obligations(lookup="required", source_span="검색해서 알려줘")), request=req)
    assert completion_kind("버전 확인", selected) == "sourced"
    assert validate_completion("버전 확인", [], selected)


@pytest.mark.asyncio
async def test_typed_external_change_replaces_request_type_rule(env):
    req = "답변을 다시 정리해 주세요"
    event(env, req)
    # request_type modify, but typed: no external change, so no mutation is demanded
    _, _, selected = await select(env, plan(request_type="modify", obligations=Obligations()), request=req)
    selected.verdict("정리된 답변")
    assert selected.completion_supported()
    # untyped modify keeps the old rule on non-answer-only paths
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(request_type="modify", route="execute", answer=None,
        information_needs=[], completion_checks=["c"]), request="파일 수정해줘")
    assert not selected.completion_supported()


# ---- stage 4: correction record for answers a superseded rule wrapped ----
from nanobot.agent.context_selection import _is_correction  # noqa: E402
from nanobot.agent.context_trace import ContextTrace  # noqa: E402

T1_ANSWER = "0:00-20:00 퍼팅 20분 - 목표: 거리감\n20:00-40:00 어프로치 20분\n40:00-60:00 아이언 20분"


def _wrapper(checks, answer=T1_ANSWER, stop="completed"):
    return ("요청을 완료하지 못했습니다.\n종료 상태: " + stop + "\n남은 조건: " + ", ".join(checks)
            + "\n부분 응답(미검증): " + answer)


async def _old_turn(env, wrapper_checks=None, stop="completed", design=None):
    event(env, GOLF_T1)
    design = design or plan(answer=T1_ANSWER, completion_checks=["시간표 60분", "드라이버 제외"])
    selector, _, selected = await select(env, design, request=GOLF_T1)
    reply = event(env, _wrapper(wrapper_checks or design.completion_checks, stop=stop), actor="assistant")
    return selector, selected, reply


def _tasks(selector, env):
    return selector.tasks.list(env[1], "s")


@pytest.mark.asyncio
async def test_wrapped_answer_only_turn_gets_linked_correction(env):
    selector, selected, reply = await _old_turn(env)
    assert _tasks(selector, env)[0]["unfinished"]
    assert selector.reconcile_verdicts(env[1], "s", ContextTrace(request_id="r2")) == 1
    events = selector.tasks.events(env[1], "s")
    fix = [e for e in events if _is_correction(e)]
    assert len(fix) == 1 and fix[0].parent_event_id == reply.event_id
    original = next(e for e in events if e.event_id == reply.event_id)
    assert original.content.startswith("요청을 완료하지 못했습니다")  # never edited or deleted
    state = _tasks(selector, env)[0]
    assert state["status"] == "DONE" and not state["unfinished"]
    assert state["completed"][-1]["kind"] == "verdict_correction"
    # idempotent
    assert selector.reconcile_verdicts(env[1], "s", ContextTrace(request_id="r3")) == 0
    event(env, GOLF_T2)
    payload = selector.minimal_input(GOLF_T2, env[1], "s", env[2], env[3])
    shown = next(s for s in payload["sources"] if s["event_id"] == reply.event_id)
    assert shown["content"] == T1_ANSWER and shown["verdict"] == "corrected_complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["blocker", "stopped", "action", "computed"])
async def test_no_correction_when_the_turn_was_really_incomplete(env, case):
    design = None
    kwargs = {}
    if case == "blocker":
        kwargs["wrapper_checks"] = ["design_timeout"]
    if case == "stopped":
        kwargs["stop"] = "max_iterations"
    if case == "computed":
        design = plan(answer=T1_ANSWER, completion_checks=["합계"], completion_specs=[CompletionSpec(
            check="합계", result_kind="computed", expected_values={"values": {"퍼팅": 30}, "total": 30})])
    if case == "action":
        event(env, "notes.md 파일을 저장해 주세요")
        selector, _, _ = await select(env, plan(route="execute", answer=None, completion_checks=["저장"]),
                                      request="notes.md 파일을 저장해 주세요")
        event(env, _wrapper(["저장"]), actor="assistant")
    else:
        selector, _, _ = await _old_turn(env, design=design, **kwargs)
    assert selector.reconcile_verdicts(env[1], "s", ContextTrace(request_id="r2")) == 0
    assert not [e for e in selector.tasks.events(env[1], "s") if _is_correction(e)]
