"""t47 MOCK scenarios through the whole execution loop (AgentRunner + real selection/evidence/plan code
+ real file tools). The executor's decisions are SCRIPTED (MOCK model); they show that the loop connects
'evaluate result -> revise the needed part of the plan -> change method -> re-execute', not real model
performance. Every model request is counted: no evaluator or extra model call is added."""
import json

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401

def META_for(path):
    return {"intent_summary": "write the requested file", "target": path, "scope": "once",
            "reversible": True}


class Script(LLMProvider):
    """MOCK executor: each step sees the messages so far and returns the next response."""

    def __init__(self, steps):
        super().__init__()
        self.steps, self.requests = list(steps), []

    def get_default_model(self):
        return "MOCK"

    async def chat(self, messages, **kw):
        self.requests.append(messages)
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step


def calls(*items):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=i, name=n, arguments=a) for i, n, a in items],
                       usage={})


def final(text):
    return LLMResponse(content=text, usage={})


def tool_text(messages, cid):
    return next((str(m.get("content")) for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == cid),
                None)


def fs(env, tmp_path):
    from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
    for t in (WriteFileTool(workspace=tmp_path), ReadFileTool(workspace=tmp_path)):
        env[2].unregister(t.name) if env[2].get(t.name) is not None else None   # replace the fake reader
        env[2].register(t)


async def run(sel, steps, request, max_iterations=14):
    prov = Script(steps)
    result = await AgentRunner(prov).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": request}], tools=sel.tools, model="offline",
        max_iterations=max_iterations, max_tool_result_chars=4000, selected_context=sel))
    return prov, result


def progress(**p):
    base = {"obtained": "", "remaining": [], "plan_valid": True, "next_action": "continue"}
    return {"reason": "update_plan", "information_needs": [], "completion_checks": [], "progress": base | p}


def ok_writes(sel):
    return [o for o in sel.observations.values() if o.get("name") == "write_file" and o.get("status") == "ok"]


async def two_files(env, tmp_path, a="t47_a.md", b="t47_b.md"):
    from nanobot.agent.context_selection import Obligations, RelationSpec
    fs(env, tmp_path)
    text = f"{a} 파일과 {b} 파일을 만들어서 각각 '우유 사기', '빵 사기'를 적어 줘."
    event(env, text)
    rels = [RelationSpec(action="write", target="external_resource", necessity="required", source_span=s, target_ref=r)
            for s, r in ((f"{a} 파일과", a), (f"{b} 파일을 만들어서", b))]
    design = plan(route="execute", answer=None, completion_checks=[], capabilities_needed=["write_file", "read_file"],
                  obligations=Obligations(target="external_resource", external_change="required", relations=rels))
    _, _, sel = await select(env, design, request=text)                       # MOCK design
    return sel, text


def rid(sel, name):
    return next(r for r in __import__("nanobot.agent.context_plan", fromlist=["x"]).requirement_ids(sel)
                if r.endswith("/" + name))


