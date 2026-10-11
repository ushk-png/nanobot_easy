"""Connect method evaluation to the agent turn without replacing normal chat."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider
from nanobot.reasoning.contract import parse_procedure_payload
from nanobot.reasoning.dynamic import (
    MAX_DYNAMIC_RECOVERY,
    prepare_dynamic_procedure,
    recovery_budget_exceeded,
)
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.models import Procedure, ReasoningCase
from nanobot.reasoning.registry import approved_procedures
from nanobot.reasoning.registry import procedure as approved_procedure

_PROCEDURE_MARKERS: dict[str, tuple[str, ...]] = {
    "cause-analysis": ("원인분석", "원인 분석", "왜 이런", "왜 발생", "원인이"),
    "problem-solving": ("문제 해결", "해결 방법", "어떻게 해결", "해결 방안"),
    "document-review": ("문서 검토", "보고서 검토", "문서 검토해", "검토해 줘"),
    "rule-judgment": ("규칙 판단", "규정 적용", "규정에 따라", "규칙에 따라"),
    "method-evaluation": (
        "방법이 적합",
        "방법 평가",
        "이 방법이",
        "적합한가요",
        "적합한가",
        "method evaluation",
    ),
}


def select_procedure(content: str) -> str | None:
    """Select one approved procedure. Ambiguous or unknown requests stay direct."""

    text = content.strip().lower()
    matches = [
        procedure_id
        for procedure_id, markers in _PROCEDURE_MARKERS.items()
        if any(marker.lower() in text for marker in markers)
    ]
    if len(matches) != 1 or matches[0] not in approved_procedures():
        return None
    return matches[0]


def is_method_evaluation_request(content: str) -> bool:
    return select_procedure(content) == "method-evaluation"


STAGE_REASONING_EFFORT = "none"


class ProviderStageLLM:
    """One structured call per stage. The model cannot set verification state."""

    reasoning_effort = STAGE_REASONING_EFFORT

    def __init__(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.call_records: list[dict[str, Any]] = []

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = _stage_prompt(stage)
        body = json.dumps(payload, ensure_ascii=False)
        started = time.monotonic()
        response = asyncio.run(self._complete(stage, payload, prompt=prompt, body=body))
        duration_ms = int((time.monotonic() - started) * 1000)
        content = response.content or ""
        if stage == "procedure":
            parsed, defects = parse_procedure_payload(content)
            if parsed is None:
                parsed = {"parse_defects": [item.message() for item in defects], "raw": content}
        else:
            parsed = _parse_object(content)
        usage = response.usage if isinstance(response.usage, dict) else {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        reasoning_tokens = usage.get("reasoning_tokens")
        self.call_records.append(
            {
                "call_kind": _call_kind(stage),
                "step_id": None if stage in {"procedure", "review"} else stage,
                "duration_ms": duration_ms,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "reasoning_request": _reasoning_request(self.provider, self.reasoning_effort),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": None if reasoning_tokens is None else int(reasoning_tokens),
                "cached_tokens": usage.get("cached_tokens"),
                "server_side_tools_used": usage.get("server_side_tools_used"),
                "payload_bytes": len(prompt.encode("utf-8")) + len(body.encode("utf-8")),
                "finish_reason": getattr(response, "finish_reason", None),
                "output_keys": sorted(str(key) for key in parsed if key != "raw"),
            }
        )
        parsed["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": int(reasoning_tokens or 0),
            "duration_ms": duration_ms,
        }
        return parsed

    async def _complete(
        self,
        stage: str,
        payload: dict[str, Any],
        *,
        prompt: str | None = None,
        body: str | None = None,
    ):
        prompt = prompt if prompt is not None else _stage_prompt(stage)
        body = body if body is not None else json.dumps(payload, ensure_ascii=False)
        return await self.provider.chat_with_retry(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": body},
            ],
            tools=None,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
        )


def _stage_prompt(stage: str) -> str:
    if stage == "procedure":
        return _PROCEDURE_PROMPT
    if stage == "review":
        return _REVIEW_PROMPT
    return (
        f"단계 {stage}의 JSON 객체만 반환한다. "
        "question에 답하고 expected_outputs에 적힌 키를 모두 채운다. "
        "이 단계는 검증용 짧은 판단이다. 설명, 배경, 장문 근거는 쓰지 않는다. "
        "각 문장은 40자 안팎의 한 문장으로 하고, 항목은 필요한 최소 개수만 둔다. "
        "expected_outputs에 answer가 있는 단계에서만 사용자에게 보일 최종 답을 "
        "2~4문장으로 answer에 쓴다. 그 외 단계의 answer는 비운다. "
        "repair_defects가 있으면 그 결함만 고친다. 검증 상태나 다음 단계 지시는 포함하지 않는다."
    )


def _call_kind(stage: str) -> str:
    if stage == "procedure":
        return "compose"
    if stage == "review":
        return "review"
    return "step"


def _reasoning_request(provider: Any, effort: str | None) -> Any:
    """Report the reasoning field actually placed in the request body."""

    inner = getattr(provider, "_primary", None)  # FallbackProvider wraps the real provider.
    describe = getattr(provider, "reasoning_request_options", None) or getattr(
        inner, "reasoning_request_options", None
    )
    if not callable(describe):
        return "unknown"
    return describe(effort)


async def run_reasoning_procedure(
    provider: LLMProvider,
    *,
    model: str,
    request: str,
    procedure_id: str | None = None,
    workspace: Path | None = None,
    on_progress: Callable[..., Awaitable[None]] | None = None,
) -> ReasoningCase:
    llm = ProviderStageLLM(provider, model)
    selected = approved_procedure(procedure_id) if procedure_id else None
    if selected is None:
        await _emit_progress(on_progress, "절차를 구성하고 있습니다.")
        selected, defects, trace = await _propose_dynamic_procedure(llm, request, _known_goal(request))
        if selected is None:
            case = ReasoningCase(
                id="dynamic-rejected",
                request=request,
                procedure_id="dynamic-rejected",
                procedure_version="dynamic",
                status="stopped",
                stop_reason=_user_status(defects),
            )
            _finish_calls(case, llm, None)
            _record_execution(case, selected, workspace, defects, trace)
            return case
    loop = asyncio.get_running_loop()
    pending: list[asyncio.Future[None]] = []

    def _on_stage(stage: str, state: str, label: str) -> None:
        if on_progress is None:
            return
        text = f"{label} 단계를 마쳤습니다." if state == "passed" else f"{label} 단계에서 멈췄습니다."
        pending.append(asyncio.run_coroutine_threadsafe(_emit_progress(on_progress, text), loop))

    engine = ReasoningEngine(
        llm,
        reasoning_effort="none",
        reasoning_disabled_guaranteed=False,
        on_stage=_on_stage,
    )
    case = await asyncio.to_thread(engine.run, request, procedure=selected)
    if pending:
        await asyncio.gather(*(asyncio.wrap_future(item) for item in pending))
    if case.status == "completed" and not case.answer.strip():
        case.status = "failed_empty_answer"
    if case.status == "failed_empty_answer":
        case.stop_reason = _empty_answer_status(selected)
    _finish_calls(case, llm, selected)
    _record_execution(case, selected, workspace, [])
    return case


def _finish_calls(case: ReasoningCase, llm: Any, selected: Procedure | None) -> None:
    """Attach per-call measurements to the case and log one line per call."""

    records = [dict(item) for item in getattr(llm, "call_records", [])]
    roles = {step.id: step.role for step in selected.steps} if selected else {}
    for record in records:
        record["run_id"] = case.id
        if record.get("call_kind") == "step" and roles.get(record.get("step_id")) == "answer":
            record["call_kind"] = "answer"
        logger.info("reasoning call {}", json.dumps(record, ensure_ascii=False, sort_keys=True))
    case.call_records = records
    totals = _call_totals(records)
    logger.info("reasoning run {}", json.dumps({"run_id": case.id, **totals}, sort_keys=True))
    mode = _reasoning_mode(getattr(llm, "provider", None), getattr(llm, "model", None))
    case.history.append({"event": "reasoning_mode", **mode})
    if mode["disable_supported"] is False:
        logger.warning(
            "reasoning run {}: {} does not support non-reasoning mode; "
            "reasoning_effort=none is not applied (provider default reasoning runs)",
            case.id,
            mode["model"],
        )


def _reasoning_mode(provider: Any, model: str | None) -> dict[str, Any]:
    """Design 1: show when 'reasoning off' cannot be honoured instead of hiding it."""

    inner = getattr(provider, "_primary", None)
    probe = getattr(provider, "reasoning_disable_supported", None) or getattr(
        inner, "reasoning_disable_supported", None
    )
    supported = probe(model) if callable(probe) else None
    if supported is False:
        status = "unsupported: this model has no non-reasoning mode"
    elif supported is None:
        status = "unknown: provider does not report non-reasoning support"
    else:
        status = "supported"
    return {
        "model": model,
        "requested_effort": STAGE_REASONING_EFFORT,
        "disable_supported": supported,
        "status": status,
    }


def _call_totals(records: list[dict[str, Any]]) -> dict[str, Any]:
    reasoning = [item.get("reasoning_tokens") for item in records]
    return {
        "calls": len(records),
        "duration_ms": sum(int(item.get("duration_ms") or 0) for item in records),
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in records),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in records),
        "reasoning_tokens": (
            None if all(item is None for item in reasoning)
            else sum(int(item or 0) for item in reasoning)
        ),
        "payload_bytes": sum(int(item.get("payload_bytes") or 0) for item in records),
    }


async def _propose_dynamic_procedure(llm: ProviderStageLLM, request: str, known_goal: str = ""):
    defects: list[str] = []
    trace: list[dict[str, Any]] = []
    proposal: dict[str, Any] = {}
    procedure_id: str | None = None
    for attempt in range(MAX_DYNAMIC_RECOVERY + 1):
        payload = {"request": request} if attempt == 0 else {
            "request": request,
            "repair_defects": defects,
            "repair_kind": _repair_kind(defects),
            "previous_procedure": proposal,
        }
        proposal = await asyncio.to_thread(llm.complete, "procedure", payload)
        proposal.pop("usage", None)
        if proposal.get("parse_defects"):
            defects = [str(item) for item in proposal["parse_defects"]]
            trace.append({"attempt": attempt, "defects": defects})
            if recovery_budget_exceeded(attempt):
                break
            continue
        selected, attempt_trace, contract_defects = prepare_dynamic_procedure(
            proposal,
            request=request,
            known_goal=known_goal,
            procedure_id=procedure_id,
            revision=attempt + 1,
        )
        normalized = attempt_trace.get("normalized")
        if isinstance(normalized, dict):
            proposal = normalized
            procedure_id = str(normalized.get("id") or procedure_id or "")
        public_trace = {key: value for key, value in attempt_trace.items() if key != "normalized"}
        trace.append({"attempt": attempt, **public_trace, "defects": [item.message() for item in contract_defects]})
        if selected is not None:
            # Fitness review is a separate call; the generator's output is not its evidence.
            review_defects, review_trace = await _review_procedure(llm, request, known_goal, selected)
            trace.append({"attempt": attempt, "review": review_trace, "defects": review_defects})
            if not review_defects:
                return selected, [], trace
            defects = review_defects
            if recovery_budget_exceeded(attempt):
                break
            continue
        defects = [item.message() for item in contract_defects]
        if recovery_budget_exceeded(attempt):
            break
    return None, defects, trace


async def _review_procedure(
    llm: Any,
    request: str,
    known_goal: str,
    selected: Procedure,
) -> tuple[list[str], dict[str, Any]]:
    """Ask a separate call where each request item is handled; code decides pass/fail."""

    outline = _procedure_outline(selected) or {}
    payload = {
        "request": request,
        "known_goal": known_goal,
        "procedure": {"purpose": selected.purpose, "steps": outline.get("steps", [])},
    }
    result = await asyncio.to_thread(llm.complete, "review", payload)
    result.pop("usage", None)
    defects = review_defects(result, selected)
    return defects, {
        "request_items": len(result.get("request_items") or []),
        "answer_step_id": result.get("answer_step_id"),
        "defects": defects,
    }


def review_defects(result: dict[str, Any], selected: Procedure) -> list[str]:
    """Turn the reviewer's mapping into defects. The reviewer has no pass flag."""

    steps = {step.id: step for step in selected.steps}
    defects: list[str] = []
    items = result.get("request_items")
    if not isinstance(items, list) or not items:
        return ["review_unusable: no request items were mapped to steps"]
    for raw in items:
        item = raw if isinstance(raw, dict) else {}
        text = str(item.get("item") or "").strip() or "unnamed item"
        step_id = str(item.get("step_id") or "").strip()
        if step_id not in steps:
            defects.append(f"review_unhandled: no step handles '{text}'")
    answer_id = str(result.get("answer_step_id") or "").strip()
    answer_step = steps.get(answer_id)
    if answer_step is None or "answer" not in answer_step.outputs:
        defects.append("review_no_answer_step: no step writes the final answer")
    for raw in result.get("defects") or []:
        if str(raw).strip():
            defects.append(f"review_defect: {str(raw).strip()}")
    return defects


