"""Sparse public progress tied to the current task and actual tool operations."""
from __future__ import annotations

import re
import shlex
import time
from pathlib import Path

_INTERNAL = {"update_task", "skill_search", "skill_decision", "long_task", "complete_goal"}
_COMPLETION = re.compile(r"완료했|완료됐|완료되었|성공했|저장했|저장됐|검증됐|통과했|확인했|수정했|분석했|검토했|\b(?:completed|saved|succeeded|verified|passed)\b", re.I)
_CHECK = re.compile(r"(?:^|[\s/\\])(?:pytest|ruff|mypy|tsc|vitest|jest|unittest)(?:\s|$)|\b(?:npm|bun)\s+(?:run\s+)?(?:test|build|lint)\b")


def public_text(value: str, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    if not text or _COMPLETION.search(text):
        return ""
    return text if len(text) <= limit else text[:limit - 3] + "..."


class TaskProgress:
    def __init__(self, clock=time.monotonic, min_interval=25.0):
        self.clock = clock
        self.min_interval = min_interval
        self.last_at: float | None = None
        self.last_phase = ""
        self.last_message = ""
        self.plan_sent = False

    def reset(self):
        self.last_at = None
        self.last_phase = self.last_message = ""
        self.plan_sent = False

    def _emit(self, phase, message):
        if not message or message == self.last_message:
            return None
        now = self.clock()
        if self.last_at is not None:
            # A new file in the same read/search phase is not a new stage.
            interval = 60.0 if phase == self.last_phase else self.min_interval
            if now - self.last_at < interval:
                return None
        self.last_at, self.last_phase, self.last_message = now, phase, message
        return message

    def plan(self, update):
        if self.plan_sent or update is None or update.status != "active":
            return None
        steps = [public_text(step) for step in update.planned_steps[:2]]
        steps = list(dict.fromkeys(step for step in steps if step))
        if not steps:
            return None
        message = self._emit("plan", " ".join(steps))
        if message:
            self.plan_sent = True
        return message

    def operation(self, name, params):
        if name in _INTERNAL:
            return None
        path = params.get("path") or params.get("file") or ""
        label = public_text(Path(str(path)).name, 70) if path else ""
        label = f"'{label}'" if label and label != "." else "요청한 자료"
        if name in {"read_file", "web_fetch", "read_memory_events"}:
            phase = "read"
            pages = params.get("pages")
            offset, limit = params.get("offset", 1), params.get("limit")
            if name == "web_fetch":
                message = "찾은 웹 문서의 본문을 읽고 질문과 관련된 근거를 확인하겠습니다."
            elif pages:
                message = f"{label}의 {public_text(str(pages), 30)}쪽 내용을 확인하겠습니다."
            elif isinstance(offset, int) and isinstance(limit, int) and limit > 0:
                message = f"{label}의 {offset}~{offset + limit - 1}행을 읽고 필요한 내용을 확인하겠습니다."
            else:
                message = f"{label}의 내용을 읽고 요청한 검토 범위를 확인하겠습니다."
        elif name in {"find_files", "grep", "list_dir", "web_search", "search_memory"}:
            phase = "search"
            if name == "web_search":
                message = "웹에서 관련 자료를 찾고 답변에 사용할 출처를 살펴보겠습니다."
            elif name == "search_memory":
                message = "현재 대화에 없는 과거 결정과 제약을 기억 기록에서 찾아보겠습니다."
            elif name == "grep":
                message = f"{label}에서 관련 구현과 호출 지점을 찾아보겠습니다."
            else:
                message = f"{label}에서 검토할 파일의 위치와 구성을 확인하겠습니다."
        elif name in {"write_file", "edit_file", "apply_patch"}:
            phase = "edit"
            message = f"{label}에 요청한 변경을 적용하겠습니다."
        elif name == "exec":
            command = str(params.get("command") or params.get("cmd") or "")
            if _CHECK.search(command):
                phase = "verify"
                try:
                    names = [Path(word).name for word in shlex.split(command)
                             if word.endswith((".py", ".test.ts", ".test.tsx"))]
                except ValueError:
                    names = []
                subject = public_text(", ".join(names[:2]), 100)
                action = ("빌드" if re.search(r"\bbuild\b", command) else
                          "코드 검사" if re.search(r"\b(?:ruff|mypy|tsc|lint)\b", command) else "테스트")
                message = (f"'{subject}' 검증을 실행해 변경 후 동작을 확인하겠습니다." if subject
                           else f"{action}를 실행해 변경 후 동작을 확인하겠습니다.")
            else:
                phase = "execute"
                purpose = public_text(params.get("intent_summary") or "", 100)
                message = (f"다음 작업을 실행하겠습니다: {purpose}" if purpose
                           else "실행 도구로 현재 작업에 필요한 상태와 결과를 확인하겠습니다.")
        else:
            phase = name
            purpose = public_text(params.get("intent_summary") or "", 100)
            if not purpose:
                return None
            message = f"다음 작업을 진행하겠습니다: {purpose}"
        return self._emit(phase, message)
