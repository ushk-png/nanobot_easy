"""Skill registration governance must fail closed (review A18, C2-08)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.webui.skill_manage_api import _declared_risk, _draft_governance, _risk_at_least


def _draft(review: dict, risk: str = "low") -> SimpleNamespace:
    markdown = f"---\nname: x\nmetadata:\n  nanobot:\n    risk_level: {risk}\n---\n\nbody\n"
    return SimpleNamespace(review_json=review, markdown=markdown)


@pytest.mark.parametrize("label", ["critical", "severe", "높음", "unknown"])
def test_an_unknown_risk_label_is_never_read_as_low(label) -> None:
    assert _risk_at_least(label, "high") is True


def test_known_labels_keep_their_order() -> None:
    assert _risk_at_least("medium", "medium") and not _risk_at_least("low", "medium")
    assert not _risk_at_least("medium", "high") and _risk_at_least("high", "medium")
    assert _risk_at_least("", "low") and not _risk_at_least("", "medium")


def test_a_low_first_flag_cannot_cancel_a_high_overall_level() -> None:
    review = {"security_risk_level": "high", "red_flags": [{"kind": "security", "severity": "low"}]}
    governance = _draft_governance(_draft(review), None)
    assert governance["blocked"] is True


def test_the_most_severe_security_flag_counts_not_the_first() -> None:
    review = {"red_flags": [{"kind": "security", "severity": "low"}, {"kind": "security", "severity": "high"}]}
    assert _draft_governance(_draft(review), None)["blocked"] is True


def test_a_model_review_cannot_lower_the_risk_the_draft_declares() -> None:
    review = {"status": "ready", "security_risk_level": "low", "red_flags": []}
    governance = _draft_governance(_draft(review, risk="high"), None)
    assert governance["blocked"] is True


def test_an_unknown_reviewer_label_requires_attention() -> None:
    governance = _draft_governance(_draft({"security_risk_level": "critical"}), None)
    assert governance["blocked"] is True


def test_a_clean_low_risk_draft_is_not_blocked() -> None:
    governance = _draft_governance(_draft({"security_risk_level": "low", "red_flags": []}), None)
    assert governance["blocked"] is False
    assert _declared_risk(_draft({}, risk="medium").markdown) == "medium"