def _known_goal(request: str) -> str:
    """Use an explicit goal sentence only. Do not invent one."""

    match = re.search(r"(?:목표|목적)\s*[:：]\s*(.+)", request)
    return match.group(1).splitlines()[0].strip() if match else ""


def _repair_kind(defects: list[str]) -> str:
    """Separate a contract-link failure from a procedure that omits work."""

    if any(item.startswith("review_") for item in defects):
        return "fitness_review"
    if any(item.startswith(("requirement_unlinked", "requirement_unjudged")) for item in defects):
        return "contract_link"
    return "procedure_content"


def _empty_answer_status(selected: Procedure) -> str:
    answer_steps = [
        step.id for step in selected.steps if step.role == "answer" or "answer" in step.outputs
    ]
    where = f"최종 답변 단계({', '.join(answer_steps)})" if answer_steps else "최종 답변 단계"
    return (
        f"판단 단계는 진행했지만 {where}가 답변을 채우지 않아 완료로 처리하지 않았습니다. "
        "같은 요청을 다시 보내 주세요."
    )


def _user_status(defects: list[str]) -> str:
    if any(item.startswith("review_") for item in defects):
        return "요청의 일부를 처리하거나 최종 답을 쓰는 단계가 없는 절차라 실행하지 않았습니다."
    if any(item.startswith("parse_failed") for item in defects):
        return "절차를 구성하지 못해 요청을 진행하지 못했습니다."
    if any("purpose_conflict" in item for item in defects):
        return "확인된 목표와 절차 목적이 달라 임의로 확정하지 않았습니다."
    if any(item.startswith("purpose_") for item in defects):
        return "요청 목적과 맞는 절차를 확정하지 못했습니다."
    if any("requirement_unlinked" in item or "requirement_unjudged" in item for item in defects):
        return "정리된 필수 조건이 판정과 선정 단계에 연결되지 않아 실행하지 않았습니다."
    if any("requirement_missing" in item for item in defects):
        return "필수 조건을 빠뜨리지 않는 절차를 확정하지 못했습니다."
    if defects:
        return "절차의 단계 연결이 완전하지 않아 실행하지 않았습니다."
    return "판단을 완료하지 못했습니다."


