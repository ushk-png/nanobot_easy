---
name: complex-research
description: "복잡한 검색: 여러 조건을 모두 만족하는 대상 찾기, 흩어진 단서를 연결해서 후보 식별, 출처 충돌 검증, 막힌 검색의 전략 전환. Investigate multi-clue identification, dependent lookups, and conflicting sources. 일반 최신 동향·짧은 개요는 스킬 로딩 없이 웹 도구로 직접 조사. Skip this skill for an ordinary overview."
metadata:
  nanobot:
    version: "1.1.2"
    status: candidate
    category: research.identification
    risk_level: low
    requires_exec: false
    triggers:
      - "학회와 저자가 졸업한 대학의 조건으로 논문을 찾아줘"
      - "흩어진 단서를 연결해서 누구인지 찾아줘"
      - "출처마다 내용이 다른데 어느 쪽이 맞는지 확인해줘"
      - "주심이 특정 국가 출신인 경기를 기록과 교차 검증해줘"
      - "검색이 막혔으니 다른 단서로 추적해줘"
      - "Identify the paper or person matching all these clues"
      - "Resolve conflicting sources with multi-hop research"
    when_to_use: "복잡한 검색 질문: 여러 조건의 동시 충족, 논문·저자·학력·인물·경기·사건 식별, 단서 간 연결, 다단계 조회, 출처 모순, 검색 실패 후 전략 전환. Multi-condition identification, dependent lookups, cross-source verification, or stalled investigation. Find a paper by combining author identity, degree level, publication year and venue; establish an event from participants, dates and records. Choose by evidence needs, not the word complex."
    when_not_to_use: "일반 최신 동향·현황·짧은 개요는 스킬을 찾거나 읽지 않고 웹 도구로 직접 조사한다. 최신성·전문용어·여러 주제·링크 요청만으로 적용하지 않는다. 단일 사실 조회·요약·번역·일반 설명·제공 자료만의 종합에도 적용하지 않는다. Ordinary overviews use web tools directly, without loading a replacement skill. Escalate only for a necessary dependent lookup or a specific answer-changing source conflict; explicit skill requests still apply."
---

# Complex Research — Iterative Evidence-Based Inquiry

Find a defensible answer by choosing useful searches, testing candidates, and preserving the user's exact conditions. Use existing search and page-reading tools; this skill adds no tools or execution permissions. Keep the process proportional to the question, with brief progress updates rather than a long planning preamble.

## When To Use

- Check scope before loading this skill: a short overview of recent developments,
  trends, or the state of a topic uses web tools directly without skill discovery
  or loading a replacement skill.
  Recency, technical terminology, multiple topics, or citation requests alone do
  not require this workflow. If already loaded for such a request, return to the
  requested scope without adding candidate checks or repeating discovery.
- Identifying the answer must satisfy several eligibility conditions, or finding
  one entity is necessary to look up the next clue. This includes unnamed papers,
  people, events, products, and policies; the user need not say "complex search".
- Sources disagree about a decisive fact, or an investigation has stalled and
  needs a different entry point. Explicit requests to use this skill also apply.
- Do not add this workflow merely because a question is long or asks for links.
  Use ordinary lookup for one fact and `research-synthesis` to combine supplied
  materials without new investigation. Escalate an overview only when a necessary
  dependent lookup or a specific conflict affecting the answer actually arises;
  reuse consulted sources and investigate only the unresolved part.

## Method

### 1. Preserve the question

Extract the target and its mandatory conditions: date range, identity/order, quantities, qualifications, and exclusions. Keep ambiguous wording visible; do not silently weaken it to fit a candidate. For example, undergraduate education is not a doctorate or current affiliation, and "not in the first half" can include extra time.

For each decisive condition, identify what would count as evidence before accepting
a candidate: exact author order in the paper, an explicit degree in a biography,
or the applicable version of an official policy. Keep assumptions separate from
requirements. Resolve ambiguity from available context first; ask only if it
materially changes eligibility or the requested result.

### 2. Choose an entry point

- Start with a discriminating clue that is likely to be searchable, not necessarily the first condition. Search one or two clues together rather than the whole question.
- Choose between candidate-first lookup (venue/year → papers → authors) and attribute-first lookup (distinctive biography → author → papers). Switch if one direction yields too many candidates or little evidence.
- Use compact keyword variants, original-language names, aliases, and relevant official domains. Search syntax varies by engine; simplify queries that repeatedly return nothing.
- Tie each search to an unresolved condition, candidate discovery, or a specific
  source conflict. Prefer a lookup likely to eliminate candidates or resolve the
  weakest link. Independent lookups may be batched; use established findings to
  formulate dependent follow-ups rather than guessing all queries in advance.