@pytest.mark.asyncio
async def test_b_create_exist_content_partial_fix_no_recreate(env, tmp_path):
    sel, text = await two_files(env, tmp_path)
    A, B = str(tmp_path / "t47_a.md"), str(tmp_path / "t47_b.md")
    seen = {}

    def evaluate(msgs):
        seen["rc_b"] = tool_text(msgs, "rc_b")              # content check of b failed in code
        seen["plan_before"] = json.loads(json.dumps(sel.exec_plan, default=str))
        return calls(("up1", "revise_context_requirements", progress(
                         obtained="a 내용 확인 통과; b 내용 확인 실패(빵 샀기)", remaining=[rid(sel, "t47_b.md")],
                         plan_valid=False, next_action="revise_plan", cause="partial_error",
                         affected=[rid(sel, "t47_b.md")], change_reason="b의 오타 줄만 고친다")),
                     ("wb_again", "write_file", {"path": B, "content": "빵 샀기\n", **META_for(B)}))  # identical: gated

    steps = [
        calls(("wa", "write_file", {"path": A, "content": "우유 사기\n", **META_for(A)}),
              ("wb", "write_file", {"path": B, "content": "빵 샀기\n", **META_for(B)})),
        calls(("ea", "record_context_completion", {"check": "a 생성", "tool_call_ids": ["wa"], "status": "passed",
               "finding": "written", "spec": {"result_kind": "mutation_result", "target": "t47_a.md"}}),
              ("ra", "read_file", {"path": A}), ("rb", "read_file", {"path": B})),
        calls(("rc_a", "record_context_completion", {"check": "a 내용", "tool_call_ids": ["ra"], "status": "passed",
               "finding": "우유 사기", "spec": {"result_kind": "file_content", "target": "t47_a.md",
                                                 "expected_values": {"contains": "우유 사기"}}}),
              ("rc_b", "record_context_completion", {"check": "b 내용", "tool_call_ids": ["rb"], "status": "passed",
               "finding": "빵 사기", "spec": {"result_kind": "file_content", "target": "t47_b.md",
                                               "expected_values": {"contains": "빵 사기"}}})),
        evaluate,
        calls(("wb2", "write_file", {"path": B, "content": "빵 사기\n", **META_for(B)})),
        calls(("rb2", "read_file", {"path": B})),
        calls(("rc_b2", "record_context_completion", {"check": "b 내용", "tool_call_ids": ["rb2"], "status": "passed",
               "finding": "빵 사기", "spec": {"result_kind": "file_content", "target": "t47_b.md",
                                               "expected_values": {"contains": "빵 사기"}}})),
        final("t47_a.md에 '우유 사기', t47_b.md에 '빵 사기'를 적었습니다."),
    ]
    prov, result = await run(sel, steps, text)
    assert result.stop_reason == "completed", (result.stop_reason, result.final_content)
    assert len(prov.requests) == len(steps)                      # no extra model call
    assert "expected text is not in current target" in seen["rc_b"]                # first failure: content check rejected
    gate = tool_text(result.messages, "wb_again")
    assert "already applied" in gate                             # identical re-creation not executed
    assert [o["tool_call_id"] for o in ok_writes(sel)] == ["wa", "wb", "wb2"]
    p = sel.exec_plan
    assert p["version"] == 2 and p["last_change"]["cause"] == "partial_error"
    assert p["last_change"]["affected"] == [rid(sel, "t47_b.md")]
    assert rid(sel, "t47_a.md") in p["last_change"]["kept_done"]  # a preserved, not re-verified
    assert "a 내용" in sel.completion_reports and sel.completion_reports["a 내용"]["status"] == "passed"
    assert (tmp_path / "t47_b.md").read_text() == "빵 사기\n"
    (tmp_path / "t47_plan_example.json").write_text(json.dumps(
        {"before": seen["plan_before"], "after": json.loads(json.dumps(p, default=str))}, ensure_ascii=False))


async def one_file(env, tmp_path, name, text=None, **design):
    fs(env, tmp_path)
    text = text or f"{name} 파일을 만들어서 '완료'를 적어 줘."
    event(env, text)
    caps = design.pop("capabilities_needed", ["write_file", "read_file"])
    from nanobot.agent.context_selection import Obligations, RelationSpec
    span = text[text.index(name):text.index(name) + len(name) + 4]
    rel = RelationSpec(action="write", target="external_resource", necessity="required", source_span=span,
                       target_ref=name)
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[], capabilities_needed=caps,
                                       obligations=Obligations(target="external_resource", external_change="required",
                                                               relations=[rel]), **design), request=text)  # MOCK
    assert [r["target"] for r in sel.required_results() if r["kind"] == "file_change"] == [str(tmp_path / name)]
    return sel, text


def write(cid, path, content):
    return (cid, "write_file", {"path": path, "content": content, **META_for(path)})


def check(cid, name, ids, kind, target, contains=None):
    spec = {"result_kind": kind, "target": target} | ({"expected_values": {"contains": contains}} if contains else {})
    return (cid, "record_context_completion", {"check": name, "tool_call_ids": ids, "status": "passed",
                                               "finding": "observed", "spec": spec})


@pytest.mark.asyncio
async def test_d_links_existing_evidence_without_new_lookup(env, tmp_path):
    sel, text = await one_file(env, tmp_path, "t47_d.md")
    path = str(tmp_path / "t47_d.md")
    steps = [calls(write("w", path, "완료\n")), calls(("r", "read_file", {"path": path})),
             calls(check("c1", "d 내용", ["w"], "file_content", "t47_d.md", "완료")),       # wrong evidence cited
             calls(("up", "revise_context_requirements", progress(
                 obtained="읽기 결과 r에 '완료' 있음; 기록이 쓰기 호출을 인용함", next_action="continue",
                 cause="evidence_link_missing", evidence_calls=["r"], change_reason="이미 얻은 읽기 결과를 연결")),
                   check("c2", "d 내용", ["r"], "file_content", "t47_d.md", "완료")),
             final("t47_d.md에 '완료'를 적었습니다.")]
    prov, result = await run(sel, steps, text)
    first = tool_text(result.messages, "c1")
    assert result.stop_reason == "completed", result.final_content
    assert len(prov.requests) == len(steps)
    assert "Error" in first or "not" in first                        # first failure: evidence not linkable
    reads = [o for o in sel.observations.values() if o.get("name") == "read_file"]
    assert len(reads) == 1 and len(ok_writes(sel)) == 1              # no new lookup, no re-write
    assert sel.exec_plan["last_change"]["evidence_calls"] == ["r"]
    assert (tmp_path / "t47_d.md").read_text() == "완료\n"


