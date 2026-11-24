# 실제 모델 평가 하네스 (1단계)

`AgentLoop.process_direct` 경로를 격리된 임시 워크스페이스에서 실제 모델로 평가한다. 프로덕션 동작·운영 설정·운영 데이터는
바꾸지 않는다(설정은 메모리에서만 수정, 비밀값은 복사·출력하지 않음). 기준선 모델은 `testy` + `gpt-6-luna`.
규칙(반복 횟수·비용 한도·통과 기준)은 [PREREGISTRATION.md](PREREGISTRATION.md)에 결과를 보기 전에 고정했다.

실제 모델 평가와 모의 응답 회귀 검사는 분리한다.

| 종류 | 명령 | 모델 호출 |
| --- | --- | --- |
| 하네스·판정 자가 검증(모의) | `.venv/bin/python -m pytest tests/eval -q` | 없음(테스트에서 실제 호출 차단) |
| 기존 핵심 8개 회귀(모의) | `.venv/bin/python scripts/improvement_eval/run.py --suite core --out OUT.json` | 없음 |
| 개발용 기준선(실제 Luna) | `scripts/real_eval/run.sh run --split dev --out .local/eval/<이름>` | 있음(상한 150만 토큰) |
| 일부 사례만 | `scripts/real_eval/run.sh run --split dev --cases D03,D07 --reps 1 --out ...` | 있음 |
| 중단 후 이어서 | 같은 명령에 `--resume` | 있음 |
| 환경·버전 기록 | `scripts/real_eval/run.sh manifest --out m.json` | 없음 |
| 사례 정의 내보내기 | `.venv/bin/python -m scripts.real_eval export --out DIR` | 없음 |
| 두 결과 비교 | `.venv/bin/python -m scripts.real_eval compare 이전DIR 이후DIR` | 없음 |
| 답변만으로 판정되는 사례 재채점 | `.venv/bin/python -m scripts.real_eval rescore DIR` | 없음 |
| 최종 검증용(단계 종료 시 1회) | `scripts/real_eval/run.sh run --split final --confirm-final --out ...` | 있음 |

`run.sh`는 `.local/env`를 하위 셸에서만 불러온다(값을 출력하지 않는다).

## 결과 읽는 법

`PASS` / `FAIL`(나노봇 쪽) / `NOT_EXERCISED`(대상 경로가 실행되지 않아 검증 불가) / `HARNESS_ERROR`(하네스 쪽).
치명 위반 6종은 평균 성공률과 별도로 센다. 각 실행의 답변·산출물·도구 순서·토큰(목적별)·장애 주입 기록은 `runs.jsonl`에 있다.

## 최종 검증용 사례

`cases_final.py`는 `FINAL_SEAL.sha256`으로 봉인되어 있고, 개발 중에는 실행하지 않는다. 판정 코드가 동작하는지만
합성 답변으로 확인했다(`tests/eval`). 최종 실행 후 실패를 근거로 제품을 고쳤다면 그 사례는 폐기하고 새로 만든다.
