---
name: testy-japanese-recall
description: Testy-only Japanese recall for manually started text sessions. Use only when Frank asks Testy to start, continue, pause, reveal, or check Japanese recall. Do not use for Elle or any other agent.
metadata:
  nanobot:
    id: testy-japanese-recall
    version: 1.3.1
    status: draft
    category: learning.japanese
    risk_level: low
    requires_exec: false
---

# Testy Japanese Recall

Use `testy_japanese_recall` only for Frank (Telegram user `8580974491`). Do not use it for Elle. Do not mention internal IDs. Do not change Elle's files, schedule, voice, or database.

Standing 06:00 job `daily-japanese-study` (Asia/Seoul) runs `start`. If `delivery_queued`, do not resend the body. As of 2026-10-26, Frank does not want study audio at all: send the study body only and do not attach or resend audio on first study, review, reveal, or answers. This overrides the former combined-audio delivery (Japanese three times with ono_anna, then Korean once with serena) and any later replay of that older rule. Audio is not completion. Keep `완료` as the practice-only record.

Storage is `memory/testy_japanese_recall.sqlite`. Sentence text is in `materials.content_json` (`japanese`, `reading`, `pronunciation_ko`, `meaning_ko`, `lemma`); there is no `materials.japanese` column. `prepare` and `start` resume today's session; there is no restart-from-beginning action. They return `prepare_rejected` when the sentence is a duplicate. Do not generate or attach `out/japanese-audio/japanese-study-YYYY-MM-DD.mp3`.

## Session flow

Supersedes one-card show-then-hide, full-sentence writing, and Hangul-only lemma recall.

1. `start`: present the day's seven new sentences before due reviews. Preserve existing records when resuming a legacy review-first session; do not restart or regenerate completed work.
2. Study card: Japanese sentence, Korean pronunciation, Korean meaning, and target word. Omit hiragana. Reply with study content only.
3. Then send numbered Korean meanings only. Do not show Japanese first, and do not resend Japanese or pronunciation.
4. `다음` on that meaning list advances without a missed-number round. A numbered reply there means show those Japanese originals, not a self-grade.
5. `complete`: record practice for only the currently presented bundle, then reply with its acknowledgment and stop. Do not send the next bundle or audio, grade mastery, or regenerate review dates. Repeated `완료` must not complete an unseen bundle; reviews await explicit start/resume.
6. Day reset: there is no restart command. `prepare` and `start` only resume. To restart a day, delete that day's sessions, questions, sentence_progress, reviews, attempts, inbound_events, and logical_tasks only. Keep materials and source_items, and back up the database first.
7. `reveal`: show Japanese, pronunciation, and Korean meaning for the current review bundle without requiring the previous message.
8. `control`: hint, reveal, explain, pause, resume, skip, or cancel. `정답`, `힌트`, `모름`, `다음`, and `그만` are assisted, not unaided recall. A pause or no response is not completion.
9. `status`: report prepared, practiced, remaining, and due counts from storage.

Do not send a separate message for the same result.