### 3. Test candidates and adapt

Read the underlying pages for decisive facts. Search snippets, model recollection, and answer compilations are leads, not sufficient proof of an exact match.

Keep a compact evidence note when several candidates or conditions are in play:

| Candidate | Condition / evidence needed | Observed fact | Status | Source / locator |
|-----------|-----------------------------|---------------|--------|------------------|
| Name or ID | Exact requirement and what establishes it | What the page actually establishes | supported / contradicted / unknown | URL + section, page, or brief excerpt |

- Check the weakest or most discriminating remaining condition next. A clear contradiction excludes an exact-match candidate; resemblance does not outweigh it. Retain the rejection reason to avoid revisiting it.
- After several searches yield no new evidence or candidates, change the entry point, source collection, language, or search mechanism. Rewording the same query indefinitely is not progress.
- Count progress as a candidate added/excluded, a condition resolved, a conflict
  clarified, or an access/coverage gap established. More URLs or paraphrases of
  the same source do not by themselves count. Retain rejected candidates and
  unproductive lookups so a continuation does not repeat them without new evidence.
- Distinguish an empty search result from a timeout, blocked page, or unavailable tool. Use an accessible alternative source within existing permissions; report access limits when unresolved.
- Record known coverage gaps before claiming a list is exhaustive. A partial conference index or missing years cannot establish that no matching paper exists.

### 4. Resolve evidence carefully

Prefer sources authoritative for the particular fact: proceedings for author order; explicit CV/biography for degrees; match records for events; official legal text for policy status. Assess specificity, date, and provenance when sources disagree. Two sites copying one record are not independent confirmation.

One clear authoritative source can establish a fact. Seek independent corroboration for ambiguous, disputed, indirect, or consequential claims instead of requiring two sources for everything. Keep unresolved discrepancies visible and explain whether they affect eligibility. Never turn missing evidence into a negative fact: say "not confirmed in the sources checked," not "not publicly available."

Before finalizing a leading candidate, check the weakest connection for a
disqualifying mismatch: different person, degree level, author position, time,
jurisdiction, or document version. Reuse decisive evidence already read; do not
require a redundant second search for every condition. Source scores and tool
labels such as ANSWERABLE cannot substitute for this condition-by-condition check.

## Targeted tactics

- **Papers:** Check venue, year, track, and exact author order in the proceedings. Legacy archive IDs and Findings/main tracks may differ. Verify the specified degree level and person identity separately; do not infer education from employment or affiliation. Inspect a bounded candidate set before downloading an entire corpus.
- **Historical events / sports:** Verify participants, date, officials, event counts, and causal qualifiers separately. Count unique substitutions, not both incoming and outgoing rows. A substitution timestamp alone does not establish injury. Distinguish stoppage time, extra time, and source timestamp differences.
- **Policy:** Use the user's jurisdiction and reference date; do not invent a recent-only time window. Distinguish proposals, adoption, effective dates, amendments, and repeals. News can identify leads; the applicable official text supports the conclusion.

## Done Criteria

- An exact match needs direct support for every mandatory condition and no
  unresolved disqualifying contradiction. If uniqueness is requested, address
  remaining competing matches within the checked scope.
- A partial result identifies the unconfirmed condition, checked scope, blocker,
  and next useful lookup. An inaccessible page is not evidence against a candidate.
- Cite consulted originals for decisive claims; distinguish facts from inference.

## Completion and reporting

Stop when the requested answer is supported across all mandatory conditions, or when remaining useful searches are blocked or the available budget is exhausted. Obey existing time, call, and cost limits; this skill does not increase them. There is no universal search-count target. If investigation must stop, preserve a short candidate/evidence checkpoint and the next useful lookup rather than repeating the search from scratch.

- **Verified answer:** Give the exact requested name/title first, followed by concise condition-to-source evidence. Resolve competing matches if uniqueness is required.
- **Unresolved or contradictory:** Label the best candidate as provisional or excluded, identify the missing/conflicting condition, and state the checked scope. Do not present a near-match as solved or claim that no answer exists from incomplete coverage.
- Link the pages actually consulted. Include a short search trail only when useful or requested. Report observed evidence and uncertainty, not a detailed internal reasoning transcript.

Selecting this skill or writing a plan is not completion: perform the applicable searches and page checks, or clearly report the blocker.
