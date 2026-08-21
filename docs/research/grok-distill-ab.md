# Grok Build 방법론 증류 A/B 실행 결과

목적: 동일한 코딩 과제 2개에 대해 보강 전/후 응답 쌍을 기록한다. 차이 유무와 품질 판정은 사람이 한다.

주의:
- 실제 저장소 파일을 수정하지 않는 문서화용 실행 비교다.
- Before는 `code-modify`가 없고 기존 코딩 스킬만 있는 조건을 가정한다.
- After는 `code-modify` draft 및 보강된 이웃 스킬이 있는 조건을 가정한다.

## 과제 1: 작은 버그 수정

요청:

```text
로그인 버튼 클릭 후 API 실패 시 버튼 disabled 상태가 다시 풀리지 않는 버그를 고쳐줘.
```

### Before 응답

```text
로그인 버튼의 disabled 상태가 API 실패 경로에서 reset되지 않는 것으로 보입니다.

확인할 부분:
- 로그인 버튼 상태를 관리하는 컴포넌트
- API 호출 try/catch/finally 구조
- 실패 시 setLoading(false) 또는 setDisabled(false)가 호출되는지

수정 방향:
- API 호출이 성공/실패와 관계없이 종료될 때 disabled 상태를 해제하도록 finally 블록을 추가합니다.
- 관련 테스트가 있다면 실패 응답 케이스를 추가합니다.
```

### After 응답

```text
진행 절차:
1. 대상 저장소의 AGENTS.md/CONTRIBUTING.md와 로그인 컴포넌트 주변 코드를 먼저 확인합니다.
2. 로그인 버튼 상태를 변경하는 가장 작은 코드 경로를 찾습니다.
3. API 실패 경로에서만 빠진 상태 복구를 최소 diff로 수정합니다.
4. 기존 테스트 패턴이 있으면 실패 응답 케이스를 추가하거나 가장 가까운 테스트를 실행합니다.
5. 최종 보고에는 변경 파일, 실행한 검증 명령, 미검증 항목만 남깁니다.

예상 수정 형태:
- try/catch/finally 구조가 있다면 finally에서 loading/disabled 상태를 해제
- 중복 상태 변경이 있다면 기존 패턴에 맞춰 한 곳으로 정리
```

## 과제 2: 함수 리팩터링

요청:

```text
사용자 목록 필터링 함수가 너무 길어. 동작은 유지하면서 중복 조건을 줄이도록 리팩터링해줘.
```

### Before 응답

```text
리팩터링 방향은 다음과 같습니다.

- 필터 조건을 작은 predicate 함수로 분리합니다.
- 검색어, 상태, 권한 조건을 각각 별도 함수로 나눕니다.
- 기존 동작을 유지하기 위해 입력/출력 예시 기준으로 테스트합니다.

예시 구조:
- matchesKeyword(user, keyword)
- matchesStatus(user, status)
- matchesRole(user, role)
- filterUsers(users, filters)
```

### After 응답

```text
진행 절차:
1. 해당 함수가 있는 파일과 주변 호출부를 읽어 기존 데이터 구조와 테스트 패턴을 확인합니다.
2. 동작 변경 없이 중복 조건만 줄이는 범위로 제한합니다.
3. 새 추상화는 기존 파일 스타일에 맞춰 최소한으로 만듭니다.
4. 기존 테스트가 있으면 동일 케이스가 통과하는지 확인하고, 경계 케이스가 없으면 가까운 테스트에 추가합니다.
5. 포맷터나 새 라이브러리는 도입하지 않습니다.

예상 수정 형태:
- 조건식을 predicate 배열 또는 작은 helper 함수로 정리
- 공개 API와 반환 형식은 유지
- 관련 없는 네이밍/파일 이동은 하지 않음
```

## 검증 기록

- `nanobot skill audit`: Attention 0건
- `code-modify` routing: 10/10, 100.0%
- `code-review` routing: 12/12, 100.0%
- `code-debugging` routing: 11/12, 91.7%
- `debug-procedure` routing: 11/12, 91.7%

판정: 미기재. 사람이 판단한다.
