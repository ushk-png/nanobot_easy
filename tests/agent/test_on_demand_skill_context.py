"""Skill bodies stay out of ordinary prompts but remain discoverable/readable."""
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.config.schema import Config
from nanobot.skill_store import SkillStore

from .t51_harness import Mock, calls, final

BODY = 'SPECIALIZED_PROCEDURE_BODY_DO_NOT_PRELOAD'


def write_skill(workspace):
    path = workspace / 'skills' / 'testy-voice' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: testy-voice\ndescription: Voice speech synthesis for Testy\n'
                    'always: true\nmetadata:\n  nanobot:\n    status: verified\n'
                    '---\n\n# Testy Voice\n\n' + BODY)
    return path


@pytest.mark.parametrize('preload', [True, False])
def test_automatic_body_is_optional_and_explicit_selection_still_works(tmp_path, preload):
    write_skill(tmp_path)
    ctx = ContextBuilder(tmp_path, preload_always_skills=preload)
    prompt = ctx.build_system_prompt()
    assert (BODY in prompt) is preload
    assert ('testy-voice' in ctx.persistent_skill_names()) is preload
    assert BODY in ctx.build_system_prompt(skill_names=['testy-voice'])
    assert BODY in ctx.build_selected_system_prompt(['testy-voice'])
    assert BODY in ctx.skills.load_skill('testy-voice')
    assert any(s['name'] == 'testy-voice' for s in ctx.skills.list_skills())


def test_other_agents_keep_existing_default(tmp_path):
    write_skill(tmp_path)
    assert Config().agents.defaults.preload_always_skills is True
    assert BODY in ContextBuilder(tmp_path).build_system_prompt()


async def test_config_reaches_actual_loop_then_search_and_read_load_the_skill(tmp_path):
    path = write_skill(tmp_path)
    config = Config.model_validate({'agents': {'defaults': {
        'workspace': str(tmp_path), 'skills': [], 'preloadAlwaysSkills': False,
        'contextSelection': {'enabled': False},
    }}})
    mock = Mock()
    loop = AgentLoop.from_config(config, provider=mock)
    assert loop.context.preload_always_skills is False
    assert loop.context.persistent_skill_names() == []
    mock.steps.append(final('안녕하세요. 프랭크.'))
    out = await loop._process_message(InboundMessage(channel='cli', sender_id='owner',
                                                    chat_id='ordinary', content='안녕'))
    assert out.content == '안녕하세요. 프랭크.'
    assert len(mock.requests) == 1 and BODY not in str(mock.requests[0]['messages'])
    # Real index and tools, with synthetic provider decisions; no external audio is generated.
    mock.steps.extend([
        calls(('find', 'skill_search', {'queries': [{'query': 'testy-voice speech synthesis', 'top_k': 1}]})),
        calls(('read', 'read_file', {'path': str(path)})),
        final('음성 생성 절차를 확인했습니다.'),
    ])
    out = await loop._process_message(InboundMessage(channel='cli', sender_id='owner',
                                                    chat_id='specialized', content='음성 생성 절차를 확인해 줘. 생성은 하지 마.'))
    assert out.content == '음성 생성 절차를 확인했습니다.'
    assert len(mock.requests) == 4
    assert BODY not in str(mock.requests[1]['messages'])
    assert any(m['role'] == 'tool' and 'testy-voice' in str(m['content'])
               for m in mock.requests[2]['messages'])
    assert any(m['role'] == 'tool' and BODY in str(m['content'])
               for m in mock.requests[3]['messages'])


def test_real_profile_drafts_have_discovery_cards_without_promotion(tmp_path):
    source = Path(__file__).parents[2] / 'docs/runtime-profiles/testy-lean'
    for name in ['testy-voice', 'testy-japanese-recall']:
        dest = tmp_path / 'skills' / name / 'SKILL.md'
        dest.parent.mkdir(parents=True)
        dest.write_text((source / name / 'SKILL.md').read_text())
    store = SkillStore(tmp_path)
    store.reindex()
    ctx = ContextBuilder(tmp_path, preload_always_skills=False,
                         on_demand_skills=['testy-voice', 'testy-japanese-recall'])
    assert ctx.persistent_skill_names() == []
    prompt = ctx.build_system_prompt()
    assert '## Session flow' not in prompt
    for name in ['testy-voice', 'testy-japanese-recall']:
        assert str(tmp_path / 'skills' / name / 'SKILL.md') in prompt
        assert any(row['name'] == name and row['status'] == 'draft' for row in store.list_skills())