class Fetch(__import__("nanobot.agent.tools.base", fromlist=["Tool"]).Tool):
    """MOCK official sources: one host fails at transport level, a second official host answers."""
    name = "web_fetch"
    description = "Fetch a URL"
    read_only = True
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    pages = {"https://docs.example.org/a": "항목 A: 10", "https://mirror.example.org/b": "항목 B: 20"}

    def __init__(self):
        self.urls = []

    async def execute(self, url, **kw):
        self.urls.append(url)
        if url == "https://docs.example.org/b":
            return "Error: connection reset by peer"
        return self.pages.get(url, "Error: not found")


@pytest.mark.asyncio
async def test_c_transport_failure_other_official_source_fills_only_missing(env, tmp_path):
    tool = Fetch()
    env[2].register(tool)
    sel, text = await one_file(env, tmp_path, "t47_c.md", text="공식 문서의 항목 A와 항목 B 값을 t47_c.md 파일에 적어 줘.",
                               capabilities_needed=["web_fetch", "write_file", "read_file"])
    path = str(tmp_path / "t47_c.md")
    seen = {}

    def retry_limit(msgs):
        return calls(("up2", "revise_context_requirements", progress(
            obtained="B 재시도도 연결 실패", next_action="continue", cause="transient_error", failed_call="fb2",
            change_reason="한 번 더 재시도")))

    def switch(msgs):
        seen["limit"] = tool_text(msgs, "up2")
        return calls(("up3", "revise_context_requirements", progress(
            obtained="A=10 확보, B는 docs 호스트 연결 실패", remaining=[], plan_valid=False,
            next_action="other_method", cause="unsuitable_approach", failed_call="fb2",
            change_reason="같은 문서의 다른 공식 호스트에서 B만 가져온다")),
            ("fb3", "web_fetch", {"url": "https://mirror.example.org/b"}))

    steps = [calls(("fa", "web_fetch", {"url": "https://docs.example.org/a"}),
                   ("fb", "web_fetch", {"url": "https://docs.example.org/b"})),
             calls(("up1", "revise_context_requirements", progress(
                 obtained="A=10, B 연결 실패", next_action="continue", cause="transient_error", failed_call="fb",
                 change_reason="일시적 연결 실패 재시도")),
                   ("fb2", "web_fetch", {"url": "https://docs.example.org/b"})),
             retry_limit, switch,
             calls(write("w", path, "항목 A: 10\n항목 B: 20\n")), calls(("r", "read_file", {"path": path})),
             calls(check("c", "c 내용", ["r"], "file_content", "t47_c.md", "항목 B: 20")),
             final("t47_c.md에 항목 A: 10, 항목 B: 20을 적었습니다.")]
    prov, result = await run(sel, steps, text)
    assert result.stop_reason == "completed", result.final_content
    assert len(prov.requests) == len(steps)
    assert "retry limit is reached" in seen["limit"]                  # bounded retries
    assert tool.urls.count("https://docs.example.org/a") == 1        # obtained item not fetched again
    assert tool.urls.count("https://docs.example.org/b") == 2
    assert tool.urls[-1] == "https://mirror.example.org/b"
    assert sel.exec_plan["last_change"]["cause"] == "unsuitable_approach"
    assert (tmp_path / "t47_c.md").read_text() == "항목 A: 10\n항목 B: 20\n"


class Memory(__import__("nanobot.agent.tools.base", fromlist=["Tool"]).Tool):
    """MOCK history lookup (stands in for the real memory tool)."""
    name = "search_memory"
    description = "Search conversation history"
    read_only = True
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self, found):
        self.found = found

    async def execute(self, query, **kw):
        return self.found or "No results found"


