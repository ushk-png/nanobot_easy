from nanobot.utils.prompt_templates import render_template


def test_consolidator_archive_preserves_topic_boundaries_and_identifiers() -> None:
    prompt = render_template("agent/consolidator_archive.md")

    assert "When multiple topics appear" in prompt
    assert "Preserve unresolved items" in prompt
    assert "file paths" in prompt
