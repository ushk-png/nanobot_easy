from nanobot.config.schema import Config


def test_webui_skill_management_config_aliases() -> None:
    config = Config(
        tools={
            "webuiSkillManagement": {
                "enabled": True,
                "draftExpireDays": 14,
                "redFlags": {
                    "minRoutingPasses": 8,
                    "securityRiskAtLeast": "medium",
                    "securityBlockAtLeast": "high",
                    "duplicateScoreAtLeast": 0.75,
                },
            }
        }
    )

    skill_management = config.tools.webui_skill_management

    assert skill_management.enabled is True
    assert skill_management.draft_expire_days == 14
    assert skill_management.red_flags.min_routing_passes == 8
    assert skill_management.red_flags.security_block_at_least == "high"

    dumped = config.model_dump(mode="json", by_alias=True)
    assert dumped["tools"]["webuiSkillManagement"]["draftExpireDays"] == 14
    assert dumped["tools"]["webuiSkillManagement"]["redFlags"]["securityBlockAtLeast"] == "high"
