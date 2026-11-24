---
name: complex-research
description: "Investigate multi-clue questions requiring candidate discovery, cross-source evidence, and search strategy changes. Use to identify a paper, person, event, or policy from scattered conditions, or resolve conflicting evidence; ordinary single-fact lookups do not need this workflow."
metadata:
  nanobot:
    version: "1.0.0"
    category: research.identification
    risk_level: low
    requires_exec: false
    triggers:
      - "complex-research multi-hop research evidence verification strategic search"
      - "복잡한 검색 다중 조건 단서 교차 검증 근거 추적 심층 조사 전략적 검색"
      - "논문 저자 학력 인물 식별 역사 경기 기록 출처 충돌 검색 막힘"
    when_to_use: "여러 조건에 맞는 논문·인물·사건을 찾아야 하거나, 흩어진 출처를 교차 검증하거나, 검색이 막혀 전략 전환이 필요할 때. Identify entities from multiple clues or resolve conflicting source evidence."
    when_not_to_use: "단일 사실 조회, 단순 요약·번역, 외부 근거 조사가 필요 없는 일반 대화. Simple lookups, summarization, translation, or ordinary conversation."
---

# Complex Research — Iterative Evidence-Based Inquiry

Find a defensible answer by choosing useful searches, testing candidates, and preserving the user's exact conditions. Use existing search and page-reading tools; this skill adds no tools or execution permissions. Keep the process proportional to the question, with brief progress updates rather than a long planning preamble.

## Core Workflow

### 1. Preserve the question

Extract the target and its mandatory conditions: date range, identity/order, quantities, qualifications, and exclusions. Keep ambiguous wording visible; do not silently weaken it to fit a candidate. For example, undergraduate education is not a doctorate or current affiliation, and "not in the first half" can include extra time.

### 2. Choose an entry point

- Start with a discriminating clue that is likely to be searchable, not necessarily the first condition. Search one or two clues together rather than the whole question.
- Choose between candidate-first lookup (venue/year → papers → authors) and attribute-first lookup (distinctive biography → author → papers). Switch if one direction yields too many candidates or little evidence.
- Use compact keyword variants, original-language names, aliases, and relevant official domains. Search syntax varies by engine; simplify queries that repeatedly return nothing.

### 3. Test candidates and adapt

Read the underlying pages for decisive facts. Search snippets, model recollection, and answer compilations are leads, not sufficient proof of an exact match.

Keep a compact evidence note when several candidates or conditions are in play:

| Candidate | Condition | Observed fact | Status | Source / locator |
|-----------|-----------|---------------|--------|------------------|
| Name or ID | Exact requirement | What the page actually establishes | supported / contradicted / unknown | URL + section, page, or brief excerpt |

- Check the weakest or most discriminating remaining condition next. A clear contradiction excludes an exact-match candidate; resemblance does not outweigh it. Retain the rejection reason to avoid revisiting it.
- After several searches yield no new evidence or candidates, change the entry point, source collection, language, or search mechanism. Rewording the same query indefinitely is not progress.
- Distinguish an empty search result from a timeout, blocked page, or unavailable tool. Use an accessible alternative source within existing permissions; report access limits when unresolved.
- Record known coverage gaps before claiming a list is exhaustive. A partial conference index or missing years cannot establish that no matching paper exists.

### 4. Resolve evidence carefully

Prefer sources authoritative for the particular fact: proceedings for author order; explicit CV/biography for degrees; match records for events; official legal text for policy status. Assess specificity, date, and provenance when sources disagree. Two sites copying one record are not independent confirmation.

One clear authoritative source can establish a fact. Seek independent corroboration for ambiguous, disputed, indirect, or consequential claims instead of requiring two sources for everything. Keep unresolved discrepancies visible and explain whether they affect eligibility. Never turn missing evidence into a negative fact: say "not confirmed in the sources checked," not "not publicly available."

## Targeted tactics

- **Papers:** Check venue, year, track, and exact author order in the proceedings. Legacy archive IDs and Findings/main tracks may differ. Verify the specified degree level and person identity separately; do not infer education from employment or affiliation. Inspect a bounded candidate set before downloading an entire corpus.
- **Historical events / sports:** Verify participants, date, officials, event counts, and causal qualifiers separately. Count unique substitutions, not both incoming and outgoing rows. A substitution timestamp alone does not establish injury. Distinguish stoppage time, extra time, and source timestamp differences.
- **Policy:** Use the user's jurisdiction and reference date; do not invent a recent-only time window. Distinguish proposals, adoption, effective dates, amendments, and repeals. News can identify leads; the applicable official text supports the conclusion.

## Completion and reporting

Stop when the requested answer is supported across all mandatory conditions, or when remaining useful searches are blocked or the available budget is exhausted. Obey existing time, call, and cost limits; this skill does not increase them. There is no universal search-count target. If investigation must stop, preserve a short candidate/evidence checkpoint and the next useful lookup rather than repeating the search from scratch.

- **Verified answer:** Give the exact requested name/title first, followed by concise condition-to-source evidence. Resolve competing matches if uniqueness is required.
- **Unresolved or contradictory:** Label the best candidate as provisional or excluded, identify the missing/conflicting condition, and state the checked scope. Do not present a near-match as solved or claim that no answer exists from incomplete coverage.
- Link the pages actually consulted. Include a short search trail only when useful or requested. Report observed evidence and uncertainty, not a detailed internal reasoning transcript.

Selecting this skill or writing a plan is not completion: perform the applicable searches and page checks, or clearly report the blocker.
