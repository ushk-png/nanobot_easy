from datetime import UTC, datetime

import pytest

from nanobot.evidence import EvidenceItem, EvidencePacketBuilder
from nanobot.evidence.types import Answerability


def test_evidence_packet_scores_trust_and_answerability() -> None:
    packet = EvidencePacketBuilder().build(
        question="What is the current OpenAI API model?",
        items=[
            EvidenceItem(
                id="W1",
                source_type="web",
                title="OpenAI docs",
                url_or_path="https://platform.openai.com/docs/models",
                content_snippet="Models documentation",
                timestamp=datetime.now(UTC).isoformat(),
                relevance=0.8,
            ),
            EvidenceItem(
                id="W2",
                source_type="web",
                title="Forum copy",
                url_or_path="https://reddit.com/r/test",
                content_snippet="Discussion",
                relevance=0.2,
            ),
        ],
        time_sensitive=True,
    )

    assert packet.answerability == Answerability.PARTIAL
    assert packet.items[0].trust_level == "official"
    assert packet.items[0].trust_score > packet.items[1].trust_score
    assert packet.freshness_warnings


def test_evidence_packet_marks_no_evidence() -> None:
    packet = EvidencePacketBuilder().build(question="unknown", items=[])

    assert packet.answerability == Answerability.NO_EVIDENCE
    assert packet.missing_info == ["No retrieval evidence was found."]


def test_evidence_packet_preserves_distinct_documents_from_same_origin() -> None:
    packet = EvidencePacketBuilder().build(
        question="OpenAI docs",
        items=[
            EvidenceItem(
                id="W1",
                source_type="web",
                title="A",
                url_or_path="https://openai.com/a",
                content_snippet="weak",
                relevance=0.1,
            ),
            EvidenceItem(
                id="W2",
                source_type="web",
                title="B",
                url_or_path="https://www.openai.com/b",
                content_snippet="strong",
                relevance=0.9,
            ),
        ],
    )

    assert len(packet.items) == 2
    assert packet.items[0].id == "W2"
    assert packet.items[0].independence_key == packet.items[1].independence_key


@pytest.mark.parametrize("urls", [
    ["https://example.edu/map"],
    ["https://first.example/map", "https://second.example/map"],
])
def test_source_reputation_or_count_cannot_verify_an_answer(urls) -> None:
    packet = EvidencePacketBuilder().build(
        question="Which second author earned an undergraduate degree in 1998?",
        items=[EvidenceItem(
            id=str(i), source_type="web", title="Campus map", url_or_path=url,
            content_snippet="Directions to campus", relevance=0.0,
        ) for i, url in enumerate(urls)],
        time_sensitive=False,
    )

    assert packet.answerability == Answerability.PARTIAL
    assert packet.missing_info


def test_even_relevant_snippets_require_condition_verification() -> None:
    packet = EvidencePacketBuilder().build(
        question="Find the paper whose second author studied at X.",
        items=[EvidenceItem(
            id="W1", source_type="web", title="Paper", url_or_path="https://example.edu/paper",
            content_snippet="Second author is affiliated with X", relevance=1.0,
        )],
        time_sensitive=False,
    )
    assert packet.answerability == Answerability.PARTIAL


def test_evidence_packet_dedupes_repeated_document_but_preserves_query_variants() -> None:
    packet = EvidencePacketBuilder().build(
        question="Find matching papers",
        items=[EvidenceItem(
            id=str(i), source_type="web", title=str(i), url_or_path=url,
            content_snippet="Paper", relevance=score,
        ) for i, (url, score) in enumerate([
            ("https://archive.example/paper?id=1", 0.1),
            ("https://archive.example/paper?id=1", 0.9),
            ("https://archive.example/paper?id=2", 0.8),
        ])],
    )
    assert [item.id for item in packet.items] == ["1", "2"]


def test_different_documents_sharing_provenance_are_not_deleted() -> None:
    packet = EvidencePacketBuilder().build(
        question="Compare the report and its correction",
        items=[EvidenceItem(
            id=str(i), source_type="web", title=str(i), url_or_path=url,
            content_snippet="Report", independence_key="same-publisher",
        ) for i, url in enumerate([
            "https://publisher.example/report", "https://mirror.example/correction",
        ])],
    )
    assert len(packet.items) == 2
    assert {item.independence_key for item in packet.items} == {"same-publisher"}


def test_freshness_warning_is_preserved_without_claiming_answerability() -> None:
    packet = EvidencePacketBuilder().build(
        question="What is the current policy?",
        items=[EvidenceItem(
            id="W1", source_type="web", title="Policy", url_or_path="https://example.gov/policy",
            content_snippet="Policy", timestamp="2000-01-01", relevance=1.0,
        )],
        time_sensitive=True,
    )
    assert packet.answerability == Answerability.STALE_RISK
