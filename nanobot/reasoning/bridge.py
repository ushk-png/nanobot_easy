"""Connect method evaluation to the agent turn without replacing normal chat."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from nanobot.providers.base import LLMProvider, hosted_tools_disabled
from nanobot.reasoning.contract import parse_procedure_payload
from nanobot.reasoning.dynamic import (
    MAX_DYNAMIC_RECOVERY,
    prepare_dynamic_procedure,
    recovery_budget_exceeded,
)
from nanobot.reasoning.engine import ReasoningEngine, first_step_contract
from nanobot.reasoning.llm import BudgetExceededError, RunBudget
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


# Stage, compose and review calls. grok-4.5/4.6/4.7 cannot turn reasoning off and
# default to "high" when the field is omitted; "low" is the lowest documented value.
STAGE_REASONING_EFFORT = "low"

# Per-run budget for every model call of a stepwise run (routing, steps, review, retries).
DEFAULT_TIME_BUDGET_S = 120.0
DEFAULT_CALL_BUDGET = 8


class ProviderStageLLM:
    """One structured call per stage. The model cannot set verification state."""

    reasoning_effort = STAGE_REASONING_EFFORT

    def __init__(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.call_records: list[dict[str, Any]] = []
        self.budget: RunBudget | None = None

    def complete(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = _stage_prompt(stage)
        body = json.dumps(payload, ensure_ascii=False)
        remaining = self.budget.before_call(stage) if self.budget is not None else None
        started = time.monotonic()
        try:
            # The time budget also bounds the request in flight, not only the gap between calls.
            response = asyncio.run(
                self._complete(stage, payload, prompt=prompt, body=body, timeout=remaining)
            )
        except TimeoutError as exc:
            self.call_records.append(
                {
                    "call_kind": _call_kind(stage),
                    "step_id": None if stage in {"procedure", "review"} else stage,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "model": self.model,
                    "finish_reason": "budget_timeout",
                    "payload_bytes": len(prompt.encode("utf-8")) + len(body.encode("utf-8")),
                }
            )
            raise BudgetExceededError("time", f"request for {stage} cut at the time budget") from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        content = response.content or ""
        if stage == "procedure":
            parsed, defects = parse_procedure_payload(content)
            if parsed is None:
                parsed = {"parse_defects": [item.message() for item in defects], "raw": content}
        else:
            parsed = _parse_object(content)
        # Adapter metadata is owned by code; a model-written value must not steer the flow.
        parsed.pop("revision", None)
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
                **_sent_reasoning(response, self.provider, self.reasoning_effort),
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
        timeout: float | None = None,
    ):
        prompt = prompt if prompt is not None else _stage_prompt(stage)
        body = body if body is not None else json.dumps(payload, ensure_ascii=False)
        # Stage calls have no tools; hosted (server-side) tools are not attached either.
        with hosted_tools_disabled():
            request = self.provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": body},
                ],
                tools=None,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
            )
            if timeout is None:
                return await request
            return await asyncio.wait_for(request, timeout=max(timeout, 0.001))


def _stage_prompt(stage: str) -> str:
    if stage == "procedure":
        return _PROCEDURE_PROMPT
    if stage == "review":
        return _REVIEW_PROMPT
    return (
        f"단계 {stage}의 JSON 객체만 반환한다. "
        "question에 답하고 expected_outputs에 적힌 키를 모두 채운다. "
        "output_shape가 있으면 그 키와 형태를 그대로 따른다. "
        "defects에는 결론을 바꾸거나 규칙(필수 조건 상쇄, 미확인의 충족 승격, 근거 없는 판단, "
        "판단과 결론 불일치)을 어기는 결함만 적는다. 표현 다듬기나 허용된 미확인 표시는 결함이 아니다. "
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


def _sent_reasoning(response: Any, provider: Any, effort: str | None) -> dict[str, Any]:
    """The reasoning field as sent (provider-reported), else as derived from the effort."""

    options = getattr(response, "request_options", None)
    if isinstance(options, dict) and "reasoning" in options:
        return {"reasoning_request": options["reasoning"], "reasoning_request_source": "request"}
    return {
        "reasoning_request": _reasoning_request(provider, effort),
        "reasoning_request_source": "derived",
    }


def _reasoning_request(provider: Any, effort: str | None) -> Any:
    """Report the reasoning field actually placed in the request body."""

    inner = getattr(provider, "_primary", None)  # FallbackProvider wraps the real provider.
    describe = getattr(provider, "reasoning_request_options", None) or getattr(
        inner, "reasoning_request_options", None
    )
    if not callable(describe):
        return "unknown"
    return describe(effort)


class ReasoningSessions:
    """Last stepwise case per session, kept so a follow-up can resume or update it."""

    def __init__(self, ttl_s: float = 30 * 60, max_items: int = 256) -> None:
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._items: dict[str, tuple[float, ReasoningCase, Procedure]] = {}

    def get(self, key: str) -> tuple[ReasoningCase, Procedure] | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, case, procedure = item
        if time.monotonic() - stored_at > self.ttl_s:
            self._items.pop(key, None)
            return None
        return case, procedure

    def put(self, key: str, case: ReasoningCase, procedure: Procedure) -> None:
        self._items[key] = (time.monotonic(), case, procedure)
        while len(self._items) > self.max_items:
            self._items.pop(next(iter(self._items)))

    def drop(self, key: str) -> None:
        self._items.pop(key, None)


@dataclass
class _Proposal:
    procedure: Procedure | None
    defects: list[str]
    trace: list[dict[str, Any]]
    path: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    prefill: dict[str, dict[str, Any]] = field(default_factory=dict)


async def run_reasoning_procedure(
    provider: LLMProvider,
    *,
    model: str,
    request: str,
    procedure_id: str | None = None,
    workspace: Path | None = None,
    on_progress: Callable[..., Awaitable[None]] | None = None,
    conversation: list[dict[str, str]] | None = None,
    sessions: ReasoningSessions | None = None,
    session_key: str | None = None,
    z3_check: bool = False,
    time_budget_s: float | None = DEFAULT_TIME_BUDGET_S,
    call_budget: int | None = DEFAULT_CALL_BUDGET,
    layout: str = "auto",
    review_required: tuple[str, ...] | list[str] = (),
    procedures: tuple[str, ...] | list[str] | None = None,
    prefill: bool | None = None,
) -> ReasoningCase:
    """Run one request. ``procedure_id`` is an explicit selection; otherwise the
    first call chooses direct / reuse / new, or resume / update for the session's
    previous case (design 4.2, 7)."""

    llm = ProviderStageLLM(provider, model)
    # One budget for routing, steps, review and retries (design 9, 12).
    llm.budget = RunBudget(seconds=time_budget_s, max_calls=call_budget)
    conversation = list(conversation or [])
    mode = _reasoning_mode(provider, model)
    if mode["disable_supported"] is False:
        # Checked before any call (design 1); the user is told via reasoning_mode_notice.
        logger.warning(
            "stepwise reasoning: {} has no non-reasoning mode; reasoning_effort=none "
            "cannot be applied; stage calls run at effort {}",
            model,
            STAGE_REASONING_EFFORT,
        )
    prior = sessions.get(session_key) if sessions is not None and session_key else None
    granularity = procedure_granularity(provider, model, layout)
    # The routing call also does the first step's extraction in the two-call layout.
    prefill_enabled = granularity == "two_call" if prefill is None else prefill
    catalog_ids = tuple(procedures) if procedures is not None else approved_procedures()

    def load(procedure_name: str) -> Procedure:
        loaded = approved_procedure(procedure_name, granularity)
        if procedure_name in review_required:
            loaded.review_policy = "required"
        return loaded

    selected = load(procedure_id) if procedure_id else None
    proposal = _Proposal(selected, [], [], "explicit")
    if selected is None:
        await _emit_progress(on_progress, "절차를 구성하고 있습니다.")
        try:
            proposal = await _propose_dynamic_procedure(
                llm,
                request,
                _known_goal(request),
                conversation,
                prior,
                granularity,
                load=load,
                catalog_ids=catalog_ids,
                prefill_enabled=prefill_enabled,
            )
        except BudgetExceededError as exc:
            case = ReasoningCase(
                id=f"incomplete-{uuid4().hex[:12]}",
                request=request,
                procedure_id="unrouted",
                procedure_version="-",
                status="incomplete",
                stop_reason=f"budget:{exc.kind}",
                history=[{"event": "budget_exceeded", "stage": "procedure", "kind": exc.kind}],
            )
            _finish_calls(case, llm, None)
            _record_execution(case, None, workspace, [])
            return case
        if proposal.path == "direct":
            # Chat, simple lookups and tool work go to the normal agent loop.
            case = ReasoningCase(
                id=f"direct-{uuid4().hex[:12]}",
                request=request,
                procedure_id="direct",
                procedure_version="-",
                status="direct",
                path="direct",
            )
            _finish_calls(case, llm, None)
            _record_execution(case, None, workspace, [], proposal.trace)
            return case
        selected = proposal.procedure
        if selected is None and proposal.path not in {"resume", "update"}:
            case = ReasoningCase(
                id="dynamic-rejected",
                request=request,
                procedure_id="dynamic-rejected",
                procedure_version="dynamic",
                status="stopped",
                stop_reason=_user_status(proposal.defects),
                path=proposal.path,
            )
            _finish_calls(case, llm, None)
            _record_execution(case, selected, workspace, proposal.defects, proposal.trace)
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
        reasoning_effort=STAGE_REASONING_EFFORT,
        reasoning_disabled_guaranteed=False,
        on_stage=_on_stage,
        z3_check=z3_check,
    )
    if proposal.path in {"resume", "update"} and prior is not None:
        previous_case, selected = prior
        if proposal.path == "resume":
            case = await asyncio.to_thread(engine.resume, previous_case, selected, request)
        else:
            case = await asyncio.to_thread(
                engine.continue_with_facts, previous_case, selected, proposal.facts
            )
    else:
        assert selected is not None
        case = await asyncio.to_thread(
            engine.run,
            request,
            procedure=selected,
            conversation=conversation,
            prefill=proposal.prefill,
        )
    case.path = proposal.path
    if pending:
        await asyncio.gather(*(asyncio.wrap_future(item) for item in pending))
    if case.status == "completed" and not case.answer.strip():
        case.status = "failed_empty_answer"
    if case.status == "failed_empty_answer":
        case.stop_reason = _empty_answer_status(selected)
    if sessions is not None and session_key:
        if case.status in {"waiting_info", "completed"}:
            sessions.put(session_key, case, selected)
        else:
            sessions.drop(session_key)
    _finish_calls(case, llm, selected)
    _record_execution(case, selected, workspace, [], proposal.trace)
    return case


def _finish_calls(case: ReasoningCase, llm: Any, selected: Procedure | None) -> None:
    """Attach per-call measurements to the case and log one line per call."""

    records = [dict(item) for item in getattr(llm, "call_records", [])]
    answer_ids = {
        step.id for step in (selected.steps if selected else [])
        if step.role == "answer" or "answer" in step.outputs
    }
    for record in records:
        record["run_id"] = case.id
        if record.get("call_kind") == "step" and record.get("step_id") in answer_ids:
            record["call_kind"] = "answer"
        logger.info("reasoning call {}", json.dumps(record, ensure_ascii=False, sort_keys=True))
    case.call_records = records
    totals = _call_totals(records)
    logger.info("reasoning run {}", json.dumps({"run_id": case.id, **totals}, sort_keys=True))
    mode = _reasoning_mode(getattr(llm, "provider", None), getattr(llm, "model", None))
    case.history.append({"event": "reasoning_mode", **mode})


# Dynamic procedure step limits by granularity (design 10).
_MAX_STEPS = {"fine": 7, "compact": 4, "two_call": 4}
# Layout chosen by layout="auto" for models that reason internally.
AUTO_REASONING_LAYOUT = "two_call"


def procedure_granularity(provider: Any, model: str | None, layout: str = "auto") -> str:
    """Merge logical steps when the model reasons internally anyway (design 1, 10).

    A model that keeps reasoning on (or stage calls run above 'none') already works
    through intermediate steps inside each call, so separate calls per logical step
    mostly add latency. Fine steps stay for models whose reasoning is truly off.
    """

    if layout in {"fine", "compact", "two_call"}:
        return layout
    if STAGE_REASONING_EFFORT.lower() != "none":
        return AUTO_REASONING_LAYOUT
    if _reasoning_mode(provider, model)["disable_supported"] is False:
        return AUTO_REASONING_LAYOUT
    return "fine"


def reasoning_mode_notice(provider: Any, model: str | None) -> str | None:
    """User-facing notice when 'reasoning off' cannot be honoured (design 1)."""

    if _reasoning_mode(provider, model)["disable_supported"] is not False:
        return None
    return (
        f"안내: {model} 모델은 추론을 끌 수 없어, 설정의 '추론 끄기'가 적용되지 않습니다. "
        f"단계별 절차는 이 모델의 추론을 가장 낮은 강도({STAGE_REASONING_EFFORT})로 켠 채 "
        "실행되며 응답이 느릴 수 있습니다."
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


async def _propose_dynamic_procedure(
    llm: ProviderStageLLM,
    request: str,
    known_goal: str = "",
    conversation: list[dict[str, str]] | None = None,
    prior: tuple[ReasoningCase, Procedure] | None = None,
    granularity: str = "fine",
    *,
    load: Callable[[str], Procedure] | None = None,
    catalog_ids: tuple[str, ...] | None = None,
    prefill_enabled: bool = False,
) -> _Proposal:
    """First call: choose direct / reuse / new / resume / update. Code checks the choice."""

    defects: list[str] = []
    trace: list[dict[str, Any]] = []
    proposal: dict[str, Any] = {}
    procedure_id: str | None = None
    load = load or (lambda name: approved_procedure(name, granularity))
    catalog_ids = catalog_ids if catalog_ids is not None else approved_procedures()
    max_steps = _MAX_STEPS[granularity]
    base: dict[str, Any] = {
        "request": request,
        "approved_procedures": _approved_catalog(load, catalog_ids, prefill_enabled),
        "step_budget": {"granularity": granularity, "max_steps": max_steps},
    }
    if conversation:
        base["conversation"] = conversation
    if prior is not None:
        base["previous_case"] = _previous_summary(*prior)
    for attempt in range(MAX_DYNAMIC_RECOVERY + 1):
        payload = dict(base) if attempt == 0 else {
            **base,
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
        mode = str(proposal.pop("mode", "") or "").strip().lower()
        if mode == "direct":
            trace.append({"attempt": attempt, "mode": "direct"})
            return _Proposal(None, [], trace, "direct")
        if mode in {"resume", "update"}:
            follow_defects, facts = _follow_up_defects(mode, proposal, prior)
            trace.append({"attempt": attempt, "mode": mode, "defects": follow_defects})
            if not follow_defects:
                # Same task and unchanged procedure: no regeneration or review (design 10).
                return _Proposal(None, [], trace, mode, facts)
            defects = follow_defects
            if recovery_budget_exceeded(attempt):
                break
            continue
        if mode == "reuse":
            chosen = str(proposal.get("procedure_id") or "").strip()
            if chosen in catalog_ids:
                # Unchanged approved procedure: no regeneration or review (design 10).
                loaded = load(chosen)
                first = proposal.get("first_step_output")
                prefilled = (
                    {loaded.steps[0].id: first}
                    if prefill_enabled and isinstance(first, dict) and first
                    else {}
                )
                trace.append(
                    {"attempt": attempt, "mode": "reuse", "procedure_id": chosen, "prefilled": bool(prefilled)}
                )
                return _Proposal(loaded, [], trace, "reuse", prefill=prefilled)
            defects = [f"unknown_procedure at procedure_id: expected approved id; observed {chosen or 'missing'}"]
            trace.append({"attempt": attempt, "mode": "reuse", "defects": defects})
            if recovery_budget_exceeded(attempt):
                break
            continue
        selected, attempt_trace, contract_defects = prepare_dynamic_procedure(
            proposal,
            request=request,
            known_goal=known_goal,
            procedure_id=procedure_id,
            revision=attempt + 1,
            max_steps=max_steps,
        )
        normalized = attempt_trace.get("normalized")
        if isinstance(normalized, dict):
            proposal = normalized
            procedure_id = str(normalized.get("id") or procedure_id or "")
        public_trace = {key: value for key, value in attempt_trace.items() if key != "normalized"}
        trace.append({"attempt": attempt, **public_trace, "defects": [item.message() for item in contract_defects]})
        if selected is not None:
            # Fitness review is a separate call; the generator's output is not its evidence.
            review_defects, review_trace = await _review_procedure(
                llm, request, known_goal, selected, conversation
            )
            trace.append({"attempt": attempt, "review": review_trace, "defects": review_defects})
            if not review_defects:
                return _Proposal(selected, [], trace, "new")
            defects = review_defects
            if recovery_budget_exceeded(attempt):
                break
            continue
        defects = [item.message() for item in contract_defects]
        if recovery_budget_exceeded(attempt):
            break
    return _Proposal(None, defects, trace, "new")


def _previous_summary(case: ReasoningCase, procedure: Procedure) -> dict[str, Any]:
    return {
        "status": case.status,
        "procedure_id": procedure.id,
        "goal": case.goal,
        "questions": case.questions,
        "facts": [
            {"id": item.id, "proposition": item.proposition, "status": item.status}
            for item in case.facts[:20]
        ],
        "can_update_facts": _has_role(procedure, "facts"),
    }


def _has_role(procedure: Procedure, role: str) -> bool:
    return any(role in step.roles for step in procedure.steps)


def _follow_up_defects(
    mode: str,
    proposal: dict[str, Any],
    prior: tuple[ReasoningCase, Procedure] | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Code-side check of resume / update choices."""

    if prior is None:
        return [f"{mode}_invalid at mode: expected a previous case; observed none"], []
    case, procedure = prior
    if mode == "resume":
        if case.status != "waiting_info":
            return [f"resume_invalid at mode: expected waiting_info; observed {case.status}"], []
        return [], []
    if case.status != "completed" or not _has_role(procedure, "facts"):
        return ["update_invalid at mode: previous case has no facts step to update"], []
    known = {item.id for item in case.facts}
    facts: list[dict[str, Any]] = []
    for index, raw in enumerate(proposal.get("facts") or []):
        if not isinstance(raw, dict) or not str(raw.get("proposition") or "").strip():
            continue
        fact_id = str(raw.get("id") or "").strip()
        if not fact_id:
            fact_id = f"u{len(known) + index + 1}"
        status = str(raw.get("status") or "confirmed")
        facts.append(
            {
                "id": fact_id,
                "proposition": str(raw["proposition"]).strip(),
                "source": "user",
                "status": status if status in {"confirmed", "estimated", "unknown"} else "unknown",
            }
        )
    if not facts:
        return ["update_invalid at facts: expected one or more facts with a proposition"], []
    return [], facts


