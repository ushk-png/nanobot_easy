"""Per-call measurements for stepwise reasoning runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from nanobot.providers.openai_responses.parsing import _usage_from_response_obj
from nanobot.reasoning.bridge import run_reasoning_procedure

GOAL = "일정 충돌 조정"
STAGE_OUTPUTS = {
    "S1": {"goal": GOAL, "constraints": [], "questions": []},
    "S2": {"facts": [{"id": "f1", "proposition": "두 일정이 겹침", "source": "request", "status": "confirmed"}]},
    "S3": {"criteria": [{"id": "c1", "statement": "충돌 해소", "kind": "required", "method": "time", "linked_goal": GOAL}]},
    "S4": {"judgments": [{"id": "j1", "criterion_id": "c1", "result": "met", "evidence_ids": ["f1"], "step_id": "S4"}]},
    "S5": {"conclusion": {"id": "k1", "status": "suitable", "statement": "시간 조정", "conditions": [], "judgment_ids": ["j1"]}},
    "S6": {"defects": []},
    "S7": {"answer": "겹치는 일정의 시간을 조정합니다."},
}


class FakeProvider:
    """Answers each stage from the payload and reports Responses-style usage."""

    def __init__(self, outputs: dict[str, dict] | None = None) -> None:
        self.outputs = outputs or STAGE_OUTPUTS
        self.requests: list[dict] = []

    @staticmethod
    def reasoning_request_options(effort: str | None):
        return None if effort == "none" else {"effort": effort}

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        payload = json.loads(kwargs["messages"][1]["content"])
        stage = payload.get("stage", "")
        return SimpleNamespace(
            content=json.dumps(self.outputs.get(stage, {}), ensure_ascii=False),
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "reasoning_tokens": 30,
            },
            finish_reason="stop",
        )


def _run(provider: FakeProvider, tmp_path: Path):
    return asyncio.run(
        run_reasoning_procedure(
            provider,
            model="xai-grok/grok-4.7",
            request="겹치는 일정 조정 방법 평가",
            procedure_id="method-evaluation",
            workspace=tmp_path,
        )
    )


def test_each_stage_call_records_duration_usage_and_payload(tmp_path: Path) -> None:
    case = _run(FakeProvider(), tmp_path)

    assert case.status == "completed"
    assert [item["step_id"] for item in case.call_records] == [f"S{i}" for i in range(1, 8)]
    for record in case.call_records:
        assert record["run_id"] == case.id
        assert isinstance(record["duration_ms"], int) and record["duration_ms"] >= 0
        assert record["model"] == "xai-grok/grok-4.7"
        assert record["reasoning_effort"] == "low"
        assert record["reasoning_request"] == {"effort": "low"}
        assert record["reasoning_request_source"] == "derived"
        assert record["input_tokens"] == 100
        assert record["output_tokens"] == 40
        assert record["reasoning_tokens"] == 30
        assert record["payload_bytes"] > 0
    kinds = [item["call_kind"] for item in case.call_records]
    assert kinds[:-1] == ["step"] * 6
    assert kinds[-1] == "answer"
    assert case.usage.reasoning_tokens == 30 * 7


def test_run_totals_are_written_to_the_existing_trace_file(tmp_path: Path) -> None:
    case = _run(FakeProvider(), tmp_path)

    lines = (tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()
    record = json.loads(lines[-1])

    assert record["case_id"] == case.id
    assert record["dynamic"] is False
    assert len(record["calls"]) == 7
    assert record["call_totals"]["calls"] == 7
    assert record["call_totals"]["input_tokens"] == 700
    assert record["call_totals"]["reasoning_tokens"] == 210
    assert record["call_totals"]["payload_bytes"] == sum(
        item["payload_bytes"] for item in record["calls"]
    )


def test_unreported_reasoning_tokens_stay_unknown(tmp_path: Path) -> None:
    class NoReasoningUsage(FakeProvider):
        async def chat_with_retry(self, **kwargs):
            response = await super().chat_with_retry(**kwargs)
            response.usage = {"prompt_tokens": 10, "completion_tokens": 5}
            return response

    case = _run(NoReasoningUsage(), tmp_path)

    assert all(item["reasoning_tokens"] is None for item in case.call_records)
    record = json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])
    assert record["call_totals"]["reasoning_tokens"] is None


def test_responses_usage_parses_reasoning_and_cached_tokens() -> None:
    usage = _usage_from_response_obj(
        {
            "usage": {
                "input_tokens": 2847,
                "input_tokens_details": {"cached_tokens": 1152},
                "output_tokens": 307,
                "output_tokens_details": {"reasoning_tokens": 302},
                "total_tokens": 3154,
                "num_server_side_tools_used": 0,
            }
        }
    )

    assert usage["prompt_tokens"] == 2847
    assert usage["completion_tokens"] == 307
    assert usage["reasoning_tokens"] == 302
    assert usage["cached_tokens"] == 1152
    assert usage["server_side_tools_used"] == 0
