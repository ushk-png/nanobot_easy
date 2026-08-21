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
                timestamp="2026-07-01",
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

    assert packet.answerability == Answerability.ANSWERABLE
    assert packet.items[0].trust_level == "official"
    assert packet.items[0].trust_score > packet.items[1].trust_score
    assert packet.freshness_warnings


def test_evidence_packet_marks_no_evidence() -> None:
    packet = EvidencePacketBuilder().build(question="unknown", items=[])

    assert packet.answerability == Answerability.NO_EVIDENCE
    assert packet.missing_info == ["No retrieval evidence was found."]


def test_evidence_packet_dedupes_same_origin() -> None:
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

    assert len(packet.items) == 1
    assert packet.items[0].id == "W2"
