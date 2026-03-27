# 개발 문서 (docs/dev/)

각 팀은 작업 완료 시 이 디렉토리에 개발 문서를 작성한다.

## 문서 목록

| 파일 | 담당 팀 | 내용 |
|------|---------|------|
| `core-infrastructure.md` | Team 1 | Config, DB, Logger, IPC, Rate Limiter API 명세 |
| `data-indicators.md` | Team 2 | Bybit API 래핑, WebSocket, 지표 계산 공식 |
| `strategy-scanner.md` | Team 3 | 스캐너 로직, 스코어링 공식 |
| `strategy-position.md` | Team 3 | 진입/청산 조건, SL/TP 계산 |
| `strategy-asset.md` | Team 3 | 사이징 공식, 거부 조건, 드로다운 |
| `execution.md` | Team 4 | 주문 흐름, Paper/Live, 가상 체결 |
| `position-tracking.md` | Team 4 | P&L 계산, 성과 지표 |
| `gui-api.md` | Team 5 | REST 엔드포인트, WebSocket 메시지 |
| `gui-screens.md` | Team 5 | 화면별 데이터 바인딩 |

## 문서 작성 규칙

1. **인터페이스 우선** — 함수 시그니처, 입출력 타입, 사용 예시를 먼저 기술
2. **변경 시 업데이트** — 코드 변경이 문서에 영향을 주면 반드시 동시 업데이트
3. **의존하는 팀에 공유** — 인터페이스 변경 시 의존 팀에 알림
