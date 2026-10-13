"""Conservative, opt-in detection of execution promises, not an authorization gate.

Conversation focus is inferred metadata: it may ground a short proceed reply,
but never grants permission. Ambiguous language deliberately passes through.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from nanobot.session.goal_state import sustained_goal_waits_for_user
from nanobot.session.history_visibility import is_hidden_history_message

_ACTION = re.compile(
    r"\b(?:run|test|check|inspect|read|search|fetch|fix|implement|edit|create|execute|verify)\b"
    r"|(?:테스트|확인|실행|구현|수정|검증|진행|수행)\s*해\s*(?:줘|주세요)?"
    r"|찾아\s*줘|고쳐\s*줘|읽어\s*줘|단계.*해\s*줘", re.I,
)
_EXPLAIN = re.compile(
    r"\b(?:explain|how to|how do|plan only|planning only|propose|describe)\b"
    r"|설명해|방법.*알려|계획만|설계만|제안만", re.I,
)
_PROMISE = re.compile(
    r"\b(?:I(?:'ll| will| am going to)|we(?:'ll| will))\s+(?:now\s+)?"
    r"(?:run|test|check|inspect|read|search|fetch|fix|implement|edit|create|execute|verify|continue|start)\b"
    r"|(?:확인|검증|테스트|실행|수정|구현|진행|읽|찾|작업|착수)[^.!?\n]{0,35}"
    r"(?:하겠습니다|할게요|하겠어요|하겠|해보겠습니다|겠습니다)", re.I,
)
_OPTIONAL = re.compile(r"\b(?:if you|would you|could|can also|next stage|later stage)\b|원하시면|필요하면|다음\s*단계", re.I)
_BLOCKER = re.compile(
    r"\b(?:blocked|cannot|can't|unable|failed|error|need your approval|awaiting approval|waiting for approval|incomplete)\b"
    r"|승인.*(?:대기|필요|전까지)|권한.*필요|실패|오류|불가|할 수 없|미완료", re.I,
)
_RESTRICTION = re.compile(r"\b(?:do not execute|don't execute|no tools|plan only|wait for approval)\b|실행하지\s*마|승인\s*전.*금지", re.I)
_PROCEED = re.compile(r"^(?:approved|proceed|continue|go ahead|승인|진행해줘|계속 진행해줘)[.!\s]*$", re.I)

CORRECTION = (
    "The previous reply promises execution but does not establish completion. "
    "Preserve all existing user approvals, scope restrictions and manual/stage boundaries; "
    "this message grants NO permission and approves NO new stage. Perform actual task tools "
    "only for work already authorized, within the remaining budgets. Routing, skill_decision, "
    "goal recording and preparation are not task completion. If authorization, input, tools "
    "or budget are missing, report a clear blocker/incomplete result and what remains. "
    "Do not end with another promise or claim unverified completion."
)
INCOMPLETE = (
    "Incomplete: the assistant promised further work but did not establish that the requested "
    "deliverable was completed. Any recorded tool results are partial evidence only. "
    "No additional work or later stage is authorized by this response; existing approval "
    "and scope restrictions remain unchanged."
)


def is_premature_final(
    content: str | None,
    messages: list[dict[str, Any]],
    tools: Any,
    tool_events: list[dict[str, str]],
    session_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Require execution intent AND an unconditional remaining-work promise.

    Successful tools are evidence of attempts, never proof the whole task is done.
    Error outcomes plus a blocker are valid finals. Bookkeeping is not actual work.
    """
    if not content:
        return False
    users = [str(m.get("content", "")) for m in messages if m.get("role") == "user"
             and not is_hidden_history_message(m)
             and not str(m.get("content", "")).startswith((
                 "You have an active sustained goal:", "Output limit reached.",
                 "You have an active sustained goal."
             ))]
    if not users:
        return False
    request = users[-1]
    if _EXPLAIN.search(request) or _RESTRICTION.search(request):
        return False
    if _PROCEED.fullmatch(request.strip()):
        # Only actual conversation text grounds approval; focus alone cannot authorize.
        prior = next((u for u in reversed(users[:-1]) if _ACTION.search(u)), "")
        focus = (session_metadata or {}).get("conversation_focus", {})
        if not prior or (isinstance(focus, dict) and focus.get("missing_slots")):
            return False
        request = prior
    if not _ACTION.search(request):
        return False
    if sustained_goal_waits_for_user(session_metadata) and not re.search(
        r"\b(?:run|execute|implement|test)\s+stage\s+\d+|\d+단계\s*(?:실행|진행|해줘)",
        request, re.I,
    ):
        return False
    clauses = re.split(r"[.!?\n]+", content)
    promises = [c for c in clauses if _PROMISE.search(c) and not _OPTIONAL.search(c)
                and not _BLOCKER.search(c)]
    if not promises:
        return False
    if any(_BLOCKER.search(c) and not _OPTIONAL.search(c) for c in clauses):
        # A later-stage approval boundary is not a blocker for currently approved work.
        # Actual errors and approval waits for the present task still win.
        return False
    actual = [e for e in tool_events if e.get("status") == "ok"
              and e.get("name") not in {"skill_decision", "long_task"}
              and getattr(tools.get(e.get("name", "")), "observability", False) is not True]
    if not actual:
        return True
    # Partial success does not excuse an unconditional promise to do remaining work.
    return True
