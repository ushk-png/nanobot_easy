"""t30 B1/B2: hot-path skills and persona reach the context-selection path generically."""

from nanobot.agent.context import ContextBuilder
from nanobot.config.schema import AgentDefaults


def _workspace(tmp_path):
    (tmp_path / "SOUL.md").write_text("PERSONA-SOUL: answer as the named secretary.")
    (tmp_path / "AGENTS.md").write_text("PERSONA-AGENTS: classify simple vs complex first.")
    (tmp_path / "USER.md").write_text("PERSONA-USER: the user is Frank.")
    for name, body in {"persona-skill": "HOT-PATH-METHOD-BODY", "other-skill": "OTHER-SKILL-BODY"}.items():
        d = tmp_path / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} test\n---\n\n{body}\n")
    return tmp_path


def test_selected_prompt_always_carries_hot_path_skills(tmp_path):
    builder = ContextBuilder(_workspace(tmp_path), hot_path_skills=["persona-skill"])
    unselected = builder.build_selected_system_prompt([], channel="cli")
    assert "HOT-PATH-METHOD-BODY" in unselected
    assert "OTHER-SKILL-BODY" not in unselected
    selected = builder.build_selected_system_prompt(["other-skill", "persona-skill"], channel="cli")
    assert selected.count("HOT-PATH-METHOD-BODY") == 1
    assert "OTHER-SKILL-BODY" in selected


def test_selected_prompt_without_hot_path_is_unchanged(tmp_path):
    builder = ContextBuilder(_workspace(tmp_path))
    assert "HOT-PATH-METHOD-BODY" not in builder.build_selected_system_prompt([], channel="cli")


def test_design_prompt_persona_is_opt_in_and_keeps_design_rules(tmp_path):
    builder = ContextBuilder(_workspace(tmp_path), hot_path_skills=["persona-skill"])
    plain = builder.build_design_system_prompt(channel="cli")
    for marker in ("PERSONA-SOUL", "PERSONA-AGENTS", "PERSONA-USER", "HOT-PATH-METHOD-BODY"):
        assert marker not in plain
    persona = builder.build_design_system_prompt(channel="cli", persona=True)
    for marker in ("PERSONA-SOUL", "PERSONA-AGENTS", "PERSONA-USER", "HOT-PATH-METHOD-BODY"):
        assert marker in persona
    assert "OTHER-SKILL-BODY" not in persona
    assert persona.startswith(plain)  # design identity and permission gates come first, unchanged
    assert "Explicit Permission Gates" in persona
    assert "do not change the design rules" in persona
    assert "General Tool Contract" not in persona


def test_design_persona_config_defaults_off_and_parses_camel_case():
    assert AgentDefaults().context_selection.design_persona is False
    cfg = AgentDefaults.model_validate({"contextSelection": {"enabled": True, "designPersona": True}})
    assert cfg.context_selection.design_persona is True
