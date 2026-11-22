"""Synthetic long-term memory for phase-5 core-memory tests (no real user data).

REQUIRED lists conditions declared *before* any summary exists; each entry holds the
acceptable exact phrasings. REVOKED phrases must never come back as current rules.
DETAILS exist only in the source and must be reached through search, not core memory.
"""

RULES = """# 사용자·역할·언어
- 답변은 항상 한국어로 한다. 코드 식별자는 원문 그대로 둔다.
- 사용자는 운영자이고 봇은 보조자다. 운영 설정 변경은 사용자 승인 후에만 한다.
- 역할 분리: 검토 봇은 코드를 수정하지 않고 의견만 낸다.

# 현재 금지·제한
- 다른 게이트웨이(포트 18793-18797)는 재시작하지 않는다.
- 외부 메시지는 초안만 작성하고 직접 보내지 않는다.
- 정정(2026-10-01): 기존 보고서는 덮어쓰지 않는다. 이전의 덮어쓰기 허용 지시는 철회됨.
- 실모델 호출 예산 상한 코드: BUDGET-CAP-4471 (이 값을 바꾸지 않는다).

# 과거 승인 기록 (현재 권한 아님)
- 2026-09-02: 일회성 배포 승인, 당일 만료됨.
"""

HISTORY = "".join(
    f"- 완료 작업 {i:03d}: 모듈 synthetic_{i:03d} 리팩터링, 테스트 {i * 3}건 통과, "
    f"조사 메모와 구현 세부 사항은 커밋 로그 참고 (TASK-{7000 + i}).\n"
    for i in range(1, 121)
)

DETAILS = """
# 프로젝트 Kestrel 상세
Kestrel 출시 창은 셋째 화요일이며 내부 추적 번호는 KESTREL-DETAIL-5521 이다.
Kestrel 데이터 파이프라인은 3단계 배치로 구성된다.
"""

SOURCE = RULES + "\n# 완료된 작업 기록\n" + HISTORY + DETAILS

CORE = """사용자·역할·언어
- 답변은 항상 한국어로 한다; 코드 식별자는 원문 유지.
- 사용자=운영자, 봇=보조자. 운영 설정 변경은 사용자 승인 후에만.
- 역할 분리: 검토 봇은 코드를 수정하지 않고 의견만 낸다.

현재 금지·제한
- 다른 게이트웨이(포트 18793-18797)는 재시작하지 않는다.
- 외부 메시지는 초안만 작성하고 직접 보내지 않는다.
- 정정(2026-10-01): 기존 보고서는 덮어쓰지 않는다(이전 허용은 철회).
- 실모델 호출 예산 상한 코드 BUDGET-CAP-4471, 변경 금지.

기록
- 2026-09-02 일회성 배포 승인은 만료된 과거 기록이며 현재 권한이 아니다.
- 완료 작업 기록과 프로젝트 상세(Kestrel 등)는 검색으로 확인한다."""

REQUIRED = [
    ["한국어로"],
    ["사용자 승인 후에만"],
    ["검토 봇은 코드를 수정하지 않고"],
    ["18793-18797"],
    ["초안만 작성"],
    ["보고서는 덮어쓰지 않는다"],
    ["BUDGET-CAP-4471"],
]
REVOKED = ["덮어써도 된다", "배포를 진행해도 된다", "배포 승인됨(현재)"]
DETAIL_FACT = "KESTREL-DETAIL-5521"

CORRECTION = "- 정정(2026-11-20): 주간 요약은 금요일에만 보낸다.\n"