async def _emit_progress(
    on_progress: Callable[..., Awaitable[None]] | None,
    content: str,
) -> None:
    if on_progress is None or not content.strip():
        return
    await on_progress(content, tool_hint=False)


def _record_execution(
    case: ReasoningCase,
    selected: Procedure | None,
    workspace: Path | None,
    defects: list[str],
    trace: list[dict[str, Any]] | None = None,
) -> None:
    """Record one execution. Registration for reuse remains a separate decision."""

    if workspace is None:
        return
    path = workspace / "memory" / "dynamic-reasoning.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "case_id": case.id,
        "request": case.request,
        "procedure_id": selected.id if selected else case.procedure_id,
        "purpose": selected.purpose if selected else "",
        "dynamic": selected.dynamic if selected else True,
        "procedure": _procedure_outline(selected),
        "reusable": False,
        "status": case.status,
        "answer": case.answer,
        "stop_reason": case.stop_reason,
        "conclusion": case.conclusion.model_dump() if case.conclusion else None,
        "step_states": case.step_states,
        "history": case.history,
        "defects": defects,
        "trace": trace or [],
        "registration": "not_evaluated",
        "reasoning_mode": next(
            (
                {k: v for k, v in item.items() if k != "event"}
                for item in case.history
                if item.get("event") == "reasoning_mode"
            ),
            None,
        ),
        "calls": case.call_records,
        "call_totals": _call_totals(case.call_records),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _procedure_outline(selected: Procedure | None) -> dict[str, Any] | None:
    """Keep the executed step graph so a run can be diagnosed later."""

    if selected is None:
        return None
    return {
        "id": selected.id,
        "revision": selected.revision,
        "steps": [
            {
                "id": step.id,
                "role": step.role,
                "question": step.question,
                "inputs": step.inputs,
                "outputs": step.outputs,
                "failure_returns_to": step.failure_returns_to,
            }
            for step in selected.steps
        ],
    }


