# xAI Grok OAuth 프로바이더

X Premium / Grok 구독 계정으로 로그인해서 API 키 없이 Grok 모델을 쓰는 프로바이더다.
HKUDS nanobot 커밋 `c7393c78`(PR #5035)의 구현을 nanobot-easy(v0.2.2 기반)에 이식했다.

이 로그인은 xAI 개발자 API나 X Developer OAuth가 아니다. Grok Build가 쓰는 공개 OAuth
클라이언트 계약(Authorization Code + PKCE, `auth.x.ai`)과 구독용 프록시
(`cli-chat-proxy.grok.com`)를 따른다. xAI가 이 계약을 바꾸면 예고 없이 동작이 멈출 수 있다.

## 로그인

CLI:

```bash
nanobot provider login xai-grok --set-main      # 로그인하고 기본 모델로 지정
nanobot provider logout xai-grok
nanobot provider login xai-grok --config <설정 파일>   # 다른 인스턴스용
```

WebUI: 설정 > 모델에서 xAI Grok의 "로그인"을 누르거나, 온보딩 마법사의 기타 프로바이더에서
xAI Grok을 고른다. 새 탭에서 로그인하면 보통 자동으로 완료된다. nanobot을 다른 기기의
브라우저로 열었다면 콜백이 닿지 않으므로, 로그인 후 xAI가 보여 주는 인증 코드(또는 마지막
콜백 주소)를 대화상자에 붙여 넣는다.

- 기본 모델은 `xai-grok/grok-4.5`, 컨텍스트 창은 500,000 토큰이다.
- 모델 목록은 불러오지 않는다. 다른 모델은 설정에서 모델 ID를 직접 입력한다.
- 토큰은 인스턴스별 `auth/xai.json`(기본 `~/.nanobot/auth/xai.json`)에 권한 600으로 저장되고
  `config.json`에는 들어가지 않는다. 설정 파일이 다른 인스턴스는 각각 로그인해야 한다.
- 프록시가 필요하면 `config.json`의 `providers.xaiGrok.proxy`에 적는다.
- OAuth 프로바이더는 자동 폴백 대상이 아니다.

## X Search와 안전 모드

모델이 지원하면 xAI 서버가 실행하는 X 검색(`x_search`)이 자동으로 켜진다. 이 검색은
`ToolRegistry`를 거치지 않아 안전 모드의 도구 차단이 적용되지 않으므로,
`tools.safeMode`가 켜져 있으면 요청에 넣지 않는다(`providers/factory.py`의 `hosted_search`).

## 문제 해결

| 증상 | 조치 |
|---|---|
| 403 또는 구독 접근 거부 | 로그인한 계정에 X Premium / Grok 구독이 있는지 확인하고 다시 로그인 |
| 로그인 창에서 넘어가지 않음 | 대화상자에 인증 코드를 붙여 넣기. 대화상자의 "로그인 페이지 열기" 링크로 다시 열 수 있음 |
| "sign-in expired" | 로그인은 10분 안에 끝내야 한다. 다시 시작 |
| 잘 되다가 갑자기 실패 | xAI 쪽 계약 변경 가능성. 업스트림 nanobot의 수정 여부 확인 |

## 업스트림과 다른 점

- `ProviderSpec`에 `builtin_models`가 없어 레지스트리 항목에서 뺐다. 기본 모델은 설정 API가
  `oauth_default_model`로 내려준다.
- `XAIGrokProvider`에 `hosted_search` 플래그를 추가했다(위 안전 모드 항목).
- WebUI 로그인은 설정 화면과 온보딩 마법사가 공용 훅(`useProviderOAuthFlow`)과 대화상자
  (`ProviderOAuthLoginDialog`)를 쓴다.
- `provider login --config`가 로그인 전에 적용되도록 고쳤다. 이전에는 토큰이 항상 기본
  인스턴스에 저장됐다.

## 가져오지 않은 것

온라인 모델 목록 조회, Grok 4.6, 검색 중단 복구, X Search 진행 표시, WebUI에서 프록시 수정
(업스트림 `bc4de246`, `2389ab1f`, `7941450a`, `9cf2fb19`).
