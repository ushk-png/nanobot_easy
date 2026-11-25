"""Small safety fixes from the second review: LIKE wildcards (N20), Telegram token masking
(N18) and a floor for WebUI automation intervals (A15)."""

from __future__ import annotations

from pathlib import Path

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.memory.search import MemorySearcher, _escape_like
from nanobot.security.secrets import redact
from nanobot.webui.ws_http import _parse_automation_schedule


def test_escape_like_makes_wildcards_literal() -> None:
    assert _escape_like("100%_done\\x") == "100\\%\\_done\\\\x"


def test_percent_and_underscore_in_a_query_match_literally(tmp_path: Path) -> None:
    """Review N20: `%` and `_` in a term used to act as wildcards and match unrelated events."""
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id="main", agent_id="nanobot")
    for content in ("discount is 50% off today", "the value is fiftyXoff", "snake_case name here",
                    "snakeXcase unrelated"):
        store.append_event(scope=scope, conversation_id="c", session_id="s", actor="user",
                           event_type="USER_MESSAGE", content=content)
    with store.connect() as conn:
        searcher = MemorySearcher(store)
        where_hits = lambda q: {  # noqa: E731
            row["content"]
            for _score, row in searcher._like_hits(conn, scope, q, None, None, None, None, set(), 10)
        }
        assert where_hits("50%") == {"discount is 50% off today"}
        assert where_hits("snake_case") == {"snake_case name here"}


def test_telegram_bot_token_is_masked_inside_a_url_path() -> None:
    """Review N18: the pattern needed a word boundary, which `/bot<token>/` does not have."""
    token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyz0123456789"
    masked = redact(f"POST https://api.telegram.org/bot{token}/sendMessage failed")
    assert token not in masked and "ABCdefGhIJKlmNoPQRsTUVwxyz" not in masked
    assert token not in redact(f"token={token}")


def test_automation_interval_has_a_floor() -> None:
    assert isinstance(_parse_automation_schedule({"kind": "every", "every_ms": 1}), str)
    assert "at least 60 seconds" in _parse_automation_schedule({"kind": "every", "every_ms": 59_999})
    schedule = _parse_automation_schedule({"kind": "every", "every_ms": 60_000})
    assert schedule.kind == "every" and schedule.every_ms == 60_000
