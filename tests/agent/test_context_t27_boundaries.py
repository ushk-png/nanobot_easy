"""t27: boundary tests so case-specific t26 rules do not over-change common behaviour."""
from __future__ import annotations

import json

import pytest

from nanobot.agent.context_control import effective_scope, granted_targets, instruction_policy
from nanobot.agent.context_relations import parse_relations
from nanobot.agent.context_selection import EvidenceToolRegistry
from nanobot.agent.tools.base import Tool

from .test_context_t24_replay import _d1, replay


# ---- record-only (observability) tool exemption --------------------------------------------------
class _Writer(Tool):
    name, description, read_only = "write_file", "w", False
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, **kw):
        return "ok"


class _TruthyFlagWriter(_Writer):
    name = "fake_note"
    observability = "yes"            # truthy but not True: must NOT be exempt


def _gated(sel, tool, params):
    reg = EvidenceToolRegistry(sel)
    reg.register(tool)
    _, _, err = reg.prepare_call(tool.name, params)
    return err


def test_ordinary_write_is_still_gated_where_skill_decision_is_not():
    sel = replay("A_golf_s1-t2")
    assert _gated(sel, _Writer(), {"path": "/workspace/e2e/ws/plan.md"})


def test_only_an_explicit_true_flag_exempts():
    sel = replay("A_golf_s1-t2")
    assert _gated(sel, _TruthyFlagWriter(), {"path": "/workspace/e2e/ws/plan.md"})


def test_exempt_tools_are_only_the_known_record_only_ones():
    from nanobot.agent.tools.skill_decision import SkillDecisionTool
    from nanobot.agent.tools.filesystem import EditFileTool, WriteFileTool
    assert SkillDecisionTool(workspace="/tmp").observability is True
    for cls in (WriteFileTool, EditFileTool):
        assert getattr(cls, "observability", False) is not True


# ---- URL re-fetch block ---------------------------------------------------------------------------
class _Fetch(Tool):
    name, description, read_only = "web_fetch", "f", True
    parameters = {"type": "object", "properties": {"url": {"type": "string"},
                  "extractMode": {"type": "string"}, "maxChars": {"type": "integer"}}, "required": ["url"]}

    async def execute(self, **kw):
        return ""


def _fetch_err(sel, params):
    reg = EvidenceToolRegistry(sel)
    reg.register(_Fetch())
    return reg.prepare_call("web_fetch", params)[2]


def _d1_url():
    rec, sel = _d1()
    return rec["tool_calls"][2]["params"]["url"], sel


@pytest.mark.parametrize("variant,blocked", [
    (lambda u: u, True),                                   # identical
    (lambda u: u + "/", True),                             # trailing slash only
    (lambda u: u + "?page=2", False),                      # other query = other page
    (lambda u: u.rsplit("/", 1)[0] + "/other", False),     # other path
    (lambda u: "https://example.org/", False),             # other host
])
def test_refetch_identity_boundaries(variant, blocked):
    url, sel = _d1_url()
    assert bool(_fetch_err(sel, {"url": variant(url)})) is blocked


def test_other_extract_mode_or_bigger_cap_after_truncation_is_allowed():
    url, sel = _d1_url()
    assert _fetch_err(sel, {"url": url, "extractMode": "text"}) is None
    for o in sel.observations.values():
        if o.get("url"):
            o["_text"] = json.dumps({"truncated": True}) + "x" * 500
            o["fetch_mode"] = ["markdown", 5000]
    assert _fetch_err(sel, {"url": url, "maxChars": 20000}) is None
    assert _fetch_err(sel, {"url": url, "maxChars": 5000})          # same cap again: refused


def test_failed_earlier_fetch_does_not_block():
    url, sel = _d1_url()
    for o in sel.observations.values():
        if o.get("url"):
            o["status"] = "error"
    assert _fetch_err(sel, {"url": url}) is None


def test_other_tools_are_unaffected_by_the_fetch_rule():
    _, sel = _d1_url()
    assert "fetch_repeats" not in sel.trace.counters


# ---- later permission -----------------------------------------------------------------------------
def _scope(*turns):
    entries = []
    for i, text in enumerate(turns):
        pol = instruction_policy(text)
        g = granted_targets(text)
        entries.append((i, set(pol["denied_actions"]), set(g), g))
    return effective_scope(entries)


@pytest.mark.parametrize("turns,kind,allowed", [
    (["파일 수정하지 마", "이제 notes.md는 수정해도 돼"], "change", {"notes.md"}),
    (["파일 수정하지 마", "이제 수정해도 돼"], "change", None),                    # whole kind lifted
    (["이제 notes.md는 수정해도 돼", "파일 수정하지 마"], "change", set()),          # grant BEFORE denial: no lift
    (["파일 수정하지 마", "notes.md 수정해도 돼", "다시 수정하지 마"], "change", set()),  # later denial resets
    (["파일 수정하지 마", "notes.md 삭제해도 돼"], "change", set()),                 # other kind: no lift
])
def test_later_permission_scope(turns, kind, allowed):
    scope = _scope(*turns)
    if allowed is None:
        assert kind not in scope
    else:
        assert scope.get(kind) == allowed


def test_english_permission_is_not_misread_as_a_denial():
    assert instruction_policy("You may now edit notes.md")["denied_actions"] == []
    assert instruction_policy("I know you edit files")["denied_actions"] == []
    assert instruction_policy("Do not edit files")["denied_actions"] == ["change"]
    assert instruction_policy("Never send it")["denied_actions"] == ["send"]


# ---- phrase-edit target -------------------------------------------------------------------------
@pytest.mark.parametrize("req,target", [
    ("이 문장을 고쳐 줘: '파일을 삭제해'", "answer"),
    ("이 문장을 notes.md에 써 줘", "external_resource"),
    ("다음 문구를 report.txt에 추가해 줘: '안녕'", "external_resource"),
    ("문장을 고쳐서 README.md에 반영해 줘", "external_resource"),
    ("이 텍스트를 파일로 저장해 줘", "external_resource"),
    ("Write this sentence to notes.md", "external_resource"),
    ("이 문구를 슬랙에 보내 줘", "external_resource"),
])
def test_phrase_edit_target_boundaries(req, target):
    rels = [r for r in parse_relations(req) if r.action in {"edit", "write", "send"}]
    assert rels and rels[0].target == target, [r.public() for r in rels]
