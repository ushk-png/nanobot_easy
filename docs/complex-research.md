# Complex Research

`complex-research` handles external questions where several conditions must hold,
one lookup depends on another, source records conflict, or an investigation stalls.
The user does not need to name the skill or use the word "complex". For example:

- "2019년 학회 논문 중 제2저자가 학부를 X대학에서 졸업한 논문을 찾아줘."
- "두 출처의 사건 날짜가 다른데 원문으로 확인해줘."
- "같은 검색어로는 후보가 안 나와. 다른 단서로 추적해줘."

Question length, a request for links, or the word "research" alone is insufficient.
Single-fact lookups and ordinary current-source overviews use web tools directly.
`research-brief` remains optional when explicitly requested or a specialized
research method is needed; supplied-material synthesis can use `research-synthesis`.

Apply that boundary **before loading** `complex-research`. A short overview of
recent developments or trends needs no research-skill discovery or replacement
skill load. Technical terms, recency, several topics, and citation requests do not
by themselves imply an investigation. Escalate only if a necessary dependent lookup or a specific
answer-changing conflict emerges, or the user explicitly requests the skill.
Carry forward consulted sources and investigate the unresolved part.
Reuse a selected skill and its known path; do not repeat discovery or reread a
body still in context unless the task or suitability of the method changes.

For a brief, stop when the requested main developments have source support and no
unresolved conflict changes the summary. Do not add unrequested candidate searches
or exhaustive checks. This boundary appears in the shared tool contract, both
skills' discovery metadata, and runtime workspace bootstrap guidance, because a
rule visible only after loading the skill cannot prevent unnecessary reads.

## Execution and completion

Before accepting a candidate, define what evidence would establish each mandatory
condition. Tie searches to missing conditions or candidate discovery, record
whether the evidence changed candidate eligibility, and switch strategy when it
does not. Before finalizing, check the weakest connection for a disqualifying
mismatch. Reuse consulted evidence rather than repeating every lookup.

Completion requires support for all mandatory conditions, with no unresolved
disqualifying contradiction. Otherwise give a clearly provisional result with the
missing condition, checked scope, and next useful lookup. Existing permissions
and research budgets remain in force.

The bundled source is `nanobot/skills/complex-research/SKILL.md`. Workspace copies
override it. Updating such copies requires reindexing the same workspace the agent
uses; preserve existing registry identity and lifecycle status. The common web
tool contract provides the routing cue in both executor prompt paths. Workspace
bootstrap guidance can additionally require reading the local skill before the
first search. These are model instructions, not a deterministic intent classifier.

## Retrieval evidence

When `tools.web.evidence.enabled` is enabled, evidence packets rank search
candidates and report freshness concerns. They do not verify the user's claims.
Nonempty packets remain `PARTIAL` (or `STALE_RISK`); source reputation, word overlap,
and source count never produce `ANSWERABLE` by themselves. `PARTIAL` is not a
failure and does not require extra searches once original-source checks establish
the answer. Empty results remain `NO_EVIDENCE`.

Only repeated document locators are deduplicated. Distinct pages, query strings,
and fragments remain available even when they share a domain or provenance key.
Origin grouping is a heuristic, not proof of independent corroboration.

## Verification

```bash
pytest tests/evidence/test_evidence_packet.py tests/tools/test_web_search_tool.py tests/agent/test_complex_research_skill.py -q
nanobot skill reindex --workspace /absolute/runtime/workspace
nanobot skill test-routing nanobot/skills/complex-research/routing_cases.json --workspace /absolute/runtime/workspace
```

Check the CLI's `workspace=...` line. Routing cases cover multi-clue questions,
overview-to-investigation transitions, Korean and English overview requests, and
neighboring tasks. They test retrieval ranking when discovery is requested, not
whether discovery should occur. A top-ranked `research-brief` result for an overview
does not require loading it. Prompt-delivery checks cover both executor paths,
with no research body injected by default and each explicitly selected body
included once. These checks do not prove an LLM's final skill choice or accuracy.

For a behavioral A/B evaluation, use the same fixed source collection, question
conditions, model, tools, and budget in fresh sessions. Include near-matches,
same-site records, copied sources, missing evidence, and conflicting dates.
Measure exact-answer accuracy, mandatory-condition coverage, unsupported final
claims, repeated searches, time, and tokens; retain failures and timeouts. Then
check live-web behavior separately to expose source availability differences.
The automated regression suite is not a claim of improved research performance.

The 1.1.0 synthetic pilot improved C3/C5 but unnecessarily read the skill on two
N2 overview runs. The 1.1.1 follow-up eliminated unnecessary complex-research
reads but increased mean input tokens by 70.3% in production-setting overview
runs: all 18 runs loaded research-brief, adding discovery/read round trips.
Version 1.1.2 removes automatic replacement-skill loading and repeated discovery;
source verification, completion criteria, document deduplication, and evidence
classification remain intact. Cost reduction for 1.1.2 is not yet measured.
A follow-up should compare 1.1.1 with 1.1.2 using the same questions and sources,
including C3/C5 and the unsuccessful O3 item. Evaluate `evidence=false` and
`evidence=true` separately, keeping the setting equal within each comparison.
Freeze question-specific rubrics before runs, independently check semantic grading,
and record skill reads separately from use of the investigation workflow. Set a
total model-call/token budget including execution, retries, and judging. No new
behavioral result is implied by passing routing or prompt-delivery checks.
