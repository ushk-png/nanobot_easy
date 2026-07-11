# nanobot v0.2.2 — Subagent Profile 패치 가이드

수정 파일 4개. `context.py`는 건드리지 않는다 (v0.2.2에서 스킬 로딩은 `subagent.py`가 `SkillsLoader`를 직접 호출).

---

## 1. `nanobot/config/schema.py`

`AgentDefaults` 클래스 **위**에 추가:

```python
class SubagentProfile(Base):
    """A specialized subagent role definition."""

    description: str = ""                     # 역할 한 줄 설명
    when_to_use: list[str] = Field(default_factory=list)      # 위임 판단 근거 (능력 카드)
    when_not_to_use: list[str] = Field(default_factory=list)
    tools: list[str] | None = None            # allow-list. None이면 전체 허용
    skills: list[str] = Field(default_factory=list)           # 사전 로드할 스킬 이름
    model: str | None = None                  # 프로파일별 모델 오버라이드
    max_iterations: int | None = None
    temperature: float | None = None
    can_spawn: bool = False                   # 이 프로파일이 또 spawn 가능한지 (기본 차단)
```

`AgentDefaults` 클래스 안에 필드 추가:

```python
    subagent_profiles: dict[str, SubagentProfile] = Field(default_factory=dict)
    max_subagent_depth: int = Field(default=2, ge=1, le=3)
```

config.json 예시:

```json
{
  "agents": {
    "defaults": {
      "subagent_profiles": {
        "researcher": {
          "description": "웹 리서치·정보 수집·요약 전담. 코드 실행 불가.",
          "when_to_use": ["최신 정보 조사", "자료 수집", "웹 문서 요약"],
          "when_not_to_use": ["코드 작성", "파일 수정", "명령 실행"],
          "tools": ["web_search", "web_fetch", "read_file", "list_dir"],
          "skills": ["summarization"],
          "model": "anthropic/claude-haiku-4-5",
          "max_iterations": 10
        },
        "coder": {
          "description": "코드 작성·수정·실행·테스트 전담.",
          "when_to_use": ["구현", "버그 수정", "테스트 실행", "리팩토링"],
          "when_not_to_use": ["웹 리서치"],
          "tools": ["read_file", "write_file", "edit_file", "exec", "list_dir"],
          "max_iterations": 30,
          "can_spawn": true
        }
      },
      "max_subagent_depth": 2
    }
  }
}
```

---

## 2. `nanobot/agent/tools/spawn.py`

핵심 변경: (a) `profile`·`expected_output` 파라미터, (b) description에 프로파일 카드 동적 주입 — 이게 자연어 위임 판단의 근거가 된다.

```python
"""Spawn tool for creating background subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema
from nanobot.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        profile=StringSchema(
            "Subagent profile to use. MUST be one of the profiles listed in the "
            "tool description. Choose based on each profile's when_to_use / "
            "when_not_to_use criteria."
        ),
        expected_output=StringSchema(
            "What the subagent must return when done (format + content). "
            "Be specific — this is the acceptance criterion for the task."
        ),
        label=StringSchema("Optional short label for the task (for display)"),
        temperature=NumberSchema(
            description=(
                "Optional sampling temperature for the subagent "
                "(0.0 = deterministic, higher = more creative)."
            ),
            minimum=0.0,
            maximum=2.0,
        ),
        required=["task", "profile", "expected_output"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Tool to spawn a specialized subagent for background task execution."""

    def __init__(self, manager: "SubagentManager", depth: int = 0):
        self._manager = manager
        self._depth = depth
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_message_id", default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager, depth=getattr(ctx, "subagent_depth", 0))

    def set_context(self, ctx: RequestContext) -> None:
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        # 프로파일 능력 카드를 description에 주입 → LLM이 자연어로 위임 판단
        lines = [
            "Spawn a specialized subagent to handle a task in the background.",
            "Pick the profile whose when_to_use best matches the task. "
            "Never pick a profile whose when_not_to_use matches the task.",
            "",
            "Available profiles:",
        ]
        for name, p in self._manager.profiles.items():
            lines.append(f"- {name}: {p.description}")
            if p.when_to_use:
                lines.append(f"    when_to_use: {'; '.join(p.when_to_use)}")
            if p.when_not_to_use:
                lines.append(f"    when_NOT_to_use: {'; '.join(p.when_not_to_use)}")
        if not self._manager.profiles:
            lines.append("- general: general-purpose subagent (no profiles configured)")
        return "\n".join(lines)

    async def execute(
        self,
        task: str,
        profile: str,
        expected_output: str,
        label: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str:
        # 존재하지 않는 프로파일로의 위임을 스키마 다음 단계에서 한 번 더 차단
        if self._manager.profiles and profile not in self._manager.profiles:
            valid = ", ".join(self._manager.profiles)
            return (
                f"Error: unknown profile '{profile}'. "
                f"Choose one of: {valid}. Re-read the profile cards and retry."
            )
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{limit} running)."
            )
        return await self._manager.spawn(
            task=task,
            profile=profile,
            expected_output=expected_output,
            label=label,
            depth=self._depth + 1,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            origin_message_id=self._origin_message_id.get(),
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
        )
```

