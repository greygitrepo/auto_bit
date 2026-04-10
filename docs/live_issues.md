# 실전 거래 문제점 (2026-04-09)

## 실전 환경
- 잔고: 27.54 USDT (시작) → 27.26 USDT (5분 후)
- 모드: live, compound, 듀얼 전략 (grid_bias + volatility_breakout)
- 드로다운 한도: 50% (13.77 USDT 이하 시 자동 중단)

## 발견된 문제점

### 1. Unified Account 마진 모드 설정 오류 — ✅ 수정 완료
```
ERROR: set_margin_mode failed: unified account is forbidden (ErrCode: 100028)
```
- **원인**: Bybit UTA에서는 `set_margin_mode` API 사용 불가
- **수정**: ErrCode 100028을 무시하고 레버리지 설정만 진행하도록 변경

### 2. TP PnL이 0으로 기록 — ✅ 수정 완료
```
Grid TP executed: BSBUSDT idx=-3 pnl=0.000000 fee=0.000000
```
- **원인**: LiveExecutor의 close_position/close_partial이 PnL/fee를 반환하지 않음
- **수정**: 청산 후 `get_executions`에서 fee, `get_closed_pnl`에서 PnL을 조회하여 반환

### 3. Grid Fill 후 즉시 TP (관찰 필요)
```
20:20:22 FILL BSBUSDT Buy idx=-3
20:25:04 TP BSBUSDT idx=-3 (5분 후)
```
- 5분 만에 fill→TP 순환 완료. 이건 정상 동작이나, 실전에서는 체결 지연으로 인한 슬리피지 확인 필요

### 4. 잔고 변화: -0.28 USDT (5분)
- 시작: 27.54 → 현재: 27.26 = **-0.28 USDT (-1.0%)**
- 수수료와 슬리피지로 인한 손실 가능성
- TP PnL이 0으로 기록되어 정확한 분석 불가

## 아직 미발생 (관찰 중)
- Position 전략(volatility_breakout) 실전 거래 — 시그널 1건 발생했으나 체결 미확인
- 펀딩비 영향
- 네트워크/API 지연에 의한 주문 실패
- 동시 다수 심볼 주문 시 rate limit

### 3. BybitClient.set_margin_mode retry 3회 후 에러 전파 — ✅ 수정 완료
```
ERROR: set_margin_mode failed after 3 retries: unified account is forbidden (ErrCode: 100028)
```
- **원인**: LiveExecutor에서 100028을 catch하기 전에 BybitClient의 _retry 데코레이터가 3회 재시도 후 raise
- **영향**: OrderManager.execute_order에서 에러 전파 → Position 전략 주문 실패
- **수정**: BybitClient.set_margin_mode 내부에서 100028/110026 에러를 catch하여 즉시 반환

### 4. 재시작 시 Grid 포지션 고아화 — ✅ 수정 완료
- **원인**: GridPositionManager의 `_level_positions` 매핑이 메모리에만 존재. 재시작 시 초기화되어 기존 포지션과의 연결이 끊김
- **증상**: TP_HIT 시 "no position for symbol" 경고 → 청산 불가 → 포지션 영구 잔류
- **수정**: 
  - `GridPositionManager.restore_from_positions()` 메서드 추가
  - P3 초기화 시 DB의 오픈 포지션(strategy=grid_bias/recovered)을 _level_positions에 복원
  - Live mode에서는 LivePositionLedger에도 복원

### 5. Grid spacing이 실전 비용보다 작음 — ✅ 수정 완료
```
min_spacing 0.6% < 왕복 비용 0.71% (fee 0.055%×2 + slippage 30bps×2)
```
- **원인**: 페이퍼 슬리피지 15bps 기준으로 설정. 실전 알트코인은 30~50bps
- **영향**: 모든 Grid TP 거래에서 순손실 발생
- **수정**: 
  - min_spacing_pct: 0.6% → 1.0%
  - target_spacing_pct: 0.6% → 1.0%
  - paper slippage_bps: 15 → 30
  - paper taker fee: 0.0006 → 0.00055 (실전과 동일)