@pytest.mark.parametrize("found", ["[과거 대화] 회의는 금요일 15시, 장소는 3층 회의실", None])
@pytest.mark.asyncio
async def test_e_earlier_content_looked_up_then_used_or_asked(env, tmp_path, found):
    env[2].register(Memory(found))
    item = "저장할 내용(아까 말한 내용)"
    sel, text = await one_file(env, tmp_path, "t47_e.md", text="아까 말한 내용을 t47_e.md 파일에 저장해 줘.",
                               capabilities_needed=["search_memory", "write_file", "read_file"], unresolved=[item])
    path = str(tmp_path / "t47_e.md")
    settle = {"reason": "settle_unresolved", "information_needs": [], "completion_checks": [],
              "settle_unresolved": [{"text": item, "aspect": "content", "basis_span": "아까 말한 내용",
                                     "disposition": "already_resolved" if found else "missing_user_content",
                                     "evidence_calls": ["s"] if found else []}]}
    if found:
        steps = [calls(("s", "search_memory", {"query": "아까 말한 내용"})), calls(("st", "revise_context_requirements",
                 settle)), calls(write("w", path, "회의는 금요일 15시, 장소는 3층 회의실\n")),
                 calls(("r", "read_file", {"path": path})),
                 calls(check("c", "e 내용", ["r"], "file_content", "t47_e.md", "금요일 15시")),
                 final("t47_e.md에 저장했습니다: 회의는 금요일 15시, 장소는 3층 회의실.")]
    else:
        steps = [calls(("s", "search_memory", {"query": "아까 말한 내용"})), calls(("st", "revise_context_requirements",
                 settle)), calls(write("w", path, "(추측한 내용)\n")),
                 final("이전 대화에서 저장할 내용을 찾지 못했습니다. 어떤 내용을 저장할지 알려 주세요.")]
    prov, result = await run(sel, steps, text)
    assert len(prov.requests) == len(steps)
    if found:
        assert result.stop_reason == "completed", result.final_content
        assert (tmp_path / "t47_e.md").read_text().startswith("회의는 금요일 15시")
    else:
        assert not (tmp_path / "t47_e.md").exists()                  # nothing invented or written
        assert "Error" in tool_text(result.messages, "w") or "unresolved" in tool_text(result.messages, "w")
        assert result.stop_reason != "completed"


@pytest.mark.asyncio
async def test_g_no_progress_gate_then_budget_stop_preserves_effects(env, tmp_path):
    sel, text = await one_file(env, tmp_path, "t47_g.md")
    path, src = str(tmp_path / "t47_g.md"), str(tmp_path / "t47_src.md")
    steps = [calls(("q1", "read_file", {"path": src})), calls(("q2", "read_file", {"path": src})),
             calls(("q3", "read_file", {"path": src}), write("w", path, "완료\n")),   # gated; unrelated write runs
             calls(("q4", "read_file", {"path": src + ".bak"})),                      # budget runs out here
             final("unreachable")]
    prov, result = await run(sel, steps, text, max_iterations=4)
    gate = tool_text(result.messages, "q3")
    assert gate.startswith("No progress") and "q1, q2" in gate
    assert tool_text(result.messages, "w").startswith("Successfully wrote")
    assert result.stop_reason != "completed"
    assert (tmp_path / "t47_g.md").read_text() == "완료\n"                   # effect preserved after the stop
    saved = sel.state.get("execution_plan")                     # mirrored into the existing task record
    assert saved and any(f["call"] == "w" and f["changed"] for f in saved["facts"])
    assert saved["no_progress"] and saved["no_progress"][0]["calls"] == ["q1", "q2"]
    assert len(prov.requests) == 4


@pytest.mark.asyncio
async def test_h_verifier_error_does_not_deny_change_or_repeat_it(env, tmp_path, monkeypatch):
    from nanobot.agent import context_selection as cs
    sel, text = await one_file(env, tmp_path, "t47_h.md")
    path = str(tmp_path / "t47_h.md")
    real = cs.RecordContextCompletionTool._execute

    async def broken(self, check, *a, **k):                      # MOCK verifier defect for one check
        if check == "h 내용":
            raise RuntimeError("verifier crashed: unexpected spec shape")
        return await real(self, check, *a, **k)
    monkeypatch.setattr(cs.RecordContextCompletionTool, "_execute", broken)
    steps = [calls(write("w", path, "완료\n")), calls(("r", "read_file", {"path": path})),
             calls(check("c1", "h 내용", ["r"], "file_content", "t47_h.md", "완료")),
             calls(("up", "revise_context_requirements", progress(
                 obtained="쓰기 성공, 읽기에 '완료' 있음; 검증기 오류", next_action="final_report",
                 cause="verifier_defect", check="h 내용", change_reason="검증기 결함 기록, 작업 반복 안 함")),
                   check("c2", "h 내용", ["r"], "file_content", "t47_h.md", "완료")),
             final("t47_h.md에 '완료'를 적었습니다. 내용 검증기는 오류로 확인하지 못했습니다.")]
    prov, result = await run(sel, steps, text)
    assert "verifier crashed" in tool_text(result.messages, "c1")
    assert "verifier defect" in tool_text(result.messages, "c2")       # not re-recorded
    assert [o["tool_call_id"] for o in ok_writes(sel)] == ["w"]        # change neither denied nor repeated
    assert (tmp_path / "t47_h.md").read_text() == "완료\n"
    assert result.stop_reason != "completed"                           # honest: unverified, not failed work
    assert "verifier_error:h 내용" in result.final_content
    assert "t47_h.md에 '완료'를 적었습니다" in result.final_content
    assert len(prov.requests) == len(steps)