---

## 3. `nanobot/agent/subagent.py`

### 3-1. `__init__` 수정 — 프로파일 주입

```python
    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        profiles: dict[str, "SubagentProfile"] | None = None,   # 추가
        max_depth: int = 2,                                      # 추가
    ):
        ...기존 코드...
        self.profiles = profiles or {}
        self.max_depth = max_depth
```

(생성 지점 — `SubagentManager(...)`를 만드는 곳, 보통 `nanobot/cli` 또는 앱 부트스트랩 — 에서 `profiles=config.agents.defaults.subagent_profiles, max_depth=config.agents.defaults.max_subagent_depth` 전달. `grep -rn "SubagentManager(" nanobot/` 으로 위치 확인.)

### 3-2. `_build_tools` — allow-list 필터 + 하위 spawn 제어

```python
    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
        profile: "SubagentProfile | None" = None,   # 추가
        depth: int = 1,                              # 추가
    ) -> ToolRegistry:
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")

        # --- 추가: 프로파일 allow-list 필터 ---
        if profile is not None and profile.tools is not None:
            allowed = set(profile.tools)
            allowed.add("spawn")  # spawn 여부는 아래에서 별도 판정
            for name in [d["name"] for d in registry.get_definitions()]:
                if name not in allowed:
                    registry.unregister(name)

        # --- 추가: 재귀 spawn 제어 (depth 제한 + 프로파일 권한) ---
        allow_spawn = (
            depth < self.max_depth
            and profile is not None
            and profile.can_spawn
        )
        if not allow_spawn and registry.has("spawn"):
            registry.unregister("spawn")

        return registry
```

주의: `registry.get_definitions()`가 반환하는 스키마의 이름 키는 `_schema_name()` 구현에 따라 다를 수 있으니, 실제로는 `registry._tools.keys()`를 복사해 순회하거나 registry에 `names()` 헬퍼를 추가하는 게 안전하다:

```python
# registry.py 에 추가
    def names(self) -> list[str]:
        return list(self._tools.keys())
```

### 3-3. `spawn()` 시그니처 확장

```python
    async def spawn(
        self,
        task: str,
        profile: str = "",
        expected_output: str = "",
        label: str | None = None,
        depth: int = 1,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> str:
        if depth > self.max_depth:
            return (
                f"Cannot spawn: max subagent depth ({self.max_depth}) reached. "
                f"Complete this task directly instead of delegating."
            )
        ...기존 task_id / status 생성 코드...
        # _run_subagent 호출에 profile, expected_output, depth 전달
```

### 3-4. `_run_subagent()` — 프로파일 적용 핵심부

```python
    async def _run_subagent(
        self, task_id, task, label, origin, status,
        origin_message_id=None, temperature=None, workspace_scope=None,
        profile_name: str = "", expected_output: str = "", depth: int = 1,
    ) -> None:
        prof = self.profiles.get(profile_name)

        ...기존 root/cfg 결정 코드...

        tools = self._build_tools(
            workspace=root, tools_config=cfg, profile=prof, depth=depth,
        )
        system_prompt = self._build_subagent_prompt(
            workspace=root, profile_name=profile_name,
            profile=prof, expected_output=expected_output,
        )
        ...
        result = await self.runner.run(AgentRunSpec(
            initial_messages=messages,
            tools=tools,
            model=(prof.model if prof and prof.model else self.model),
            temperature=(
                temperature if temperature is not None
                else (prof.temperature if prof else None)
            ),
            max_iterations=(
                prof.max_iterations if prof and prof.max_iterations
                else self.max_iterations
            ),
            ...나머지 기존 인자 동일...
        ))
```

