"""Stop a model stream when one sentence repeats without new content."""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[\.\u3002\uff01\uff1f!?])\s+")


class RepeatedSentenceGuard:
    """Track streamed text and stop once the same sentence keeps repeating.

    Short or unfinished text is left alone. The first copy of a repeated
    sentence is kept; later copies are dropped.
    """

    def __init__(self, *, min_sentence_chars: int = 12, max_repeats: int = 3) -> None:
        self.min_sentence_chars = min_sentence_chars
        self.max_repeats = max_repeats
        self.text = ""
        self.stopped = False

    def feed(self, delta: str) -> str:
        """Return the portion of *delta* that should still be shown."""
        if self.stopped or not delta:
            return ""
        self.text += delta
        kept = trim_repeated_sentences(
            self.text,
            min_sentence_chars=self.min_sentence_chars,
            max_repeats=self.max_repeats,
        )
        if kept == self.text:
            return delta
        self.stopped = True
        already = len(self.text) - len(delta)
        self.text = kept
        if len(kept) <= already:
            return ""
        return kept[already:]

    def finish(self, content: str | None) -> str | None:
        if content is None:
            return None
        return trim_repeated_sentences(
            content,
            min_sentence_chars=self.min_sentence_chars,
            max_repeats=self.max_repeats,
        )


def trim_repeated_sentences(
    text: str,
    *,
    min_sentence_chars: int = 12,
    max_repeats: int = 3,
) -> str:
    """Drop a trailing run after the same sentence repeats *max_repeats* times."""
    parts = _SENTENCE_END.split(text)
    if len(parts) < max_repeats + 1:
        return text
    counts: dict[str, int] = {}
    kept: list[str] = []
    for part in parts:
        key = " ".join(part.split())
        if len(key) >= min_sentence_chars:
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > max_repeats:
                break
        kept.append(part)
    if len(kept) == len(parts):
        return text
    # Preserve the original separators by rebuilding only the kept prefix.
    rebuilt = ""
    cursor = 0
    for part in kept:
        start = text.find(part, cursor)
        if start < 0:
            return text
        end = start + len(part)
        rebuilt += text[cursor:end]
        cursor = end
    return rebuilt.rstrip()
