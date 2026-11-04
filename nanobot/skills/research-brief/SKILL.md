---
name: research-brief
description: "Optional method for sourced briefs on recent developments and trends. 일반 최신 동향·짧은 개요는 스킬 로딩 없이 웹 도구로 직접 조사. Ordinary overviews do not require this skill."
metadata:
  nanobot:
    id: builtin-research-brief
    version: 1.0.2
    status: verified
    category: research.brief
    risk_level: low
    requires_exec: false
    required_tools:
      - web
    triggers:
      - find authoritative sources
      - find current sources
      - research this topic
      - find latest information
      - sourced brief
      - market overview
      - short overview of recent developments
      - latest trends overview
      - 최신 동향과 현황을 간단히 정리해줘
      - 최근 기술 동향 개요와 출처
    when_to_use: "Optional guidance when the user requests research-brief or a sourced brief needs a research method beyond ordinary web lookup. 조사 방법의 도움이 필요한 출처 기반 브리프 또는 명시적인 스킬 요청."
    when_not_to_use: "Do not load merely for an ordinary short overview of recent developments or trends; use web tools directly. Multi-clue identification or an answer-changing source conflict: complex-research. Provided-material synthesis: research-synthesis."
---

# Research Brief

Use this method when needed; ordinary overviews can proceed directly with web
tools. If already selected, reuse it without another skill search or rereading
instructions still in context.

## Method

1. Use the requested scope and length. For a broad but answerable overview, choose representative main developments; ask only when missing scope would materially change the answer.
2. Use primary or authoritative sources where possible.
3. Separate facts, source claims, and your inference.
4. Include concise citations or links in the final answer.

## Done Criteria

Stop when the requested main developments have support in consulted sources and
no unresolved source conflict changes the summary. Report relevant uncertainty;
do not expand a short overview into an exhaustive survey or unrequested searches
for named deployments, candidates, or every possible detail. A `PARTIAL` search
packet is not a requirement to keep searching once the originals support the brief.

Use `complex-research` only if a necessary dependent lookup or a specific conflict
affecting the answer emerges, or the user explicitly requests it. Carry forward
sources already read and investigate the unresolved part without restarting discovery.