### 6. DB PnL 기록 부정확 (타이밍) — ✅ 수정 완료
- **원인**: 청산 직후 get_closed_pnl 호출 시 Bybit에 아직 반영 안 됨 (0.3초 대기 부족)
- **수정**: 대기 시간 0.3초 → 1.0초, limit 5 → 10

### 7. API Rate Limit 히스토리 로딩 시 (MINOR) — ✅ 완화
- pybit 자동 재시도로 치명적이지 않으나, 로딩 간격 0.2→0.35초로 확대

### 8. set_leverage "not modified" (110043) 주문 실패 — ✅ 수정 완료
```
Order execution failed: set_leverage failed after 3 retries: leverage not modified (ErrCode: 110043)
```
- **원인**: 레버리지 이미 동일 값. pybit exception → _retry 3회 → raise → 주문 자체 실패
- **수정**: set_leverage에서 @_retry 제거, 110043 내부 catch

### 9. SL/TP 조건부 주문 Qty invalid (10001) — ✅ 수정 완료
- **원인**: set_sl_tp에서 qty를 _round_qty 없이 전달 → qtyStep 불일치
- **수정**: set_sl_tp 시작 시 qty = _round_qty(symbol, qty) 적용

### 10. Live 최소 주문 금액 5 USDT 미달 (110094) — ✅ 수정 완료
- **원인**: LiveExecutor.place_market_order에 min notional 체크 없음
- **수정**: 주문 전 notional 계산 후 minNotionalValue 미만이면 reject 반환

### 11. Trading Terms 미동의 (110123) — 관찰 중
- XAGUSDT(은) 등 특정 상품 거래 시 Bybit 약관 동의 필요
- 비치명적 (주문만 스킵). 블랙리스트 추가 또는 에러 시 심볼 자동 제외 검토

### 12. 잔고 부족 (110007) — 관찰 중
- 10개 포지션 동시 오픈으로 마진 소진
- 비치명적 (신규 주문만 거부). max_concurrent_positions 설정으로 제어 가능

### 12. 잔고 부족 (110007) — ✅ 수정 완료
- **수정**: LiveExecutor.place_market_order에서 주문 전 가용 마진 체크
  - Bybit API로 availableToWithdraw 조회
  - 필요 마진 = notional / leverage
  - 가용 잔고의 80% 초과 시 주문 거부 (20% 여유 확보)
  - leverage 캐시로 정확한 마진 계산

### 13. 포지션 청산 후 SL/TP 조건부 주문 미취소 — ✅ 수정 완료
- **원인**: Grid TP나 전략 청산으로 포지션 닫혀도 Bybit 서버의 SL/TP 조건부 주문이 잔존
- **위험**: 가격이 SL/TP에 닿으면 포지션 없이 신규 포지션 열림
- **수정**:
  - grid_manager._handle_tp_hit: 청산 전 sl_order_id/tp_order_id cancel_orders 호출
  - process._monitor_positions: SL/TP 모니터링 청산에서도 cancel_orders 호출
  - order_manager.close_position: 기존 구현 확인 (이미 cancel 있음)

### 14. Bybit 서버사이드 SL/TP 체결 시 P3 미감지 → 고아 주문 잔존 — ✅ 수정 완료
- **원인**: Bybit이 서버사이드 SL/TP로 포지션을 자동 청산하면 P3가 이를 감지하지 못함
  → DB에 포지션이 열린 상태로 남음 → 반대편 TP/SL 조건부 주문 미취소
- **위험**: 고아 조건부 주문이 트리거되면 의도치 않은 신규 포지션 열림
- **수정**: `_sync_exchange_positions()` 메서드 추가
  - 매 모니터링 주기마다 DB vs exchange 포지션 비교
  - exchange에 없는 DB 포지션 발견 시:
    1. 해당 심볼의 SL/TP 조건부 주문 취소
    2. 심볼의 모든 미체결 주문 취소 (안전망)
    3. DB 포지션 기록 청산
    4. Grid manager 매핑 정리
