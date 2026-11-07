"""t40 unit 3: calculation verification status pass / mismatch / unverified / not_applicable."""
import pytest

from .test_context_contrast import GOLF_EXPECTED, GOLF_T2, GOLF_T2_ANSWER, GOLF_T2_WRONG
from .test_context_selection import event, plan, select
from .test_context_selection import env as env  # noqa: F401

CHECK = "45분 배분"
SPEC = {"completion_checks": [CHECK], "completion_specs": [
    {"check": CHECK, "result_kind": "computed", "expected_values": GOLF_EXPECTED}]}


async def _sel(env, text, **design):
    event(env, text)
    _, _, sel = await select(env, plan(**design), request=text)   # MOCK design
    return sel


@pytest.mark.asyncio
async def test_correct_computed_answer_is_pass(env):
    sel = await _sel(env, GOLF_T2, answer=GOLF_T2_ANSWER, **SPEC)
    assert sel.calculation_status() == {"overall": "pass", "checks": {CHECK: "pass"}}


@pytest.mark.asyncio
async def test_wrong_computed_answer_is_mismatch(env):
    sel = await _sel(env, GOLF_T2, answer=GOLF_T2_WRONG, **SPEC)
    assert sel.calculation_status()["overall"] == "mismatch" and not sel.completion_supported()


@pytest.mark.asyncio
async def test_no_spec_is_unverified_never_pass_and_does_not_block(env):
    sel = await _sel(env, GOLF_T2, answer=GOLF_T2_WRONG, completion_checks=["배분 설명"])
    st = sel.calculation_status()
    assert st["overall"] == "unverified" and "pass" not in st["checks"].values()
    assert sel.completion_supported()           # general answers are not blocked; status says unverified


@pytest.mark.asyncio
async def test_empty_checks_answer_is_unverified(env):
    sel = await _sel(env, "TCP와 UDP 차이를 설명해 줘", answer="TCP는 연결 지향", completion_checks=[])
    assert sel.calculation_status()["overall"] == "unverified"


@pytest.mark.asyncio
async def test_file_change_only_is_not_applicable(env):
    sel = await _sel(env, "notes.md를 수정해 줘", route="execute", answer=None, completion_checks=["notes.md 변경"],
                     completion_specs=[{"check": "notes.md 변경", "result_kind": "mutation_result"}])
    assert sel.calculation_status()["overall"] == "not_applicable"
