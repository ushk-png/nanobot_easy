"""LLM-assisted skill draft composition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import json_repair
from loguru import logger

from nanobot.agent.skills import SYSTEM_SKILLS_DIR
from nanobot.providers.base import LLMProvider
from nanobot.skill_store import SkillDraftContent


def _read_system_skill(name: str) -> str:
    path = SYSTEM_SKILLS_DIR / name / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start : end + 1] if start != -1 and end != -1 and end > start else text
        parsed = json_repair.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("composer response must be a JSON object")
    return parsed


def _routing_cases(raw: Any, skill_name: str) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    cases: list[dict[str, str]] = []
    for item in raw[:10]:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        expected = str(item.get("expected") or skill_name).strip() or skill_name
        if query:
            cases.append({"query": query, "expected": expected})
    return cases


async def compose_skill_draft_with_llm(
    provider: LLMProvider,
    *,
    model: str,
    values: dict[str, Any],
    workspace: Path,
) -> SkillDraftContent:
    """Ask the configured model to produce draft method, review, and routing cases.

    The LLM never writes files or registry rows. The caller owns persistence and
    renders trusted frontmatter from the submitted form values.
    """

    name = str(values.get("name") or "").strip()
    description = str(values.get("description") or "").strip()
    trigger = str(values.get("trigger") or values.get("triggers") or "").strip()
    method = str(values.get("method") or "").strip()
    category = str(values.get("category") or "general").strip()
    risk_level = str(values.get("risk_level") or values.get("riskLevel") or "low").strip()
    requires_exec = bool(values.get("requires_exec") or values.get("requiresExec") or False)
    skill_docs = "\n\n".join(
        part
        for part in [
            _read_system_skill("skill-duplicate-check"),
            _read_system_skill("skill-trigger-differentiation"),
            _read_system_skill("skill-draft-generator"),
            _read_system_skill("skill-security-review"),
            _read_system_skill("skill-utility-review"),
            _read_system_skill("skill-test-generator"),
        ]
        if part
    )
    system = (
        "You are nanobot's Skill Composer. Return only JSON. Do not write files. "
        "The server will render YAML frontmatter, so do not include frontmatter. "
        "Create practical method instructions, a concise review object, and routing cases."
    )
    user = {
        "workspace": str(workspace),
        "system_skill_guidance": skill_docs,
        "draft_request": {
            "name": name,
            "description": description,
            "trigger": trigger,
            "method_seed": method,
            "category": category,
            "risk_level": risk_level,
            "requires_exec": requires_exec,
        },
        "required_json_schema": {
            "method": "markdown body for SKILL.md, without YAML frontmatter",
            "review": {
                "status": "ready",
                "summary": "short review summary",
                "security_risk_level": "low|medium|high",
                "duplicate": {
                    "score": "0.0-1.0 similarity to nearest existing skill",
                    "nearest": {
                        "name": "existing skill name or null",
                        "category": "existing skill category or null",
                        "reason": "why it does or does not overlap",
                    },
                    "classification": "new|update|duplicate",
                    "differentiation_required": "boolean",
                },
                "red_flags": [
                    {
                        "kind": "security|routing|duplicate|utility",
                        "severity": "low|medium|high",
                        "message": "brief actionable note",
                    }
                ],
            },
            "routing_cases": [
                {"query": "positive or neighboring user phrase", "expected": name}
            ],
        },
    }
    response = await provider.chat_with_retry(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        tools=None,
        model=model,
        max_tokens=4096,
        temperature=0.2,
    )
    if response.finish_reason == "error":
        raise RuntimeError(response.content or "composer model call failed")
    payload = _extract_json_object(response.content or "")
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    review.setdefault("status", "ready")
    review.setdefault("summary", "Composer review completed.")
    review.setdefault("security_risk_level", risk_level)
    review.setdefault("red_flags", [])
    logger.debug("LLM skill composer produced draft content for {}", name)
    return SkillDraftContent(
        method=str(payload.get("method") or method or "").strip(),
        review=review,
        routing_cases=_routing_cases(payload.get("routing_cases"), name),
    )
