"""t49 test definitions — NOT EXECUTED in t49 (implementation only, unverified).

Preserves the context-reference cases t47 rewrote to explicit paths ("파일 수정해줘",
"현재 파일을 수정해줘") and defines the t49 behaviours. Run them only in a later verification step.
"""
from types import SimpleNamespace

import pytest

from nanobot.agent.context_relations import bind_targets, parse_relations


def _target(text, artifacts):
    found = parse_relations(text)
    bind_targets(found, [], artifacts, text)
    return next((r for r in found if r.target == "external_resource"), None)


# Original t47-before fixtures: a reference without a path resolves only to exactly one object in context.
@pytest.mark.parametrize("text, artifacts, bound", [
    ("파일 수정해줘", [{"path": "notes.md", "kind": "created"}], "notes.md"),
    ("파일 수정해줘", [{"path": "notes.md", "kind": "created"}, {"path": "plan.md", "kind": "created"}], None),
    ("파일 수정해줘", [], None),
    ("현재 파일을 수정해줘", [{"path": "app.yaml", "kind": "read"}], "app.yaml"),
    ("그 파일 고쳐 줘", [{"path": "a.md", "kind": "created"}], "a.md"),
    ("아까 만든 문서를 수정해", [{"path": "notes.md", "kind": "created"}], "notes.md"),
])
def test_context_reference_binds_unique_or_asks(text, artifacts, bound):
    rel = _target(text, artifacts)
    assert rel is not None
    assert rel.bound_id == bound and rel.resolved is (bound is not None)
    if bound is None and len(artifacts) > 1:
        assert rel.candidates   # clarification lists the choices


def test_new_output_without_name_is_delegated_naming_not_a_word_list():
    rel = _target("결과를 새 파일에 저장해 줘", [])
    if rel is not None and rel.action == "write":
        assert rel.new_artifact and rel.bound_id is None


def test_intended_target_is_linked_before_write_and_other_path_is_not_evidence():
    """link_intended_target fixes the path at the first gated write; a successful write to a
    different path afterwards neither replaces it nor satisfies the file_change requirement."""
    pytest.skip("definition only: needs a SelectedContext fixture with a delegated new output")


def test_blocked_write_to_request_named_path_is_not_completed():
    """t50 revision (the t49 "path written in the request" rule was removed): '공식 문서의 항목 A와 항목 B
    값을 t47_c.md 파일에 적어 줘.' The path alone creates no obligation. The executor's final-answer
    omission check adds the write with reason=supplement_requirement (span = the user's words); only then
    a write refused before execution is listed as required_change_not_performed:<.../t47_c.md>(blocked:...)
    and the status is not 'completed'. Without that linked requirement the blocked write is optional.
    See test_context_t50_definitions for the executable form."""
    pytest.skip("definition only: superseded by test_context_t50_definitions (also not executed)")


def test_supplement_requirement_adds_but_never_weakens():
    """reason=supplement_requirement: a span not in the request, a quoted span, or an entry that would
    drop a required/forbidden relation is rejected; an accepted write supplement makes
    required_results contain file_change; mutation_gate still decides the write."""
    pytest.skip("definition only")


def test_plan_update_first_and_dependent_actions_skipped_on_rejection():
    """t50 revision: the update is still processed first, but a rejected update returns only its own
    error; the other actions of the response run under their own pre-execution checks (no blanket skip).
    See test_context_t50_definitions.test_rejected_update_does_not_block_independent_action."""
    pytest.skip("definition only: superseded by test_context_t50_definitions (also not executed)")


def test_retry_allowed_only_after_code_visible_change():
    """no_progress identity: blocked when nothing changed; allowed after target version change, new
    successful material, instruction change, or a resolved transport error; an unknown change outcome
    (timeout) requires a read of the target before the same change runs again."""
    pytest.skip("definition only")


def test_cause_hypothesis_unconfirmed_never_blocks():
    pytest.skip("definition only")


def test_answer_fix_kinds_and_budget():
    """calc/answer error -> one fix call; verifier error -> actual result + verifier error reported;
    budget exhausted -> no extra call. t50 revision: a target/permission condition holds changes only and
    no longer stops the answer fix; an open item linked to 'answer' (affects) -> question, no fix call."""
    pytest.skip("definition only")


def test_state_view_keeps_all_constraints_and_reference_ids():
    pytest.skip("definition only")


_ = SimpleNamespace
