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