async def run_method_evaluation(
    provider: LLMProvider,
    *,
    model: str,
    request: str,
) -> ReasoningCase:
    return await run_reasoning_procedure(
        provider,
        model=model,
        request=request,
        procedure_id="method-evaluation",
    )


_PROCEDURE_PROMPT = (
    "요청을 처리할 절차 JSON 객체 하나만 반환한다. "
    "id, title은 생략해도 된다. purpose는 목표 한 문장이다. "
    "steps는 판단에 필요한 최소 단계만 둔다. 보통 3~5개이며 7개를 넘기지 않는다. "
    "각 question은 20자 안팎의 짧은 판단 질문이다. "
    "단, 마지막 답변 단계 question에는 사용자가 답에 넣어 달라고 한 항목"
    "(선정 결과, 근거, 제외 이유 등)을 빠짐없이 적는다. "
    "각 단계는 id, question, inputs, outputs를 가진다. "
    "inputs에는 결과를 받아야 할 앞 단계 id나 request만 넣는다. 제품명이나 데이터 이름은 넣지 않는다. "
    "마지막 단계 outputs에만 answer를 포함하고, 그 단계 inputs에는 앞 단계 id를 넣는다. "
    "repair_defects가 있으면 지적된 항목을 처리하는 단계나 질문을 추가해 고친다. "
    "S1부터 S7까지 고정하지 않는다. "
    "검증 완료 상태나 재사용 승인 표시는 포함하지 않는다."
)


_REVIEW_PROMPT = (
    "절차를 만든 쪽과 별개인 검토자로서 JSON 객체 하나만 반환한다. "
    "request를 사용자가 요구한 항목(무엇을 골라야 하는지, 지켜야 할 조건, 근거 요구 등)으로 "
    "나누고, 각 항목을 실제로 처리하는 procedure.steps의 id를 붙인다. "
    "처리하는 단계가 없으면 step_id를 null로 둔다. "
    "형식: {\"request_items\": [{\"item\": \"짧은 항목\", \"step_id\": \"id 또는 null\"}], "
    "\"answer_step_id\": \"사용자에게 줄 최종 답을 쓰는 단계 id 또는 null\", "
    "\"defects\": [\"목적과 맞지 않는 점, 없으면 빈 배열\"]}. "
    "통과 여부는 쓰지 않는다. 항목은 짧게 쓴다."
)


def _parse_object(content: str) -> dict[str, Any]:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