@pytest.mark.asyncio
async def test_f_user_cancels_one_target_mid_run(env, tmp_path):
    sel, text = await two_files(env, tmp_path, "t47_fa.md", "t47_fb.md")
    A, B = str(tmp_path / "t47_fa.md"), str(tmp_path / "t47_fb.md")
    sent = []

    async def inject(**kw):
        if any(m for m in sel.observations.values() if m.get("tool_call_id") == "wa") and not sent:
            sent.append(1)
            event(env, "t47_fb.md는 만들지 말고 취소해 줘.")
            return [{"role": "user", "content": "t47_fb.md는 만들지 말고 취소해 줘."}]
        return []

    def after_change(msgs):
        return final("t47_fa.md에 '우유 사기'를 적었고, 요청대로 t47_fb.md는 만들지 않았습니다.")

    steps = [calls(write("wa", A, "우유 사기\n")), after_change]
    prov = Script(steps)
    result = await AgentRunner(prov).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": text}], tools=sel.tools, model="offline", max_iterations=6,
        max_tool_result_chars=4000, selected_context=sel, injection_callback=inject))
    assert sent and not (tmp_path / "t47_fb.md").exists()
    assert (tmp_path / "t47_fa.md").read_text() == "우유 사기\n"             # completed change kept
    assert [o["tool_call_id"] for o in ok_writes(sel)] == ["wa"]
    assert "new_user_instruction_executor_reconciliation" in sel.trace.reasons
    assert any(f["call"] == "wa" and f["changed"] for f in sel.exec_plan["facts"])
    assert result.stop_reason == "cancelled"                              # existing cancel path; no rollback
    assert "이미 완료된 실행(취소로 되돌리지 않음): write_file [wa]" in result.final_content


GOLF = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "t24_replay" / "A_golf_s1-t2.json")
                  .read_text())


@pytest.mark.asyncio
async def test_a_golf_partial_time_total_error_fixed_in_loop(env):
    """REAL-RECORD part: request and computed spec come from the recorded golf t2 design. MOCK part: the
    design call returns only that computed check, and the executor's answers are scripted."""
    computed = next(c for c in GOLF["design"]["completion_specs"] if c["result_kind"] == "computed")
    text = GOLF["request"]
    event(env, text)
    _, _, sel = await select(env, plan(route="execute", answer=None, request_type="modify",
                                       completion_checks=[computed["check"]], completion_specs=[computed]),
                             request=text)
    seen = {}
    bad = ("45분 계획 (드라이버 제외)\n퍼팅 20분 - 목표: 1m 퍼트 10개 연속\n어프로치 15분 - 목표: 2m 안 5개\n"
           "아이언 12.5분 - 목표: 방향 일정\n합계 45분")

    def fix(msgs):
        seen["feedback"] = msgs[-1]["content"]
        return final(bad.replace("어프로치 15분", "어프로치 12.5분"))

    prov, result = await run(sel, [final(bad), fix], text)
    assert len(prov.requests) == 2                                   # the fix is the executor's next call
    assert seen["feedback"].startswith("Answer checks failed in code") and computed["check"] in seen["feedback"]
    assert result.stop_reason == "completed", result.final_content
    assert "어프로치 12.5분" in result.final_content and "퍼팅 20분" in result.final_content
    f = sel.exec_plan["failures"][-1]
    assert f["error_kind"] == "answer_check_mismatch" and f["cause"] == "partial_error"


@pytest.mark.asyncio
async def test_a_golf_second_wrong_answer_is_reported_not_looped(env):
    computed = next(c for c in GOLF["design"]["completion_specs"] if c["result_kind"] == "computed")
    event(env, GOLF["request"])
    _, _, sel = await select(env, plan(route="execute", answer=None, completion_checks=[computed["check"]],
                                       completion_specs=[computed]), request=GOLF["request"])
    bad = "퍼팅 20분\n어프로치 15분\n아이언 12.5분\n합계 45분"
    prov, result = await run(sel, [final(bad), final(bad)], GOLF["request"])
    assert len(prov.requests) == 2 and result.stop_reason != "completed"