def recent_conversation(
    messages: list[dict[str, Any]] | None,
    *,
    max_turns: int = 6,
    max_chars: int = 500,
) -> list[dict[str, str]]:
    """Earlier user/assistant turns, without the current message, system or tool text."""

    turns: list[dict[str, str]] = []
    for message in messages or []:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(message.get("content"))
        if text:
            turns.append({"role": str(role), "content": text[:max_chars]})
    if turns and turns[-1]["role"] == "user":
        turns.pop()  # the current request is sent separately
    return turns[-max_turns:]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "input_text", None}
        ]
        return " ".join(part for part in parts if part).strip()
    return ""


def _approved_catalog(
    load: Callable[[str], Procedure],
    ids: tuple[str, ...],
    prefill_enabled: bool = False,
) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for item in ids:
        procedure = load(item)
        entry: dict[str, Any] = {
            "id": procedure.id,
            "title": procedure.title,
            "purpose": procedure.purpose,
            "steps": " → ".join(step.question for step in procedure.steps),
        }
        if prefill_enabled:
            entry["first_step"] = first_step_contract(procedure)
        catalog.append(entry)
    return catalog


async def _review_procedure(
    llm: Any,
    request: str,
    known_goal: str,
    selected: Procedure,
    conversation: list[dict[str, str]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Ask a separate call where each request item is handled; code decides pass/fail."""

    outline = _procedure_outline(selected) or {}
    payload = {
        "request": request,
        "known_goal": known_goal,
        "procedure": {"purpose": selected.purpose, "steps": outline.get("steps", [])},
    }
    if conversation:
        payload["conversation"] = conversation
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


def user_reply(case: ReasoningCase) -> str:
    """Text for the user. An answer that failed its last check is labelled, not passed off."""

    answer = case.answer.strip()
    if case.status == "completed" and answer:
        return answer
    if case.status == "incomplete":
        return _incomplete_reply(case)
    if case.status in {"waiting_info", "failed_empty_answer"} or case.procedure_id == "dynamic-rejected":
        return case.stop_reason.strip() or "판단을 완료하지 못했습니다."
    reason = case.stop_reason.strip()
    if answer:
        return f"{answer}\n\n※ 이 답변은 마지막 점검을 통과하지 못했습니다. (점검 내용: {reason})"
    return f"판단을 끝내지 못했습니다. (점검 내용: {reason})" if reason else "판단을 완료하지 못했습니다."


def _incomplete_reply(case: ReasoningCase) -> str:
    """Budget used up: say so, show what was confirmed, list what is unfinished."""

    kind = "시간" if case.stop_reason.endswith("time") else "호출 횟수"
    lines = [f"정해진 {kind} 예산 안에 판단을 마치지 못했습니다."]
    selection = next(
        (
            output["selection"]
            for output in case.step_outputs.values()
            if isinstance(output.get("selection"), dict)
        ),
        None,
    )
    if selection is not None:
        selected = selection.get("selected") or "없음"
        lines.append(f"코드로 계산된 선정 결과(설명 전): {selected}")
    elif case.judgments:
        results = {"met": "충족", "unmet": "불충족", "unknown": "미확인"}
        criteria = {item.id: item.statement for item in case.criteria}
        for judgment in case.judgments[:6]:
            label = criteria.get(judgment.criterion_id, judgment.criterion_id)
            lines.append(f"- {label}: {results.get(judgment.result, judgment.result)}")
    elif case.answer.strip():
        lines.append(f"점검을 마치지 못한 답변: {case.answer.strip()}")
    unfinished = [step for step, state in case.step_states.items() if state not in {"passed"}]
    if unfinished:
        lines.append(f"미완료 단계: {', '.join(unfinished)}")
    return "\n".join(lines)


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
        "granularity": selected.granularity if selected else None,
        "path": case.path,
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
    "요청을 처리할 방법을 정해 JSON 객체 하나만 반환한다. mode는 direct, reuse, new 중 하나다. "
    "direct: 인사·잡담, 단순 사실 확인·번역·요약처럼 단계별 판단이 필요 없는 요청, "
    "또는 웹 조회·파일·명령 실행 같은 도구가 필요한 요청. {\"mode\": \"direct\"}만 반환한다. "
    "후보와 조건이 값으로 주어지고 규칙대로 하나를 고르는 선정 문제는 "
    "constraint-selection을 reuse한다(선택은 코드가 계산). "
    "reuse: approved_procedures 중 목적·입력이 요청에 그대로 맞는 절차가 있으면 "
    "{\"mode\": \"reuse\", \"procedure_id\": \"id\"}를 반환한다. "
    "고른 절차에 first_step이 있으면 그 output_shape대로 첫 단계 결과를 first_step_output에 "
    "함께 낸다(대화에 정정된 값이 있으면 정정 값). "
    "목적·필수 조건·평가 기준이 바뀌는 메시지는 update가 아니라 reuse로 처음부터 다시 한다. "
    "new: 그 밖에 판단이 필요한 요청은 mode new와 함께 절차를 반환한다. "
    "previous_case가 있을 때: 이 메시지가 그 questions에 대한 답이면 {\"mode\": \"resume\"}. "
    "이전 결과에 대한 새 사실이나 정정이고 can_update_facts가 true이면 "
    "{\"mode\": \"update\", \"facts\": [{\"id\": \"고치는 사실의 id, 새 사실이면 생략\", "
    "\"proposition\": \"한 문장\", \"status\": \"confirmed|estimated|unknown\"}]}. "
    "새 주제이면 위 규칙대로 고른다. "
    "conversation은 이전 대화이며 요청 속 지시어를 해석하는 데만 쓴다. "
    "절차의 id, title은 생략해도 된다. purpose는 목표 한 문장이다. "
    "steps는 판단에 필요한 최소 단계만 두고 step_budget.max_steps를 넘기지 않는다. "
    "모델이 한 호출 안에서 추론하므로 compact이면 추출·판단·답변 정도로 묶는다. "
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