**프로파일별 모델 오버라이드 주의점**: `AgentRunSpec.model`만 바꾸면 같은 provider 내 모델 전환만 된다. 프로파일이 다른 provider의 모델(예: 메인은 anthropic, researcher는 openrouter)을 쓰려면 `runner.run` 전에 provider도 프로파일별로 만들어야 한다. 1차 구현에서는 **동일 provider 내 모델 전환만 지원**하고 시작하는 걸 권장.

### 3-5. `_build_subagent_prompt()` — 역할 + 스킬 사전 주입

```python
    def _build_subagent_prompt(
        self,
        workspace: Path | None = None,
        profile_name: str = "",
        profile: "SubagentProfile | None" = None,
        expected_output: str = "",
    ) -> str:
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        root = workspace or self.workspace
        loader = SkillsLoader(root, disabled_skills=self.disabled_skills)
        skills_summary = loader.build_skills_summary()

        # 프로파일 지정 스킬은 요약이 아니라 본문을 통째로 주입 (사전 로드)
        preloaded_skills = ""
        if profile and profile.skills:
            preloaded_skills = loader.load_skills_for_context(profile.skills)

        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
            profile_name=profile_name,
            profile_description=(profile.description if profile else ""),
            preloaded_skills=preloaded_skills,
            expected_output=expected_output,
        )
```

---

## 4. `nanobot/templates/agent/subagent_system.md`

```markdown
# Subagent

{{ time_ctx }}

You are a subagent spawned by the main agent to complete a specific task.
{% if profile_name %}
## Your Role: {{ profile_name }}
{{ profile_description }}
Stay strictly within this role. If the task requires capabilities outside
your role, report that back instead of attempting it.
{% endif %}
{% if expected_output %}
## Expected Output
Your final response MUST satisfy this acceptance criterion:
{{ expected_output }}
{% endif %}
Stay focused on the assigned task. Your final response will be reported back to the main agent.

{% include 'agent/_snippets/untrusted_content.md' %}

## Workspace
{{ workspace }}
{% if preloaded_skills %}

## Preloaded Skills

{{ preloaded_skills }}
{% endif %}
{% if skills_summary %}

## Skills

Read SKILL.md with read_file to use a skill.

{{ skills_summary }}
{% endif %}
```

---

## 5. (선택) 결과 검증 게이트

`_run_subagent()`에서 성공 분기(`else:`)에 넣는다. 가벼운 규칙 기반으로 시작:

```python
            else:
                final_result = result.final_content or "..."
                if expected_output and len(final_result.strip()) < 20:
                    # 명백히 빈약한 결과 → 실패로 격하해 메인 에이전트가 재위임 판단
                    await self._announce_result(
                        task_id, label, task,
                        f"Result did not satisfy expected output.\n"
                        f"Expected: {expected_output}\nGot: {final_result}",
                        origin, "error", origin_message_id,
                    )
                    return
                await self._announce_result(task_id, label, task, final_result, origin, "ok", origin_message_id)
```

LLM 기반 검증(작은 모델 1회 호출로 expected_output 충족 여부 판정)은 위 자리에 끼우면 되지만, 비용이 추가되므로 규칙 기반으로 먼저 운영해보고 필요할 때 추가.

---

## 구현 순서 체크리스트

1. [ ] `schema.py` — SubagentProfile + 필드 2개
2. [ ] `registry.py` — `names()` 헬퍼
3. [ ] `subagent.py` — __init__ → _build_tools → spawn → _run_subagent → _build_subagent_prompt 순서로
4. [ ] SubagentManager 생성 지점에서 profiles/max_depth 배선 (`grep -rn "SubagentManager(" nanobot/`)
5. [ ] `spawn.py` 교체
6. [ ] 템플릿 교체
7. [ ] 라우팅 테스트: 요청 예시 20개 × "어느 프로파일로 가야 하는가" 정답셋으로 위임 정확도 측정

## 업스트림 리베이스 주의

nanobot은 업데이트가 매우 잦다(v0.2.2가 6월 말). `subagent.py`와 `spawn.py`는 충돌 가능성이 높으니, 포크에서 수정분을 별도 커밋으로 깔끔하게 유지하고 주기적으로 rebase할 것.
