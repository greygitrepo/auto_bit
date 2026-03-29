# ADR-019: 시스템 장애 복구 전략

## 상태

`결정됨`

## 날짜

2026-03-25

## 맥락

멀티프로세스 시스템이 예기치 않게 종료되었을 때, 재시작 후 안전하게 상태를 복구해야 한다.
특히 Bybit에 열린 포지션과 서버사이드 SL/TP 주문이 남아있을 수 있다.

## 결정

### 복구 흐름

```
[시스템 시작]
  │
  ├─ 1. DB에서 마지막 상태 로드
  │     - 실행 모드 (paper/live)
  │     - 일일 통계 (손실, 거래횟수, 연속손절)
  │     - 드로다운 단계
  │
  ├─ 2. Bybit 동기화 (Live 모드)
  │     - GET /v5/position/list → 현재 열린 포지션 조회
  │     - GET /v5/order/realtime → 미체결 주문 (SL/TP) 조회
  │     - 잔고 조회
  │
  ├─ 3. 상태 비교 및 정합성 확인
  │     │
  │     ├─ DB 포지션 있음 + Bybit 포지션 있음 (일치)
  │     │   → 정상 복구, 관리 재개
  │     │   → 진입 시간 기준 90분 타이머 재설정
  │     │   → SL/TP 서버사이드 주문 존재 확인
  │     │
  │     ├─ DB 포지션 있음 + Bybit 포지션 없음
  │     │   → 장애 중 SL/TP로 청산됨
  │     │   → Bybit 체결 이력 조회하여 청산 기록 DB 반영
  │     │
  │     ├─ DB 포지션 없음 + Bybit 포지션 있음
  │     │   → 비정상 상태 (DB 기록 누락)
  │     │   → Bybit 포지션 정보를 DB에 기록
  │     │   → SL/TP 서버사이드 주문이 있으면 관리 재개
  │     │   → SL/TP 없으면 즉시 시장가 청산 (안전)
  │     │
  │     └─ DB 포지션 없음 + Bybit 포지션 없음
  │         → 깨끗한 상태, 정상 시작
  │
  ├─ 4. Paper 모드 복구
  │     - DB에서 가상 잔고, 가상 포지션 복원
  │     - 가상 포지션의 90분 타이머 재설정
  │     - 장애 중 SL/TP 도달 여부는 확인 불가 → 현재가 기준 재평가
  │       (SL 도달했을 가격대면 손절 처리, TP 도달했으면 익절 처리)
  │
  └─ 5. 정상 운영 시작
        - Data Collector: BTC/ETH + 보유 종목 WebSocket 구독
        - Strategy Engine: 보유 종목 지표 계산 재개
        - 빈 슬롯 있으면 스캐너 실행
```

### 상태 체크포인트

시스템 운영 중 주요 이벤트마다 DB에 상태를 기록한다.

```sql
CREATE TABLE system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER
);

-- 기록하는 항목:
-- last_heartbeat: 마지막 동작 시간
-- daily_pnl: 오늘 누적 손익
-- daily_trade_count: 오늘 거래 횟수
-- consecutive_losses: 연속 손절 횟수
-- cooldown_until: 쿨다운 해제 시간
-- drawdown_stage: 현재 드로다운 단계 (0/1/2/3)
-- trading_enabled: 거래 가능 상태
```

### 90분 타이머 복구

```
포지션 진입 시간을 DB에 기록.
재시작 시:
  경과 시간 = 현재 시간 - 진입 시간
  경과 시간 ≥ 90분 → 즉시 시장가 청산
  경과 시간 < 90분 → 남은 시간으로 타이머 재설정
```

### Graceful Shutdown

정상 종료 시:

```
SIGTERM / SIGINT 수신:
  1. 새 포지션 진입 중단
  2. 모든 프로세스에 종료 신호 전파
  3. 현재 상태 DB에 저장
  4. 열린 포지션은 그대로 유지 (SL/TP 서버사이드가 보호)
  5. WebSocket 연결 정리
  6. 프로세스 종료
```

주의: 열린 포지션을 종료 시 강제 청산하지 않는다. SL/TP 서버사이드 주문이 보호하고 있으며, 시스템 재시작 시 관리를 재개한다.

## 영향

- DB: `system_state` 테이블 추가
- DB: `positions` 테이블에 `entered_at` 타임스탬프 필수
- Main Process: 시작 시 복구 로직 실행 후 정상 루프 진입
- Order Manager: Bybit API 동기화 로직 구현
- 모든 프로세스: SIGTERM 핸들러 구현
